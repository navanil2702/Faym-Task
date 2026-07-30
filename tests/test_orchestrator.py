"""Planning tests: what gets attempted, what is settled without a browser."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from faym_returns.browser import SessionConfig
from faym_returns.orchestrator import Orchestrator, RunOptions
from faym_returns.models import Platform, ReturnStatus
from faym_returns.workbook import ReturnsWorkbook, prepare_working_copy

SOURCE = Path.home() / "Downloads" / "Faym Status Test Orders.xlsx"

pytestmark = pytest.mark.skipif(
    not SOURCE.exists(), reason="test dataset not present in ~/Downloads"
)


def _orchestrator(tmp_path: Path, **option_overrides) -> Orchestrator:
    working = tmp_path / "results.xlsx"
    prepare_working_copy(SOURCE, working)
    book = ReturnsWorkbook(working)
    options = RunOptions(**{"dry_run": True, "offline": True, **option_overrides})
    config = SessionConfig(
        profile_dir=tmp_path / "profile", artifacts_dir=tmp_path / "runs"
    )
    return Orchestrator(book, config, options)


def test_plan_separates_attemptable_from_already_decided(tmp_path: Path):
    orch = _orchestrator(tmp_path, today=dt.date(2026, 7, 6))
    to_attempt, decided = orch.plan()

    assert len(to_attempt) + len(decided) == 16
    # The two NA-marked links are settled without a browser.
    na = [o for _, o in decided if o.status is ReturnStatus.NOT_ORDERED]
    assert len(na) == 2
    # The 7-day-window row has expired by this date; the 10-day rows have not.
    expired = [o for _, o in decided if o.status is ReturnStatus.OUT_OF_WINDOW]
    assert len(expired) == 1
    assert len(to_attempt) == 13


def test_na_items_are_never_attempted(tmp_path: Path):
    orch = _orchestrator(tmp_path, today=dt.date(2026, 7, 6))
    to_attempt, _ = orch.plan()
    assert all(item.ordered for item in to_attempt)


def test_everything_is_out_of_window_today(tmp_path: Path):
    """As of the real current date the whole dataset has expired."""
    orch = _orchestrator(tmp_path, today=dt.date(2026, 7, 30))
    to_attempt, decided = orch.plan()
    assert to_attempt == []
    assert all(
        o.status in {ReturnStatus.OUT_OF_WINDOW, ReturnStatus.NOT_ORDERED}
        for _, o in decided
    )


def test_partial_success_within_one_order(tmp_path: Path):
    """Order ...997100 has 5 links: 1 NA and 4 ordered. The NA one is skipped
    and the other four are still attempted - one item never blocks the rest."""
    orch = _orchestrator(tmp_path, today=dt.date(2026, 7, 6))
    to_attempt, decided = orch.plan()

    order = "OD337974610559997100"
    attempted = [i for i in to_attempt if i.order_id == order]
    skipped = [(i, o) for i, o in decided if i.order_id == order]

    assert len(attempted) == 4
    assert len(skipped) == 1
    assert skipped[0][1].status is ReturnStatus.NOT_ORDERED


def test_order_filter(tmp_path: Path):
    orch = _orchestrator(
        tmp_path, today=dt.date(2026, 7, 6), only_orders=("OD337983106511516100",)
    )
    to_attempt, decided = orch.plan()
    everything = to_attempt + [i for i, _ in decided]
    assert {i.order_id for i in everything} == {"OD337983106511516100"}


def test_platform_filter_excludes_non_matching(tmp_path: Path):
    orch = _orchestrator(
        tmp_path, today=dt.date(2026, 7, 6), only_platforms=(Platform.AMAZON,)
    )
    to_attempt, decided = orch.plan()
    assert to_attempt == [] and decided == [], "the dataset is entirely Flipkart"


def test_limit_caps_attempts(tmp_path: Path):
    orch = _orchestrator(tmp_path, today=dt.date(2026, 7, 6), limit=3)
    to_attempt, _ = orch.plan()
    assert len(to_attempt) == 3


def test_offline_run_records_planned_and_leaves_rows_pending(tmp_path: Path):
    orch = _orchestrator(tmp_path, today=dt.date(2026, 7, 6))
    report = orch.run()
    assert len(report.results) == 16
    assert report.counts[ReturnStatus.PLANNED.value] == 13
    # Planned items are not review items - nothing was attempted.
    assert report.needs_review == []


def test_resume_skips_items_with_a_final_status(tmp_path: Path):
    orch = _orchestrator(tmp_path, today=dt.date(2026, 7, 6))
    orch.run()

    # Second pass over the same working copy: the settled NA and out-of-window
    # items must not be reconsidered, while Planned items still are.
    book = ReturnsWorkbook(orch.workbook.path)
    resumed = Orchestrator(
        book,
        orch.session_config,
        RunOptions(dry_run=True, offline=True, resume=True, today=dt.date(2026, 7, 6)),
    )
    to_attempt, decided = resumed.plan()
    assert len(decided) == 0, "final states should not be re-decided"
    assert len(to_attempt) == 13, "Planned items are not final, so they requeue"
