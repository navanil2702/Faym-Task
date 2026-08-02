"""Command-line entry point.

Safety default: every run is a dry run. Placing a real return is irreversible
and moves money, so it takes an explicit ``--live`` flag plus a typed
confirmation.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import signal
import sys
from dataclasses import dataclass
from pathlib import Path

from . import progress
from .browser import SessionConfig
from .humanize import Pacing
from .models import AgentAbort, Platform
from .normalize import explode_rows
from .orchestrator import Orchestrator, RunOptions, sign_in
from .workbook import (
    ReturnsWorkbook,
    WorkbookError,
    create_results_workbook,
    prepare_working_copy,
)

#: The bundled sample sheet, and the date at which its orders were still inside
#: their return windows. Every row in it is long expired at today's date, so a
#: sample run backdates by default - otherwise the agent correctly refuses all
#: 14 items and never demonstrates anything.
SAMPLE_NAME = "Faym Status Test Orders.xlsx"
SAMPLE_TODAY = dt.date(2026, 7, 6)


def _checkout_root() -> Path | None:
    """The repo checkout this package is running from, or None if installed.

    Everything that used to hang off a hardcoded ``parent.parent.parent`` now
    goes through here, so a real run against somebody's own spreadsheet does not
    quietly write into a source tree that may not exist.
    """
    root = Path(__file__).resolve().parent.parent.parent
    if (root / "data").is_dir() and (root / "src" / "faym_returns").is_dir():
        return root
    return None


def _state_root() -> Path:
    """Where the Chrome profile and screenshots go when not told otherwise."""
    return _checkout_root() or (Path.home() / ".faym-returns")


def find_sample_workbook() -> Path | None:
    """Locate the bundled sample sheet: in the checkout, else in ~/Downloads."""
    root = _checkout_root()
    candidates = []
    if root is not None:
        candidates.append(root / "data" / SAMPLE_NAME)
    candidates.append(Path.home() / "Downloads" / SAMPLE_NAME)
    return next((c for c in candidates if c.exists()), None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="faym-returns",
        description="Place e-commerce returns from an Excel task list, per line item.",
    )
    parser.add_argument(
        "workbook",
        type=Path,
        nargs="?",
        help="Source .xlsx with return tasks. Omit it only with --sample.",
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help=f"Run the agent against the bundled sample sheet ({SAMPLE_NAME}) "
        f"instead of a workbook you supply. Backdates to {SAMPLE_TODAY} so the "
        "rows are inside their windows and the flows actually run. Never live.",
    )
    parser.add_argument(
        "--sheet", default="Sheet1", help="Sheet holding the order rows (default: Sheet1)"
    )
    parser.add_argument(
        "--working-copy",
        type=Path,
        help="Where to write results (default: <name>-results.xlsx beside the "
        "source workbook). The source workbook is never modified.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Actually submit returns. Without this the agent walks each flow "
        "but never clicks the final confirm.",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="Sign in to the given --platform(s) and exit. Attempts no returns "
        "and writes no file. The profile persists, so later runs reuse the "
        "session instead of asking for another OTP.",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Build the work list from the account's own orders page instead of "
        "a spreadsheet. Takes no workbook path and needs --platform. An "
        "extension beyond the specified workflow, which is spreadsheet-driven.",
    )
    parser.add_argument(
        "--discover-days",
        type=int,
        default=30,
        help="How far back through the order history a discovery run looks "
        "(default: 30)",
    )
    parser.add_argument(
        "--discover-max-orders",
        type=int,
        default=25,
        help="Ceiling on orders taken from each site in a discovery run "
        "(default: 25)",
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
        help="Persistent Chrome profile directory (default: .profiles/default "
        "under the checkout, or ~/.faym-returns when installed)",
    )
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        help="Where screenshots go (default: runs/<timestamp> under the "
        "checkout, or ~/.faym-returns when installed)",
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


def _prompt_place_returns() -> bool:
    print()
    try:
        answer = input("  Type 'place returns' to proceed, anything else to cancel: ")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() == "place returns"


def _live_banner() -> None:
    print()
    print("=" * 78)
    print("  LIVE RUN - this will place REAL returns and move REAL money.")
    print("=" * 78)


def _confirm_live(count: int) -> bool:
    """Confirm a live run whose work list is already fixed by the sheet."""
    _live_banner()
    print(f"  Up to {count} line item(s) will have returns submitted.")
    print("  Returns are hard to reverse once filed.")
    return _prompt_place_returns()


def _confirm_live_discovery(options: RunOptions) -> bool:
    """Confirm a live run that will decide for itself what to return.

    Under ``--discover`` the exact count cannot be shown up front the way it can
    when a sheet fixes the list. The bounds that *do* hold are stated instead -
    which sites, how far back, and the ceiling on how many orders can be touched
    - because "up to N" is the honest form of the question here.
    """
    sites = ", ".join(p.value for p in options.discover_platforms) or "(none)"
    _live_banner()
    print(f"  Sites:        {sites}")
    print(f"  Looking back: {options.discover_within_days} day(s) of order history")
    print(f"  Ceiling:      {options.discover_max_orders} order(s) per site", end="")
    if options.limit is not None:
        print(f", {options.limit} line item(s) total")
    else:
        print()
    print()
    print("  The work list comes from the account itself, not from a spreadsheet.")
    print("  Every returnable item found inside those bounds will be submitted.")
    print("  Returns are hard to reverse once filed.")
    if options.limit is None:
        print()
        print("  Tip: --limit 1 does exactly one item, which is the right first run.")
    return _prompt_place_returns()


def _check_columns(book, log) -> bool:
    """Refuse to run on a sheet we cannot interpret, and say exactly why.

    Reporting "nothing pending" for a sheet whose columns are unrecognised looks
    like a clean result. Naming the missing columns turns a silent no-op into a
    fixable message.
    """
    missing = book.missing_columns
    if not missing:
        return True
    log.error("This sheet is missing required column(s): %s", ", ".join(missing))
    log.error("Columns found: %s", ", ".join(sorted(book.headers)) or "(none)")
    log.error(
        "Header spelling is matched loosely (case, spaces and punctuation are "
        "ignored, and common variants like 'OrderID' or 'Product URL' are "
        "recognised), so a name that still does not match needs renaming in the "
        "sheet - or use --sheet if the data is on another tab."
    )
    return False


def _build_session_config(args, root: Path) -> SessionConfig:
    """Browser session settings. Shared by a run and by --login, so that a
    sign-in lands in exactly the profile a later run will reuse."""
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
    return SessionConfig(
        profile_dir=(args.profile_dir or root / ".profiles" / "default").expanduser().resolve(),
        artifacts_dir=(args.artifacts_dir or root / "runs" / stamp).expanduser().resolve(),
        quiet_hours=(0, 0) if args.allow_quiet_hours else (1, 7),
        seed=args.seed,
        pacing=pacing,
    )


def _do_login(args, log) -> int:
    """Sign in once, report what happened, and exit."""
    if not args.platform:
        log.error(
            "--login needs to know which site to sign into: pass --platform "
            "Flipkart and/or --platform Amazon."
        )
        return 2

    session_config = _build_session_config(args, _state_root())
    platform_config = (
        {p.value.lower(): {"phone": args.phone} for p in Platform} if args.phone else {}
    )
    log.info("Profile: %s", session_config.profile_dir)

    try:
        results = sign_in(
            session_config,
            [Platform(p) for p in args.platform],
            platform_config,
        )
    except AgentAbort as exc:
        log.error("%s", exc)
        return 2

    print()
    for platform, already in results.items():
        state = "already signed in (restored from the profile)" if already else "signed in now"
        print(f"  {platform.value}: {state}")
    print()
    print(f"  Session saved to {session_config.profile_dir}")
    print("  Later runs reuse it - you should not be asked for another code")
    print("  unless the platform expires the session or the profile is deleted.")
    print()
    return 0


@dataclass
class RunMode:
    """Where this run's work list comes from.

    Exactly one of two things: a spreadsheet, or the account's own orders page.
    They are separate paths on purpose - see :func:`_resolve_mode`.
    """

    discover: bool
    source: Path | None = None
    """The input sheet. Always None in discovery mode: there is nothing to read."""

    sample: bool = False


def _resolve_mode(args, log) -> RunMode | None:
    """Decide where the work list comes from, or explain why the run cannot start.

    The spreadsheet is the default and the specified path: read pending tasks
    from Excel, place those returns, write the outcome back per line item.
    ``--discover`` opts out of it and builds the work list from the account's own
    orders page instead - useful when a sheet has gone stale, but an extension
    beyond the brief rather than a replacement for it, which is why it is never
    reached by default.

    Two rules regardless of path:

    The sample is never live.
        Those orders belong to somebody else.

    A missing path is never guessed.
        Omit the workbook without ``--sample`` or ``--discover`` and the run
        stops and asks, rather than reaching for the bundled test file.
    """
    if args.sample and args.workbook is not None:
        log.error(
            "--sample runs the bundled sample sheet, so it takes no workbook "
            "path. Drop the path to run the sample, or drop --sample to run %s.",
            args.workbook,
        )
        return None

    if args.discover and args.workbook is not None:
        log.error(
            "--discover builds its work list from the account's orders page, so "
            "it takes no workbook path. Drop '%s' to discover, or drop "
            "--discover to work from that sheet.",
            args.workbook,
        )
        return None

    if args.sample and args.discover:
        log.error("--sample reads the bundled sheet; --discover reads no sheet at all.")
        return None

    if args.sample:
        if args.live:
            log.error(
                "--sample and --live cannot be combined. The sample sheet holds "
                "someone else's orders; filing real returns against them is not "
                "something this agent will do."
            )
            return None
        source = find_sample_workbook()
        if source is None:
            log.error(
                "Sample workbook not found. Expected '%s' in the checkout's "
                "data/ directory or in ~/Downloads. Pass a workbook path instead.",
                SAMPLE_NAME,
            )
            return None
        log.info("Sample run against the bundled sheet: %s", source)
        return RunMode(discover=False, source=source, sample=True)

    if args.discover:
        if args.offline:
            log.error(
                "--offline has nothing to plan under --discover. The work list "
                "comes from the live orders page, which needs a browser."
            )
            return None
        if args.inspect:
            log.error(
                "--inspect prints what was parsed out of a sheet, and --discover "
                "has no sheet to parse."
            )
            return None
        if not args.platform:
            log.error(
                "--discover needs to know which site to sign into: pass "
                "--platform Flipkart and/or --platform Amazon. With no sheet "
                "there is nothing else to say where the orders live."
            )
            return None
        return RunMode(discover=True)

    if args.workbook is None:
        log.error(
            "No workbook given. Pass the path to your .xlsx, or use --sample to "
            "run the bundled sample sheet, or --discover to build the work list "
            "from the account's own orders page."
        )
        return None
    source = args.workbook.expanduser().resolve()
    if not source.exists():
        log.error("Workbook not found: %s", source)
        return None
    return RunMode(discover=False, source=source)


def _install_interrupt_handler() -> None:
    """Route Ctrl-C into a cooperative stop rather than an abrupt unwind.

    A bare KeyboardInterrupt lands wherever the agent happens to be - possibly
    between clicking Confirm and reading the return ID, which would leave a
    return filed with nothing recorded against it. Setting the stop flag instead
    makes the run wind down at the next pause, which is never mid-submission,
    and the outcomes gathered so far are still written back.

    A second Ctrl-C restores the default behaviour, so an operator is never
    trapped waiting for a wedged run.
    """

    def handler(_signum, _frame):
        if progress.stop.requested:
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            print("\n  Second interrupt - stopping immediately.\n", flush=True)
            raise KeyboardInterrupt
        progress.stop.request()
        print(
            "\n  Stopping after the current line item finishes, so a return is "
            "never left half-submitted.\n  Press Ctrl-C again to stop now.\n",
            flush=True,
        )

    signal.signal(signal.SIGINT, handler)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("faym_returns")

    # --login does not read or write a workbook, so it short-circuits before any
    # of the input-path resolution below.
    if args.login:
        return _do_login(args, log)

    mode = _resolve_mode(args, log)
    if mode is None:
        return 2
    source = mode.source

    # --inspect reads the source directly; nothing is copied or written.
    if args.inspect:
        try:
            book = ReturnsWorkbook(source, args.sheet)
        except WorkbookError as exc:
            log.error("%s", exc)
            return 2
        if not _check_columns(book, log):
            return 2
        items = explode_rows(book.pending_order_rows())
        _print_inspection(items)
        return 0

    today = args.today
    if mode.sample and today is None:
        today = SAMPLE_TODAY
        log.info(
            "Backdating the window check to %s. Every row in the sample sheet "
            "is long past its return window at today's real date, so without "
            "this the agent would refuse all 14 items and drive nothing. Pass "
            "--today to override.",
            today,
        )

    options = RunOptions(
        dry_run=not args.live,
        offline=args.offline,
        limit=args.limit,
        only_orders=tuple(args.orders),
        only_platforms=tuple(Platform(p) for p in args.platform),
        resume=not args.no_resume,
        grace_days=args.grace_days,
        today=today,
        discover=mode.discover,
        discover_platforms=tuple(Platform(p) for p in args.platform),
        discover_within_days=args.discover_days,
        discover_max_orders=args.discover_max_orders,
    )

    # Under --discover the count is not known until the browser has walked the
    # orders page, so the bounds are confirmed here instead - before anything is
    # created, so cancelling leaves no stray output file behind. A sheet-driven
    # live run knows its exact count and is confirmed further down.
    if args.live and mode.discover and not _confirm_live_discovery(options):
        log.info("Cancelled. Nothing was submitted.")
        return 1

    root = _state_root()
    # Results land beside the workbook they came from. A run against your own
    # spreadsheet has no reason to write into this repo's data/ directory, and
    # the sample run keeps its own file so it never clobbers the committed
    # outputs that the README quotes. A discovery run has no input file at all,
    # so it names a fresh timestamped one in the current directory.
    if args.working_copy is not None:
        working = args.working_copy
    elif mode.discover:
        working = Path.cwd() / f"returns-{dt.datetime.now():%Y%m%d-%H%M%S}.xlsx"
    elif mode.sample:
        working = source.parent / "sample-run-results.xlsx"
    else:
        working = source.parent / f"{source.stem}-results.xlsx"
    working = working.expanduser().resolve()

    if mode.discover:
        if not working.exists() or args.no_resume:
            create_results_workbook(working, args.sheet)
        log.info("Results workbook: %s (created for this run; no input sheet)", working)
    elif not working.exists() or args.no_resume:
        prepare_working_copy(source, working)
        log.info("Working copy: %s (source left untouched)", working)
    else:
        log.info("Reusing existing working copy: %s", working)

    try:
        book = ReturnsWorkbook(working, args.sheet)
    except WorkbookError as exc:
        log.error("%s", exc)
        return 2
    if not _check_columns(book, log):
        return 2

    session_config = _build_session_config(args, root)

    # The same number is offered to whichever platform needs a sign-in; only the
    # platforms present in the sheet will ever use it.
    platform_config = (
        {p.value.lower(): {"phone": args.phone} for p in Platform} if args.phone else {}
    )
    orchestrator = Orchestrator(book, session_config, options, platform_config)

    if args.live and not mode.discover and not args.offline:
        to_attempt, _ = orchestrator.plan()
        if not to_attempt:
            log.info("No eligible line items to submit. Nothing to do.")
            return 0
        if not _confirm_live(len(to_attempt)):
            log.info("Cancelled. Nothing was submitted.")
            return 1

    progress.reset()
    _install_interrupt_handler()
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
    if args.sample:
        print(
            "This was a SAMPLE run against the bundled sheet, backdated to "
            f"{today}. Point the agent at your own workbook for a real run."
        )
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
