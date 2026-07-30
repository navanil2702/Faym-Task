"""One-time-code handling tests."""

from __future__ import annotations

import threading

import pytest

from faym_returns import otp, progress
from faym_returns.models import AgentAbort


@pytest.fixture(autouse=True)
def clean():
    progress.bus.reset()
    progress.stop.clear()
    otp.broker.cancel()
    otp.broker._ready.clear()
    otp.broker._pending = None
    yield
    progress.stop.clear()


# ------------------------------------------------------------------- sanitising


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("123456", "123456"),
        (" 123 456 ", "123456"),
        ("12-34-56", "123456"),
        ("OTP: 998877", "998877"),
        ("", None),
        ("   ", None),
        ("abcdef", None),
        (None, None),
    ],
)
def test_clean_keeps_only_digits(raw, expected):
    assert otp._clean(raw) == expected


# ---------------------------------------------------------------------- broker


def test_supply_releases_a_waiting_request():
    result: list = []

    def wait():
        result.append(otp.broker.request(platform="Flipkart", phone="9205359199"))

    thread = threading.Thread(target=wait, daemon=True)
    thread.start()
    # Let the agent thread register its wait before supplying the code.
    for _ in range(50):
        if otp.broker.pending:
            break
        threading.Event().wait(0.02)
    assert otp.broker.supply("123456") is True
    thread.join(timeout=5)
    assert result == ["123456"]


def test_supply_is_rejected_when_nothing_is_waiting():
    assert otp.broker.supply("123456") is False


def test_pending_describes_what_is_being_waited_on():
    thread = threading.Thread(
        target=lambda: otp.broker.request(platform="Flipkart", phone="9205359199"),
        daemon=True,
    )
    thread.start()
    for _ in range(50):
        if otp.broker.pending:
            break
        threading.Event().wait(0.02)
    pending = otp.broker.pending
    assert pending["platform"] == "Flipkart"
    assert pending["phone"] == "9205359199"
    otp.broker.cancel()
    thread.join(timeout=5)


def test_cancel_yields_no_code_so_the_agent_falls_back():
    result: list = []
    thread = threading.Thread(
        target=lambda: result.append(
            otp.broker.request(platform="Flipkart", phone="9205359199")
        ),
        daemon=True,
    )
    thread.start()
    for _ in range(50):
        if otp.broker.pending:
            break
        threading.Event().wait(0.02)
    otp.broker.cancel()
    thread.join(timeout=5)
    assert result == [None]


def test_request_times_out_and_returns_none():
    assert otp.broker.request(platform="Flipkart", phone="9205359199", timeout_s=0) is None


def test_request_publishes_so_the_ui_can_prompt():
    listener = progress.bus.subscribe()
    otp.broker.request(platform="Flipkart", phone="9205359199", timeout_s=0)
    kinds = []
    while not listener.empty():
        kinds.append(listener.get_nowait().kind)
    assert "otp_required" in kinds


def test_a_stop_request_interrupts_the_wait():
    """An operator hitting Stop must not have to wait out the OTP timeout."""
    progress.stop.request()
    with pytest.raises(AgentAbort, match="OTP"):
        otp.broker.request(platform="Flipkart", phone="9205359199", timeout_s=30)
