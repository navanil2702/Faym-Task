"""Progress bus and stop-signal tests."""

from __future__ import annotations

import queue

import pytest

from faym_returns import progress
from faym_returns.models import AgentAbort


@pytest.fixture(autouse=True)
def clean_bus():
    progress.bus.reset()
    progress.stop.clear()
    yield
    progress.bus.reset()
    progress.stop.clear()


def test_publish_reaches_every_listener():
    a = progress.bus.subscribe()
    b = progress.bus.subscribe()
    progress.publish("item_started", sku="ABC")
    assert a.get_nowait().data["sku"] == "ABC"
    assert b.get_nowait().data["sku"] == "ABC"


def test_publish_with_no_listeners_is_harmless():
    progress.publish("item_started", sku="ABC")  # must not raise


def test_late_listener_replays_history():
    """A browser that connects mid-run must not see a blank run."""
    progress.publish("run_started", live=False)
    progress.publish("item_started", sku="ABC")
    listener = progress.bus.subscribe()
    kinds = [listener.get_nowait().kind for _ in range(2)]
    assert kinds == ["run_started", "item_started"]


def test_replay_from_skips_already_seen_events():
    progress.publish("one")
    second = progress.bus.publish("two")
    listener = progress.bus.subscribe(replay_from=second.seq)
    with pytest.raises(queue.Empty):
        listener.get_nowait()


def test_reset_clears_history_but_keeps_sequence_monotonic():
    """Rewinding the counter would make a reconnecting browser skip the next
    run's opening events, because it asks for everything after its last seq."""
    progress.publish("one")
    high = progress.bus.publish("two").seq
    progress.bus.reset()
    assert progress.bus.publish("three").seq > high
    assert len(progress.bus.history()) == 1


def test_unsubscribed_listener_stops_receiving():
    listener = progress.bus.subscribe()
    progress.bus.unsubscribe(listener)
    progress.publish("item_started")
    with pytest.raises(queue.Empty):
        listener.get_nowait()


# ------------------------------------------------------------------ stop signal


def test_check_stop_is_quiet_until_requested():
    progress.check_stop()  # must not raise


def test_check_stop_raises_once_requested():
    progress.stop.request()
    with pytest.raises(AgentAbort, match="Stopped by the operator"):
        progress.check_stop()


def test_stop_can_be_cleared_for_the_next_run():
    progress.stop.request()
    progress.stop.clear()
    progress.check_stop()  # must not raise


def test_reset_clears_a_pending_stop():
    progress.stop.request()
    progress.reset()
    assert progress.stop.requested is False


# -------------------------------------------------------------------- describe


def test_describe_renders_known_events():
    event = progress.bus.publish("item_finished", title="pink bag", status="Placed")
    assert progress.describe(event) == "pink bag: Placed."


def test_describe_returns_none_for_events_with_no_prose():
    event = progress.bus.publish("wait_tick", remaining=12)
    assert progress.describe(event) is None
