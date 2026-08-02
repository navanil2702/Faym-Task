"""Tests for the guards that stop the agent doing something it shouldn't.

These need no browser: they exercise the refusals and the planning decisions.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from faym_returns.browser import CHALLENGE_MARKERS, Session, SessionConfig
from faym_returns.models import AgentAbort, Outcome, ReturnStatus, TaskStatus
from faym_returns.orchestrator import _is_settled
from faym_returns.platforms.amazon import AmazonAdapter
from faym_returns.platforms.flipkart import FlipkartAdapter


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


# ------------------------------------------------- a rehearsal is not a return


@pytest.mark.parametrize("adapter_cls", [FlipkartAdapter, AmazonAdapter])
def test_a_dry_run_never_records_a_return_as_placed(adapter_cls):
    """Two things go wrong if it does, and the second one is the expensive one.

    The spec's ``Return status`` column would carry ``Placed`` for a return that
    was never submitted. And ``Placed`` is a *final* status, so resume would skip
    the item - meaning the documented first-run procedure (rehearse with
    --limit 1, then --live) would place nothing at all, having marked everything
    Done on the rehearsal.
    """
    adapter = adapter_cls(session=None)
    outcome = adapter.dry_run_outcome(reached_confirm=True, detail="Reason: x.")

    assert outcome.status is not ReturnStatus.PLACED
    assert outcome.status is ReturnStatus.PLANNED
    assert outcome.dry_run is True
    # Nothing was attempted, so no claim goes in the spec column at all.
    assert outcome.spec_status == ""
    assert outcome.task_status is TaskStatus.PENDING


@pytest.mark.parametrize("adapter_cls", [FlipkartAdapter, AmazonAdapter])
def test_a_dry_run_outcome_is_always_re_attemptable(adapter_cls):
    """Whether the walk succeeded or not, --live must still pick the item up."""
    adapter = adapter_cls(session=None)
    for reached in (True, False):
        outcome = adapter.dry_run_outcome(reached_confirm=reached, detail="")
        assert outcome.status.is_final is False, reached
        assert not _is_settled(outcome.status.value), reached


@pytest.mark.parametrize("adapter_cls", [FlipkartAdapter, AmazonAdapter])
def test_a_flow_that_never_reached_confirm_is_flagged(adapter_cls):
    """A rehearsal that could not find the submit button is a broken flow."""
    outcome = adapter_cls(session=None).dry_run_outcome(reached_confirm=False, detail="")

    assert outcome.status is ReturnStatus.FAILED
    assert outcome.task_status is TaskStatus.NEEDS_REVIEW


# --------------------------------------- an intermediate step is not the submit


class FakeControl:
    """The slice of a Locator that click_intermediate touches."""

    def __init__(self, label):
        self.label = label
        self.clicked = False

    def inner_text(self, timeout=None):
        return self.label


class RecordingHuman:
    def __init__(self):
        self.clicked = []

    def click(self, locator, **kwargs):
        locator.clicked = True
        self.clicked.append(locator.label)


def _adapter_with_human(adapter_cls):
    adapter = adapter_cls(session=None)
    human = RecordingHuman()
    # `human` reads through the session, which these tests do not have.
    type(adapter).human = property(lambda self, h=human: h)
    return adapter, human


@pytest.mark.parametrize(
    "label",
    [
        "Confirm Return",
        "Confirm return request",
        "Submit request",
        "Place return",
        "Confirm your return",
        "Confirm",
    ],
)
def test_an_intermediate_step_refuses_the_final_submit(label):
    """The failure this prevents: a dry run filing a real return.

    `pickup_address_confirm` used to fall back to `button:has-text('Confirm')`,
    and has-text is a substring match - so it also matched "Confirm Return". That
    click happens before the dry-run guard, so a rehearsal would have submitted.
    """
    adapter, human = _adapter_with_human(FlipkartAdapter)
    control = FakeControl(label)

    assert adapter.click_intermediate(control, what="pickup address") is False
    assert control.clicked is False
    assert human.clicked == []


@pytest.mark.parametrize(
    "label",
    ["Confirm address", "Use this address", "Deliver to this address", "Schedule pickup", ""],
)
def test_a_genuine_intermediate_control_is_clicked(label):
    adapter, human = _adapter_with_human(FlipkartAdapter)
    control = FakeControl(label)

    assert adapter.click_intermediate(control, what="pickup address") is True
    assert control.clicked is True


def test_no_intermediate_selector_can_match_the_submit_button():
    """Belt and braces: no candidate list for a mid-wizard step may be loose
    enough to reach the final submit, independently of the runtime guard."""
    for adapter_cls in (FlipkartAdapter, AmazonAdapter):
        actions = adapter_cls(session=None).sel.get("actions", {})
        for key in ("pickup_address_confirm", "pickup_option", "pickup_slot", "refund_mode"):
            for candidate in actions.get(key, []):
                assert "has-text('Confirm')" not in candidate, f"{adapter_cls.__name__}.{key}"
                assert 'has-text("Confirm")' not in candidate, f"{adapter_cls.__name__}.{key}"
