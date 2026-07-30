"""FastAPI control panel for the returns agent.

Two constraints drive the shape of this:

The agent is synchronous and long-blocking.
    Playwright's sync API cannot run inside an asyncio event loop, so a run
    executes on a worker thread and reports back over the progress bus. Request
    handlers never block on the agent.

One run at a time, per account.
    Driving two browser sessions against one Flipkart account concurrently is a
    reliable way to get it flagged, so the server holds a single run slot and
    refuses a second start with 409 rather than queueing it.

Nothing here re-implements agent logic - it drives the same Orchestrator the CLI
does, so the browser behaviour, the safety rails, and the Excel write-back are
identical whichever front end you use.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import queue
import threading
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from .. import eligibility, progress
from ..browser import SessionConfig
from ..humanize import Pacing
from ..models import Platform
from ..normalize import explode_rows
from ..orchestrator import Orchestrator, RunOptions
from ..workbook import ReturnsWorkbook, prepare_working_copy

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent.parent
STATIC = Path(__file__).resolve().parent / "static"
UPLOADS = ROOT / "data" / "uploads"
RESULTS = ROOT / "data"

#: Typed exactly, in the live-run dialog, mirroring the CLI's confirmation.
LIVE_CONFIRMATION = "place returns"


class RunManager:
    """Owns the single active run and the thread it executes on."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self.state: dict[str, Any] = {"status": "idle"}

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(
        self,
        *,
        source: Path,
        live: bool,
        limit: Optional[int],
        today: Optional[dt.date],
        orders: list[str],
        platforms: list[Platform],
        offline: bool,
        fresh: bool,
    ) -> dict:
        with self._lock:
            if self.busy:
                raise HTTPException(
                    409,
                    "A run is already in progress. Only one browser session may "
                    "drive an account at a time.",
                )

            progress.reset()
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            working = RESULTS / f"{source.stem}-results.xlsx"
            if fresh or not working.exists():
                prepare_working_copy(source, working)

            self.state = {
                "status": "running",
                "started_at": dt.datetime.now().isoformat(timespec="seconds"),
                "live": live,
                "offline": offline,
                "source": str(source),
                "results": str(working),
                "artifacts": str(ROOT / "runs" / stamp),
                "summary": None,
                "error": None,
            }

            self._thread = threading.Thread(
                target=self._run,
                name="faym-returns-run",
                daemon=True,
                kwargs=dict(
                    working=working,
                    live=live,
                    limit=limit,
                    today=today,
                    orders=orders,
                    platforms=platforms,
                    offline=offline,
                    fresh=fresh,
                    artifacts=ROOT / "runs" / stamp,
                ),
            )
            self._thread.start()
            return dict(self.state)

    def _run(
        self,
        *,
        working: Path,
        live: bool,
        limit: Optional[int],
        today: Optional[dt.date],
        orders: list[str],
        platforms: list[Platform],
        offline: bool,
        fresh: bool,
        artifacts: Path,
    ) -> None:
        try:
            book = ReturnsWorkbook(working)
            options = RunOptions(
                dry_run=not live,
                offline=offline,
                limit=limit,
                only_orders=tuple(orders),
                only_platforms=tuple(platforms),
                resume=not fresh,
                today=today,
            )
            config = SessionConfig(
                profile_dir=ROOT / ".profiles" / "default",
                artifacts_dir=artifacts,
                # The operator is present and watching, which is exactly the
                # supervision quiet hours exist to require - so they don't apply.
                quiet_hours=(0, 0),
                pacing=Pacing(),
            )
            report = Orchestrator(book, config, options).run()
            self.state["summary"] = {
                "processed": len(report.results),
                "planned": len(report.planned),
                "needs_review": len(report.needs_review),
                "counts": report.counts,
                "aborted": report.aborted,
            }
            self.state["status"] = "finished"
        except Exception as exc:  # noqa: BLE001 - surface it, never lose it
            log.exception("Run failed")
            self.state["status"] = "failed"
            self.state["error"] = str(exc)
            progress.publish("aborted", reason=str(exc))
            progress.publish("run_finished", error=str(exc))
        finally:
            progress.stop.clear()

    def request_stop(self) -> None:
        if not self.busy:
            raise HTTPException(409, "No run is in progress.")
        progress.stop.request()
        progress.publish("stop_requested")


manager = RunManager()
app = FastAPI(title="Faym Returns Agent", docs_url=None, redoc_url=None)


# ------------------------------------------------------------------ page shell


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse((STATIC / "index.html").read_text())


# ------------------------------------------------------------------- inspecting


def _resolve_source(raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (ROOT / path).resolve()
    if not path.exists():
        raise HTTPException(404, f"No such file: {path}")
    if path.suffix.lower() not in {".xlsx", ".xlsm"}:
        raise HTTPException(400, "Expected an .xlsx workbook.")
    return path


@app.post("/api/upload")
async def upload(file: UploadFile) -> dict:
    UPLOADS.mkdir(parents=True, exist_ok=True)
    name = Path(file.filename or "upload.xlsx").name
    if not name.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Expected an .xlsx workbook.")
    target = UPLOADS / name
    target.write_bytes(await file.read())
    return {"path": str(target), "name": name}


@app.get("/api/inspect")
async def inspect(
    path: str, today: Optional[str] = None, fresh: bool = False
) -> dict:
    """Parse the workbook and report what a run would do. Never opens a browser.

    Reads whichever book a run would read: the working copy when one exists and
    we are resuming, the pristine source when starting fresh. Otherwise the panel
    would show counts from the source while the run acted on the copy, and the
    two would disagree the moment any progress had been recorded.
    """
    source = _resolve_source(path)
    as_of = dt.date.fromisoformat(today) if today else None

    working = RESULTS / f"{source.stem}-results.xlsx"
    resuming = working.exists() and not fresh
    book = ReturnsWorkbook(working if resuming else source)
    items = explode_rows(book.pending_order_rows())

    orders: dict[str, dict] = {}
    for item in items:
        verdict = eligibility.check(item, today=as_of)
        bucket = orders.setdefault(
            item.order_id,
            {
                "order_id": item.order_id,
                "platform": item.platform.value if item.platform else None,
                "source_row": item.source_row,
                "qty_declared": item.qty_declared,
                "window_days": item.return_window_days,
                "delivery": item.delivery_date.isoformat() if item.delivery_date else None,
                "delivery_approx": item.delivery_date_is_approx,
                "order_total": item.order_total,
                "notes": item.parse_notes,
                "items": [],
            },
        )
        bucket["items"].append(
            {
                "index": item.item_index,
                "sku": item.sku,
                "title": item.title_hint,
                "ordered": item.ordered,
                "url": item.product_url,
                "eligible": verdict.eligible and item.ordered,
                "confident": verdict.confident,
                "reason": verdict.reason,
            }
        )

    ordered = [i for i in items if i.ordered]
    attemptable = sum(
        1
        for i in ordered
        if eligibility.check(i, today=as_of).eligible and i.sku and i.platform
    )
    return {
        "source": str(source),
        "name": source.name,
        "resuming": resuming,
        "results_path": str(working) if working.exists() else None,
        "orders": list(orders.values()),
        "totals": {
            "orders": len(orders),
            "line_items": len(items),
            "ordered": len(ordered),
            "attemptable": attemptable,
        },
        "today": (as_of or dt.date.today()).isoformat(),
    }


# ------------------------------------------------------------------- run control


@app.post("/api/run")
async def start_run(request: Request) -> dict:
    body = await request.json()
    source = _resolve_source(body.get("path", ""))
    live = bool(body.get("live"))

    if live and (body.get("confirmation") or "").strip().lower() != LIVE_CONFIRMATION:
        raise HTTPException(
            400,
            f"A live run needs the confirmation phrase {LIVE_CONFIRMATION!r} "
            "typed exactly. Nothing was submitted.",
        )

    today = body.get("today")
    return manager.start(
        source=source,
        live=live,
        offline=bool(body.get("offline")),
        limit=int(body["limit"]) if body.get("limit") else None,
        today=dt.date.fromisoformat(today) if today else None,
        orders=[o for o in (body.get("orders") or []) if o],
        platforms=[Platform(p) for p in (body.get("platforms") or []) if p],
        fresh=bool(body.get("fresh")),
    )


@app.post("/api/stop")
async def stop_run() -> dict:
    manager.request_stop()
    return {"status": "stopping"}


@app.get("/api/status")
async def status() -> dict:
    return {"busy": manager.busy, **manager.state}


@app.get("/api/events")
async def events(request: Request, since: int = 0) -> StreamingResponse:
    """Server-sent events, replaying anything the browser missed.

    A run can take many minutes across long deliberate pauses, so a dropped
    connection is expected; `since` lets a reconnecting browser resume exactly
    where it left off instead of showing a blank run.
    """
    listener = progress.bus.subscribe(replay_from=since)

    async def stream():
        try:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.to_thread(listener.get, True, 15.0)
                except queue.Empty:
                    yield ": keep-alive\n\n"  # hold the connection open
                    continue
                payload = event.as_dict()
                described = progress.describe(event)
                if described:
                    payload["message"] = described
                yield f"id: {event.seq}\ndata: {json.dumps(payload)}\n\n"
        finally:
            progress.bus.unsubscribe(listener)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------- outputs


@app.get("/api/results")
async def download_results() -> FileResponse:
    path = manager.state.get("results")
    if not path or not Path(path).exists():
        raise HTTPException(404, "No results workbook yet - run the agent first.")
    return FileResponse(
        path,
        filename=Path(path).name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/api/screenshot/{name}")
async def screenshot(name: str) -> FileResponse:
    # Resolve under the runs directory only; never trust the name as a path.
    safe = Path(name).name
    for candidate in sorted((ROOT / "runs").glob(f"*/{safe}"), reverse=True):
        return FileResponse(candidate, media_type="image/png")
    raise HTTPException(404, "No such screenshot.")


def main() -> None:
    import uvicorn

    print("\n  Faym Returns control panel -> http://127.0.0.1:8000\n")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")


if __name__ == "__main__":
    main()
