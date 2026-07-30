"""Run observability: a progress bus and a cooperative stop signal.

The agent is a synchronous, long-blocking process (real browser, deliberate
multi-second pauses). To drive it from a UI, two things are needed that the CLI
never wanted: a way to watch a run while it happens, and a way to stop one
mid-flight.

Both are module-level singletons rather than parameters threaded through every
call. That is a deliberate trade: the agent already forbids running two sessions
against one account concurrently, so "one active run per process" is an existing
invariant, not a new limitation. Keeping the bus out of the call signatures means
the CLI path is untouched and publishing from deep inside an adapter costs one
import.
"""

from __future__ import annotations

import datetime as dt
import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Event:
    kind: str
    data: dict[str, Any] = field(default_factory=dict)
    seq: int = 0
    at: str = ""

    def as_dict(self) -> dict:
        return {"kind": self.kind, "seq": self.seq, "at": self.at, **self.data}


class Bus:
    """Fan-out of run events to any number of listeners.

    Keeps a bounded history so a browser that connects late (or reconnects after
    a dropped stream) can replay what it missed instead of showing a blank run.
    """

    def __init__(self, history_limit: int = 2000):
        self._lock = threading.Lock()
        self._listeners: list[queue.Queue[Event]] = []
        self._history: list[Event] = []
        self._history_limit = history_limit
        self._seq = 0

    def publish(self, kind: str, **data: Any) -> Event:
        with self._lock:
            self._seq += 1
            event = Event(
                kind=kind,
                data=data,
                seq=self._seq,
                at=dt.datetime.now().isoformat(timespec="seconds"),
            )
            self._history.append(event)
            if len(self._history) > self._history_limit:
                del self._history[: len(self._history) - self._history_limit]
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener.put_nowait(event)
            except queue.Full:  # pragma: no cover - listener is wedged; drop it
                pass
        return event

    def subscribe(self, replay_from: int = 0) -> queue.Queue[Event]:
        """Register a listener, pre-loaded with any history it missed."""
        listener: queue.Queue[Event] = queue.Queue(maxsize=4000)
        with self._lock:
            for event in self._history:
                if event.seq > replay_from:
                    listener.put_nowait(event)
            self._listeners.append(listener)
        return listener

    def unsubscribe(self, listener: queue.Queue[Event]) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def history(self, since: int = 0) -> list[Event]:
        with self._lock:
            return [e for e in self._history if e.seq > since]

    def reset(self) -> None:
        """Drop history for a new run, but keep the sequence monotonic.

        Rewinding the counter would break reconnect: a browser holding
        ``lastSeq = 12`` asks for events after 12, so a fresh run starting again
        at 1 would have its opening events filtered out and never displayed.
        """
        with self._lock:
            self._history.clear()


class StopSignal:
    """Cooperative abort, checked wherever the agent sleeps.

    A run spends most of its wall-clock time in the deliberate pauses between
    line items, so that is where a stop request lands. Aborting inside a pause
    is also the only safe place to stop: it is never mid-way through a return
    submission, so a stop can't leave a half-filed return behind.
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def request(self) -> None:
        self._event.set()

    def clear(self) -> None:
        self._event.clear()

    @property
    def requested(self) -> bool:
        return self._event.is_set()


bus = Bus()
stop = StopSignal()


def publish(kind: str, **data: Any) -> None:
    bus.publish(kind, **data)


def reset() -> None:
    """Clear bus history and any pending stop, before starting a fresh run."""
    bus.reset()
    stop.clear()


def check_stop(message: str = "Stopped by the operator.") -> None:
    """Raise if a stop was requested. Imported lazily to avoid a cycle."""
    if stop.requested:
        from .models import AgentAbort

        raise AgentAbort(message)


class _Silent:
    """No-op stand-in used by code paths that must not publish."""

    def publish(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def describe(event: Event) -> Optional[str]:
    """Human-readable one-liner for an event, for the UI's activity feed."""
    d = event.data
    match event.kind:
        case "run_started":
            mode = "LIVE" if d.get("live") else "dry run"
            return f"Run started ({mode})."
        case "plan_ready":
            return (
                f"{d.get('orders', 0)} order(s) -> {d.get('total', 0)} line item(s); "
                f"{d.get('to_attempt', 0)} to attempt, {d.get('decided', 0)} settled without a browser."
            )
        case "browser_launched":
            return "Chrome launched with the saved profile."
        case "login_required":
            return f"Waiting for you to sign in to {d.get('platform', '')} in the browser window."
        case "login_ok":
            return f"{d.get('platform', '')} session is active."
        case "order_started":
            return f"Opening order {d.get('order_id', '')} ({d.get('items', 0)} item(s))."
        case "item_started":
            return f"Working on {d.get('title') or d.get('sku', '')}."
        case "item_finished":
            return f"{d.get('title') or d.get('sku', '')}: {d.get('status', '')}."
        case "waiting":
            return f"Pausing {int(d.get('seconds', 0))}s ({d.get('reason', '')})."
        case "screenshot":
            return f"Saved screenshot: {d.get('label', '')}."
        case "aborted":
            return f"Run aborted: {d.get('reason', '')}"
        case "run_finished":
            return "Run finished."
        case _:
            return None
