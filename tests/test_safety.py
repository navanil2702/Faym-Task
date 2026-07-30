"""Tests for the guards that stop the agent doing something it shouldn't.

These need no browser: they exercise the refusals and the planning decisions.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from faym_returns.browser import CHALLENGE_MARKERS, Session, SessionConfig
from faym_returns.models import AgentAbort, Outcome, ReturnStatus, TaskStatus


def _config(**overrides) -> SessionConfig:
    base = dict(
        profile_dir=Path("/tmp/faym-test-profile"),
        artifacts_dir=Path("/tmp/faym-test-artifacts"),
        quiet_hours=(1, 7),
    )
    base.update(overrides)
    return SessionConfig(**base)


def test_headless_is_refused():
    """Headless Chrome is trivially detectable, so it is rejected outright."""
    with pytest.raises(ValueError, match="Headless mode is refused"):
        Session(_config(headless=True))


def test_quiet_hours_block_a_run():
    session = Session(_config(quiet_hours=(1, 7)))
    with pytest.raises(AgentAbort, match="quiet hours"):
        session.check_quiet_hours(dt.datetime(2026, 7, 30, 3, 30))


def test_outside_quiet_hours_is_allowed():
    session = Session(_config(quiet_hours=(1, 7)))
    session.check_quiet_hours(dt.datetime(2026, 7, 30, 14, 0))  # no raise


def test_quiet_hours_wrapping_midnight():
    session = Session(_config(quiet_hours=(23, 6)))
    with pytest.raises(AgentAbort):
        session.check_quiet_hours(dt.datetime(2026, 7, 30, 23, 30))
    with pytest.raises(AgentAbort):
        session.check_quiet_hours(dt.datetime(2026, 7, 30, 2, 0))
    session.check_quiet_hours(dt.datetime(2026, 7, 30, 12, 0))  # no raise


def test_quiet_hours_can_be_disabled():
    session = Session(_config(quiet_hours=(0, 0)))
    session.check_quiet_hours(dt.datetime(2026, 7, 30, 3, 30))  # no raise


def test_captcha_markers_cover_the_common_wording():
    for phrase in ("captcha", "unusual traffic", "verify you are human"):
        assert phrase in CHALLENGE_MARKERS


# ------------------------------------------------------------ outcome semantics


def test_failed_is_not_final_so_it_will_be_retried():
    assert ReturnStatus.FAILED.is_final is False
    assert ReturnStatus.PLACED.is_final is True


def test_planned_is_not_final_and_stays_pending():
    """An offline plan recorded no observation, so it must not read as Done."""
    outcome = Outcome(status=ReturnStatus.PLANNED)
    assert ReturnStatus.PLANNED.is_final is False
    assert outcome.task_status is TaskStatus.PENDING


def test_support_needed_routes_to_a_human():
    assert Outcome(status=ReturnStatus.SUPPORT_NEEDED).task_status is TaskStatus.NEEDS_REVIEW


def test_out_of_window_is_done_not_a_review_item():
    """Past its window is a legitimate final answer, not a failure."""
    assert Outcome(status=ReturnStatus.OUT_OF_WINDOW).task_status is TaskStatus.DONE


def test_not_ordered_is_flagged_for_a_human():
    """Spec section 5 requires skipped items be flagged, not silently dropped -
    a never-ordered link means the sheet needs a human to check it."""
    assert Outcome(status=ReturnStatus.NOT_ORDERED).task_status is TaskStatus.NEEDS_REVIEW


def test_refund_cell_is_na_when_no_amount_was_shown():
    """Refund amounts come from the platform; absence must not become 0."""
    assert Outcome(status=ReturnStatus.PLACED).refund_cell == "N/A"
    assert Outcome(status=ReturnStatus.PLACED, refund_amount=284.0).refund_cell == 284.0


def test_stamp_sets_a_timestamp_once():
    outcome = Outcome(status=ReturnStatus.PLACED).stamp()
    first = outcome.timestamp
    assert first is not None
    assert outcome.stamp().timestamp == first
