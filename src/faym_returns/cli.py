"""Command-line entry point.

Safety default: every run is a dry run. Placing a real return is irreversible
and moves money, so it takes an explicit ``--live`` flag plus a typed
confirmation.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

from .browser import SessionConfig
from .humanize import Pacing
from .models import Platform
from .normalize import explode_rows
from .orchestrator import Orchestrator, RunOptions
from .workbook import ReturnsWorkbook, prepare_working_copy

DEFAULT_ROOT = Path(__file__).resolve().parent.parent.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="faym-returns",
        description="Place e-commerce returns from an Excel task list, per line item.",
    )
    parser.add_argument("workbook", type=Path, help="Source .xlsx with return tasks")
    parser.add_argument(
        "--sheet", default="Sheet1", help="Sheet holding the order rows (default: Sheet1)"
    )
    parser.add_argument(
        "--working-copy",
        type=Path,
        help="Where to write results (default: <root>/data/<name>-results.xlsx). "
        "The source workbook is never modified.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Actually submit returns. Without this the agent walks each flow but "
        "never clicks the final confirm.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Plan only: parse and classify without launching a browser.",
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="Print the parsed line items and exit without writing anything.",
    )
    parser.add_argument("--limit", type=int, help="Max line items to attempt this run")
    parser.add_argument(
        "--order",
        action="append",
        default=[],
        dest="orders",
        help="Restrict to this order id (repeatable)",
    )
    parser.add_argument(
        "--platform",
        action="append",
        default=[],
        choices=[p.value for p in Platform],
        help="Restrict to this platform (repeatable)",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Re-attempt line items that already hold a final recorded status",
    )
    parser.add_argument(
        "--profile-dir",
        type=Path,
        help="Persistent Chrome profile directory (default: <root>/.profiles/default)",
    )
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        help="Where screenshots go (default: <root>/runs/<timestamp>)",
    )
    parser.add_argument("--seed", type=int, help="Seed the human-timing RNG for reproducibility")
    parser.add_argument(
        "--grace-days",
        type=int,
        default=1,
        help="Days of slack before declaring an item out of window (default: 1)",
    )
    parser.add_argument(
        "--phone",
        help="Mobile number for OTP sign-in. Given this, the agent fills the "
        "login form and requests the code itself, then asks you for the code. "
        "Omit it to sign in entirely by hand.",
    )
    parser.add_argument(
        "--today",
        type=lambda s: dt.date.fromisoformat(s),
        help="Override today's date (YYYY-MM-DD) for the window check. For "
        "validating the pipeline against historical rows.",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Compress the human-pacing delays. For debugging against a staging "
        "page only - raises detection risk on live sites.",
    )
    parser.add_argument(
        "--allow-quiet-hours",
        action="store_true",
        help="Permit a run during configured quiet hours",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser


def _confirm_live(count: int) -> bool:
    print()
    print("=" * 78)
    print("  LIVE RUN - this will place REAL returns and move REAL money.")
    print("=" * 78)
    print(f"  Up to {count} line item(s) will have returns submitted.")
    print("  Returns are hard to reverse once filed.")
    print()
    try:
        answer = input("  Type 'place returns' to proceed, anything else to cancel: ")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() == "place returns"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("faym_returns")

    source = args.workbook.expanduser().resolve()
    if not source.exists():
        log.error("Workbook not found: %s", source)
        return 2

    # --inspect reads the source directly; nothing is copied or written.
    if args.inspect:
        book = ReturnsWorkbook(source, args.sheet)
        items = explode_rows(book.pending_order_rows())
        _print_inspection(items)
        return 0

    root = DEFAULT_ROOT
    working = args.working_copy or (root / "data" / f"{source.stem}-results.xlsx")
    working = working.expanduser().resolve()
    if not working.exists() or args.no_resume:
        prepare_working_copy(source, working)
        log.info("Working copy: %s (source left untouched)", working)
    else:
        log.info("Reusing existing working copy: %s", working)

    book = ReturnsWorkbook(working, args.sheet)

    options = RunOptions(
        dry_run=not args.live,
        offline=args.offline,
        limit=args.limit,
        only_orders=tuple(args.orders),
        only_platforms=tuple(Platform(p) for p in args.platform),
        resume=not args.no_resume,
        grace_days=args.grace_days,
        today=args.today,
    )

    pacing = Pacing()
    if args.fast:
        pacing = Pacing(
            think_min=0.15,
            think_max=0.5,
            key_min=0.01,
            key_max=0.04,
            between_items_min=1.0,
            between_items_max=3.0,
            between_orders_min=1.5,
            between_orders_max=4.0,
            long_break_every=0,
            long_break_min=0.0,
            long_break_max=0.0,
        )

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    session_config = SessionConfig(
        profile_dir=(args.profile_dir or root / ".profiles" / "default").expanduser().resolve(),
        artifacts_dir=(args.artifacts_dir or root / "runs" / stamp).expanduser().resolve(),
        quiet_hours=(0, 0) if args.allow_quiet_hours else (1, 7),
        seed=args.seed,
        pacing=pacing,
    )

    # The same number is offered to whichever platform needs a sign-in; only the
    # platforms present in the sheet will ever use it.
    platform_config = (
        {p.value.lower(): {"phone": args.phone} for p in Platform} if args.phone else {}
    )
    orchestrator = Orchestrator(book, session_config, options, platform_config)

    if args.live and not args.offline:
        to_attempt, _ = orchestrator.plan()
        if not to_attempt:
            log.info("No eligible line items to submit. Nothing to do.")
            return 0
        if not _confirm_live(len(to_attempt)):
            log.info("Cancelled. Nothing was submitted.")
            return 1

    report = orchestrator.run()

    print()
    print("-" * 78)
    print(report.summary())
    print("-" * 78)
    print(f"Results written to: {working}")
    if report.needs_review:
        print("\nNeeds human review:")
        for item, outcome in report.needs_review:
            print(f"  - {item.label}: {outcome.status.value} - {outcome.log[:110]}")
    if options.dry_run and not options.offline:
        print("\nThis was a DRY RUN. No returns were submitted. Re-run with --live.")
    print()
    return 0


def _print_inspection(items) -> None:
    by_order: dict[str, list] = {}
    for item in items:
        by_order.setdefault(item.order_id, []).append(item)

    total_ordered = sum(1 for i in items if i.ordered)
    print(f"\n{len(by_order)} pending order row(s) -> {len(items)} line item(s) "
          f"({total_ordered} actually ordered)\n")

    for order_id, group in by_order.items():
        head = group[0]
        platform = head.platform.value if head.platform else "UNKNOWN"
        print(f"{order_id}  [{platform}]  declared qty={head.qty_declared}  "
              f"window={head.return_window_days or '?'}d  "
              f"delivered={head.delivery_date or '?'}"
              f"{' (approx)' if head.delivery_date_is_approx else ''}")
        for item in group:
            mark = "  ordered " if item.ordered else "  NA-skip "
            print(f"{mark} {item.sku:<18} {item.title_hint[:58]}")
        for note in head.parse_notes:
            print(f"    ! {note}")
        print()


if __name__ == "__main__":
    sys.exit(main())
