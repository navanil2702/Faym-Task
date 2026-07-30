"""Control-panel API tests, focused on the gates that protect money and files."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from faym_returns.webapp.server import LIVE_CONFIRMATION, app  # noqa: E402

SOURCE = Path.home() / "Downloads" / "Faym Status Test Orders.xlsx"


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


# ------------------------------------------------------------------ live gate


def test_live_run_refused_without_the_confirmation_phrase(client: TestClient):
    res = client.post("/api/run", json={"path": str(SOURCE), "live": True})
    assert res.status_code == 400
    assert LIVE_CONFIRMATION in res.json()["detail"]
    assert "Nothing was submitted" in res.json()["detail"]


def test_live_run_refused_with_a_wrong_phrase(client: TestClient):
    res = client.post(
        "/api/run",
        json={"path": str(SOURCE), "live": True, "confirmation": "yes go ahead"},
    )
    assert res.status_code == 400


def test_confirmation_is_case_and_space_insensitive(client: TestClient):
    """The phrase must match, but not punish capitalisation or a stray space."""
    res = client.post(
        "/api/run",
        json={
            "path": "/tmp/definitely-missing.xlsx",
            "live": True,
            "confirmation": "  Place Returns ",
        },
    )
    # Rejected for the missing file, not the phrase - so the phrase was accepted.
    assert res.status_code == 404


# ------------------------------------------------------------- input validation


def test_missing_workbook_is_404(client: TestClient):
    assert client.get("/api/inspect", params={"path": "/tmp/nope.xlsx"}).status_code == 404


def test_non_xlsx_path_is_rejected(client: TestClient, tmp_path: Path):
    stray = tmp_path / "notes.txt"
    stray.write_text("not a workbook")
    res = client.get("/api/inspect", params={"path": str(stray)})
    assert res.status_code == 400


def test_screenshot_name_cannot_escape_the_runs_directory(client: TestClient):
    """The name is a filename, never a path - traversal must not read the disk."""
    for attempt in ("../../../etc/passwd", "..%2f..%2fetc%2fpasswd", "/etc/passwd"):
        assert client.get(f"/api/screenshot/{attempt}").status_code == 404


def test_stop_without_a_run_is_rejected(client: TestClient):
    res = client.post("/api/stop")
    assert res.status_code == 409


def test_results_download_before_any_run_is_404(client: TestClient):
    from faym_returns.webapp import server

    server.manager.state.pop("results", None)
    assert client.get("/api/results").status_code == 404


# -------------------------------------------------------------------- inspect


@pytest.mark.skipif(not SOURCE.exists(), reason="test dataset not in ~/Downloads")
def test_inspect_reports_the_line_item_explosion(client: TestClient):
    res = client.get(
        "/api/inspect", params={"path": str(SOURCE), "fresh": "true"}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["totals"] == {
        "orders": 7,
        "line_items": 16,
        "ordered": 14,
        "attemptable": 0,  # every window in the dataset has closed
    }


@pytest.mark.skipif(not SOURCE.exists(), reason="test dataset not in ~/Downloads")
def test_inspect_backdated_matches_what_a_run_would_attempt(client: TestClient):
    res = client.get(
        "/api/inspect",
        params={"path": str(SOURCE), "today": "2026-07-06", "fresh": "true"},
    )
    body = res.json()
    assert body["totals"]["attemptable"] == 13
    na = [
        item
        for order in body["orders"]
        for item in order["items"]
        if not item["ordered"]
    ]
    assert len(na) == 2, "the two NA-marked links must be reported as not ordered"


@pytest.mark.skipif(not SOURCE.exists(), reason="test dataset not in ~/Downloads")
def test_inspect_surfaces_the_approximate_delivery_note(client: TestClient):
    body = client.get(
        "/api/inspect", params={"path": str(SOURCE), "fresh": "true"}
    ).json()
    notes = [n for o in body["orders"] for n in o["notes"]]
    assert any("approximated" in n for n in notes)


# ------------------------------------------------------------------------- OTP


def test_otp_endpoint_reports_nothing_pending_when_idle(client: TestClient):
    assert client.get("/api/otp").json()["pending"] is None


def test_supplying_a_code_nobody_asked_for_is_rejected(client: TestClient):
    res = client.post("/api/otp", json={"code": "123456"})
    assert res.status_code == 409


def test_a_code_with_no_digits_is_rejected(client: TestClient):
    res = client.post("/api/otp", json={"code": "not-a-code"})
    assert res.status_code == 400


def test_cancelling_otp_is_always_safe(client: TestClient):
    assert client.post("/api/otp/cancel").status_code == 200
