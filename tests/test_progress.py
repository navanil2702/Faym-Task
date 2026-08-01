"""Cooperative-stop tests.

A stop must land only where the agent sleeps. That is what keeps an interrupt
from tearing through the middle of a return submission.
"""

from __future__ import annotations

import pytest

from faym_returns import progress
from faym_returns.models import AgentAbort


@pytest.fixture(autouse=True)
def clean():
    progress.stop.clear()
    yield
    progress.stop.clear()


def test_check_stop_is_quiet_until_requested():
    progress.check_stop()  # must not raise


def test_check_stop_raises_once_requested():
    progress.stop.request()
    with pytest.raises(AgentAbort, match="Stopped by the operator"):
        progress.check_stop()


def test_the_abort_carries_the_caller_s_message():
    """Each wait site explains what it was waiting on, so the log says why."""
    progress.stop.request()
    with pytest.raises(AgentAbort, match="waiting for an OTP"):
        progress.check_stop("Stopped by the operator while waiting for an OTP.")


def test_stop_can_be_cleared_for_the_next_run():
    progress.stop.request()
    progress.stop.clear()
    progress.check_stop()  # must not raise


def test_reset_clears_a_pending_stop():
    """A stop left set by an aborted run must not kill the next one."""
    progress.stop.request()
    progress.reset()
    assert progress.stop.requested is False


def test_requested_reflects_current_state():
    assert progress.stop.requested is False
    progress.stop.request()
    assert progress.stop.requested is True
