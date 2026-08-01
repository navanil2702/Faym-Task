"""The run loop: read pending tasks, place returns, write outcomes back.

Partial success is the governing rule. Every line item is processed and recorded
independently, so an order with one item past its window still gets returns filed
for the items that are eligible. An order row is only marked Done once every one
of its line items holds a final recorded state; anything unresolved is flagged
for a human rather than dropped.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from . import eligibility, progress
from .browser import Session, SessionConfig
from .otp import OtpProvider, console_otp
from .models import (
    AgentAbort,
    LineItem,
    Outcome,
    Platform,
    ReturnStatus,
    TaskStatus,
)
from .normalize import explode_rows
from .platforms import adapter_for
from .workbook import ReturnsWorkbook

log = logging.getLogger(__name__)


@dataclass
class RunOptions:
    dry_run: bool = True
    """When True the agent walks every flow but never clicks the final confirm."""

    offline: bool = False
    """Plan only: no browser is launched. Used for validating the pipeline."""

    limit: Optional[int] = None
    """Maximum line items to attempt this run."""

    only_orders: Sequence[str] = field(default_factory=tuple)
    only_platforms: Sequence[Platform] = field(default_factory=tuple)
    resume: bool = True
    """Skip line items that already hold a final status from a previous run."""

    grace_days: int = 1
    today: Optional[dt.date] = None


@dataclass
class RunReport:
    planned: list[LineItem] = field(default_factory=list)
    results: list[tuple[LineItem, Outcome]] = field(default_factory=list)
    aborted: Optional[str] = None

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for _, outcome in self.results:
            out[outcome.status.value] = out.get(outcome.status.value, 0) + 1
        return out

    @property
    def needs_review(self) -> list[tuple[LineItem, Outcome]]:
        return [
            (i, o) for i, o in self.results if o.task_status is TaskStatus.NEEDS_REVIEW
        ]

    def summary(self) -> str:
        lines = [
            f"Line items planned:   {len(self.planned)}",
            f"Line items processed: {len(self.results)}",
        ]
        for status, count in sorted(self.counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {status:<32} {count}")
        if self.needs_review:
            lines.append(f"Flagged for human review: {len(self.needs_review)}")
        if self.aborted:
            lines.append(f"Run aborted early: {self.aborted}")
        return "\n".join(lines)


class Orchestrator:
    def __init__(
        self,
        workbook: ReturnsWorkbook,
        session_config: SessionConfig,
        options: RunOptions,
        platform_config: Optional[dict] = None,
        otp_provider: Optional[OtpProvider] = None,
    ):
        self.workbook = workbook
        self.session_config = session_config
        self.options = options
        self.platform_config = platform_config or {}
        #: How to obtain a one-time code during sign-in. Without one, the agent
        #: falls back to letting the operator sign in by hand.
        self.otp_provider = otp_provider or console_otp

    # -------------------------------------------------------------- planning

    def plan(self) -> tuple[list[LineItem], list[tuple[LineItem, Outcome]]]:
        """Decide what to attempt, and pre-resolve what needs no browser.

        Returns ``(to_attempt, decided)``. ``decided`` holds items settled
        without touching a browser - never-ordered NA links and items clearly
        past their window - so they still get written back with a final state.
        """
        rows = self.workbook.pending_order_rows()
        items = explode_rows(rows)

        if self.options.only_orders:
            wanted = {o.strip() for o in self.options.only_orders}
            items = [i for i in items if i.order_id in wanted]
        if self.options.only_platforms:
            allowed = set(self.options.only_platforms)
            items = [i for i in items if i.platform in allowed]

        already: dict[tuple[str, str], str] = {}
        if self.options.resume:
            from .workbook import existing_outcomes

            already = existing_outcomes(self.workbook.path)

        to_attempt: list[LineItem] = []
        decided: list[tuple[LineItem, Outcome]] = []

        for item in items:
            prior = already.get((item.order_id, item.sku))
            if prior and _is_settled(prior):
                log.info("Skipping %s - already recorded as %s", item.label, prior)
                continue

            if not item.ordered:
                decided.append(
                    (
                        item,
                        Outcome(
                            status=ReturnStatus.NOT_ORDERED,
                            log=(
                                "This product link was marked 'NA' in the source "
                                "sheet, meaning the item was never ordered. No "
                                "return attempted."
                            ),
                            dry_run=self.options.dry_run,
                        ).stamp(),
                    )
                )
                continue

            if item.platform is None:
                decided.append(
                    (
                        item,
                        Outcome(
                            status=ReturnStatus.SUPPORT_NEEDED,
                            log=(
                                "The Platform cell is empty or unrecognised, so the "
                                "agent does not know which site to drive."
                            ),
                            dry_run=self.options.dry_run,
                        ).stamp(),
                    )
                )
                continue

            if not item.sku:
                decided.append(
                    (
                        item,
                        Outcome(
                            status=ReturnStatus.SUPPORT_NEEDED,
                            log=(
                                "No product id could be extracted from the Product "
                                "Link cell, so the line item cannot be matched on "
                                "the platform. Needs a usable product URL."
                            ),
                            dry_run=self.options.dry_run,
                        ).stamp(),
                    )
                )
                continue

            verdict = eligibility.check(
                item, today=self.options.today, grace_days=self.options.grace_days
            )
            if not verdict.eligible:
                decided.append(
                    (
                        item,
                        Outcome(
                            status=ReturnStatus.OUT_OF_WINDOW,
                            log=f"Skipped: {verdict.reason}",
                            dry_run=self.options.dry_run,
                        ).stamp(),
                    )
                )
                continue

            to_attempt.append(item)

        if self.options.limit is not None:
            to_attempt = to_attempt[: self.options.limit]
        return to_attempt, decided

    # ------------------------------------------------------------------- run

    def run(self) -> RunReport:
        to_attempt, decided = self.plan()
        report = RunReport(planned=to_attempt + [i for i, _ in decided])
        report.results.extend(decided)

        if not to_attempt:
            log.info("Nothing needs a browser this run.")
            self._persist(report)
            return report

        if self.options.offline:
            for item in to_attempt:
                outcome = Outcome(
                    status=ReturnStatus.PLANNED,
                    log=(
                        "OFFLINE PLAN ONLY: this line item is eligible and "
                        "would be attempted in a real run. No browser was "
                        "launched."
                    ),
                    dry_run=True,
                ).stamp()
                report.results.append((item, outcome))
            self._persist(report)
            return report

        # Group by platform, then by order, so one session handles one platform
        # and a multi-item order is covered in a single visit to that order.
        grouped: dict[Platform, dict[str, list[LineItem]]] = {}
        for item in to_attempt:
            assert item.platform is not None
            grouped.setdefault(item.platform, {}).setdefault(item.order_id, []).append(item)

        processed = 0
        try:
            with Session(self.session_config) as session:
                for platform, orders in grouped.items():
                    adapter = adapter_for(platform)(
                        session,
                        self.platform_config.get(platform.value.lower(), {}),
                        otp_provider=self.otp_provider,
                    )
                    # A fresh tab for this platform, then sign in on it; each
                    # subsequent record gets its own tab below.
                    session.new_page(close_previous=True)
                    adapter.ensure_logged_in()

                    for order_index, (order_id, items) in enumerate(orders.items()):
                        if processed >= self.session_config.pacing.max_items_per_session:
                            report.aborted = (
                                "Reached the per-session cap of "
                                f"{self.session_config.pacing.max_items_per_session} "
                                "line items. Remaining items stay Pending; resume later."
                            )
                            raise _SessionCapReached

                        if order_index:
                            session.human.between_orders()

                        # Spec workflow step 2: a new tab per record. The login
                        # tab is reused for the first record so the sign-in the
                        # adapter just completed is not thrown away.
                        if order_index:
                            session.new_page(close_previous=True)

                        log.info(
                            "Processing order %s (%d line item(s)) on %s",
                            order_id,
                            len(items),
                            platform.value,
                        )
                        outcomes = adapter.process_order(items, dry_run=self.options.dry_run)

                        for item in items:
                            outcome = outcomes.get(item.sku)
                            if outcome is None:
                                outcome = Outcome(
                                    status=ReturnStatus.FAILED,
                                    log=(
                                        "The adapter returned no result for this line "
                                        "item. Nothing was submitted; needs a human."
                                    ),
                                    dry_run=self.options.dry_run,
                                ).stamp()
                            report.results.append((item, outcome))
                            processed += 1
        except _SessionCapReached:
            pass
        except AgentAbort as exc:
            report.aborted = str(exc)
            log.error("Session aborted: %s", exc)
        except Exception as exc:  # noqa: BLE001 - never lose recorded work
            report.aborted = f"Unexpected session failure: {exc}"
            log.exception("Session failed")

        # Anything planned but never reached stays Pending, deliberately: it is
        # safer to re-attempt later than to record a state we did not observe.
        self._persist(report)
        return report


    def _persist(self, report: RunReport) -> None:
        if not report.results:
            return
        stats = self.workbook.write_results(
            report.results, dry_run_marker=self.options.dry_run
        )
        log.info(
            "Wrote %d line item(s) across %d order row(s) to %s",
            stats["line_items_written"],
            stats["order_rows_touched"],
            self.workbook.path,
        )


#: Recorded statuses that mean "attempt this again on the next run".
_RETRYABLE = {s.value for s in ReturnStatus if not s.is_final} | {TaskStatus.PENDING.value}


def _is_settled(recorded: str) -> bool:
    """Whether a previously recorded status means we should not re-attempt.

    Anything unrecognised counts as settled. Operators edit these cells by hand,
    and re-attempting an item whose recorded state we cannot interpret risks
    filing a duplicate return - the more expensive mistake of the two.
    """
    return recorded.strip() not in _RETRYABLE


class _SessionCapReached(Exception):
    """Internal control-flow signal for the per-session throughput cap."""
