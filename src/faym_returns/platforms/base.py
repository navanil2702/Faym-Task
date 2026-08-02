"""Platform adapter contract and shared selector plumbing."""

from __future__ import annotations

import datetime as dt
import logging
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterable, Optional, Sequence

import yaml
from playwright.sync_api import Locator, Page

from .. import progress
from ..browser import Session
from ..models import AgentAbort, LineItem, Outcome, Platform, ReturnFlow, ReturnStatus
from ..normalize import is_product_url, parse_date, sku_of, title_hint_of
from ..otp import DEFAULT_TIMEOUT_S, OtpProvider

log = logging.getLogger(__name__)

SELECTOR_DIR = Path(__file__).resolve().parent.parent / "selectors"

_ROLE_RE = re.compile(r"^role:(?P<role>[a-z]+)(?:\[name=(?P<name>.+)\])?$", re.I)
_REGEX_RE = re.compile(r"^/(?P<body>.*)/(?P<flags>[a-z]*)$")

#: Labels that mark the final, irreversible submit on either platform. No
#: intermediate wizard step is ever allowed to click a control matching this,
#: however loose the selector that found it. See ``click_intermediate``.
SUBMIT_LABEL_RE = re.compile(
    r"(confirm|submit|place|create)\s+(your\s+)?(return|request|refund)"
    r"|return\s+request"
    r"|^\s*(confirm|submit)\s*$",
    re.I,
)


def load_selectors(name: str) -> dict:
    path = SELECTOR_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"No selector file for platform {name!r} at {path}")
    with path.open() as handle:
        return yaml.safe_load(handle) or {}


def _build(page: Page, spec: str) -> Locator:
    """Turn one selector string from the YAML into a Locator.

    Supported forms, in rough order of preference for stability:
      ``role:button[name=/return/i]``  accessibility role + accessible name
      ``text:Return``                  visible text
      ``label:Reason``                 form label
      ``placeholder:Enter OTP``        input placeholder
      ``css:.foo > .bar``              raw CSS
      ``xpath://div[@id='x']``         raw XPath
    A bare string is treated as CSS.
    """
    match = _ROLE_RE.match(spec)
    if match:
        role = match.group("role").lower()
        raw_name = match.group("name")
        if raw_name is None:
            return page.get_by_role(role)  # type: ignore[arg-type]
        rx = _REGEX_RE.match(raw_name.strip())
        if rx:
            flags = re.I if "i" in rx.group("flags") else 0
            return page.get_by_role(role, name=re.compile(rx.group("body"), flags))  # type: ignore[arg-type]
        return page.get_by_role(role, name=raw_name.strip())  # type: ignore[arg-type]

    for prefix, builder in (
        ("text:", lambda v: page.get_by_text(_maybe_regex(v))),
        ("label:", lambda v: page.get_by_label(_maybe_regex(v))),
        ("placeholder:", lambda v: page.get_by_placeholder(_maybe_regex(v))),
        ("css:", page.locator),
        ("xpath:", lambda v: page.locator(f"xpath={v}")),
    ):
        if spec.startswith(prefix):
            return builder(spec[len(prefix):])  # type: ignore[operator]

    return page.locator(spec)


def _maybe_regex(value: str):
    rx = _REGEX_RE.match(value.strip())
    if rx:
        return re.compile(rx.group("body"), re.I if "i" in rx.group("flags") else 0)
    return value


def find(
    page: Page,
    candidates: Sequence[str],
    *,
    timeout: int = 6000,
    within: Optional[Locator] = None,
) -> Optional[Locator]:
    """First visible match among ordered selector candidates, else None.

    Selectors are kept as candidate lists in YAML precisely because retail DOM
    churns; when a class hash changes, the role/text candidates still hit, and
    fixing the CSS fallback is a config edit rather than a code change.
    """
    for spec in candidates or []:
        try:
            locator = _build(page, spec) if within is None else within.locator(spec)
            first = locator.first
            first.wait_for(state="visible", timeout=timeout)
            if first.count() > 0:
                return first
        except Exception:  # noqa: BLE001 - candidate simply did not match
            continue
    return None


def find_all(page: Page, candidates: Sequence[str], *, timeout: int = 6000) -> Optional[Locator]:
    """Locator covering every match for the first candidate that matches."""
    for spec in candidates or []:
        try:
            locator = _build(page, spec)
            locator.first.wait_for(state="visible", timeout=timeout)
            if locator.count() > 0:
                return locator
        except Exception:  # noqa: BLE001
            continue
    return None


def text_of(locator: Optional[Locator], limit: int = 4000) -> str:
    if locator is None:
        return ""
    try:
        return (locator.inner_text(timeout=5000) or "")[:limit]
    except Exception:  # noqa: BLE001
        return ""


def first_group(patterns: Iterable[str], haystack: str) -> str:
    for pattern in patterns:
        match = re.search(pattern, haystack, re.I)
        if match:
            return (match.group(1) if match.groups() else match.group(0)).strip()
    return ""


def parse_amount(text: str) -> Optional[float]:
    """Pull a rupee amount out of platform copy like '₹1,234' or 'Rs. 953.00'."""
    match = re.search(r"(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d{1,2})?)", text, re.I)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


class PlatformAdapter(ABC):
    """One e-commerce platform's return flow.

    ``process_order`` returns an Outcome per SKU rather than per order. That
    signature is what lets a batch platform cover several items in one flow and
    a sequential platform repeat a micro-flow per item, while the orchestrator
    and the workbook stay strictly line-item oriented either way.
    """

    platform: Platform
    selector_key: str

    def __init__(
        self,
        session: Session,
        config: dict | None = None,
        otp_provider: Optional[OtpProvider] = None,
    ):
        self.session = session
        self.config = config or {}
        self.sel = load_selectors(self.selector_key)
        self.otp_provider = otp_provider

    @property
    def page(self) -> Page:
        assert self.session.page is not None
        return self.session.page

    @property
    def human(self):
        assert self.session.human is not None
        return self.session.human

    def s(self, *path: str) -> list[str]:
        """Fetch a selector candidate list by dotted path from the YAML."""
        node: object = self.sel
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return []
            node = node[key]
        if isinstance(node, str):
            return [node]
        return list(node) if isinstance(node, list) else []

    # ------------------------------------------------------------------ contract

    @abstractmethod
    def is_logged_in(self) -> bool:
        ...

    @abstractmethod
    def ensure_logged_in(self) -> None:
        ...

    @abstractmethod
    def detect_flow(self, items: Sequence[LineItem]) -> ReturnFlow:
        ...

    @abstractmethod
    def process_order(
        self,
        items: Sequence[LineItem],
        *,
        dry_run: bool = True,
    ) -> dict[str, Outcome]:
        ...

    # --------------------------------------------------------- wizard clicking

    def click_intermediate(self, locator: Locator, *, what: str) -> bool:
        """Click a mid-wizard control, refusing anything that looks like the submit.

        Selectors are candidate lists that end in deliberately loose CSS, and
        loose CSS overlaps. Playwright's ``has-text`` is a *substring* match, so
        a fallback like ``button:has-text('Confirm')`` on the pickup-address step
        also matches "Confirm Return" - the final, irreversible submit. An
        intermediate step that reached it would file a real return in the middle
        of a dry run, which is the one thing a dry run exists to make impossible.

        So the control's own label is checked before the click, whatever selector
        found it. This holds in live runs too: reaching the submit from the
        pickup step would submit before the remaining steps had been filled in.

        Returns whether the click happened.
        """
        label = ""
        try:
            label = (locator.inner_text(timeout=2000) or "").strip()
        except Exception:  # noqa: BLE001 - an unreadable label is not a submit
            label = ""

        if label and SUBMIT_LABEL_RE.search(label):
            log.warning(
                "Refusing to click %r for the %s step: that label is the final "
                "submit, not an intermediate control. Skipping the step rather "
                "than risking an unintended return.",
                label[:60],
                what,
            )
            return False

        try:
            self.human.click(locator)
            return True
        except Exception:  # noqa: BLE001 - already selected, or not clickable
            return False

    # ---------------------------------------------------------------- dry runs

    def dry_run_outcome(
        self,
        *,
        reached_confirm: bool,
        detail: str,
        screenshot: Optional[str] = None,
    ) -> Outcome:
        """The result of walking a return flow that was deliberately not submitted.

        Never ``PLACED``. Nothing was placed, so recording it would put a false
        ``Placed`` in the spec's ``Return status`` column - but the worse problem
        is that ``PLACED`` is a *final* status. Resume skips final items, so a
        rehearsal that claimed success would make the very next ``--live`` run
        skip every item the rehearsal had just walked, and place nothing.

        ``PLANNED`` says what is true: eligible, walked, not attempted. It writes
        a blank ``Return status`` with a ``Pending`` task status, and it requeues.
        A flow that never reached its confirm step is ``FAILED`` instead - also
        non-final, but flagged for a human, because that is a broken flow rather
        than a successful rehearsal.
        """
        status = ReturnStatus.PLANNED if reached_confirm else ReturnStatus.FAILED
        outcome = "reached the final confirm step" if reached_confirm else (
            "never reached a confirm step, so the flow may have changed"
        )
        return Outcome(
            status=status,
            log=(
                f"DRY RUN: walked the return flow for this item and {outcome}. "
                f"{detail} Nothing was submitted - re-run with --live to place "
                "the return."
            ),
            screenshots=[screenshot] if screenshot else [],
            dry_run=True,
        ).stamp()

    # ---------------------------------------------------------------- discovery

    #: Pages of order history to walk before stopping. An account can hold years
    #: of orders; a run that walks all of them is a crawl, not a returns pass.
    discovery_max_pages = 3

    def discover_returnable(
        self,
        *,
        max_orders: int = 25,
        within_days: int = 30,
        today: Optional[dt.date] = None,
    ) -> list[LineItem]:
        """Walk My Orders and build line items from what the account actually holds.

        This is the input path for a run with no spreadsheet: order ids, SKUs and
        delivery dates all come from the live page. Two bounds keep it honest -
        ``within_days`` stops the walk falling off the end of an order history,
        and ``max_orders`` caps how much one session takes on.

        Cards are filtered on their visible copy: kept when the platform shows a
        return is possible, dropped when it already shows a refund, a return in
        flight or a closed window. That filter is a *cheap pre-pass*, not the
        verdict - each surviving item still goes through the adapter's normal
        classification, which is what actually decides the outcome.
        """
        today = today or dt.date.today()
        self.session.goto(self.sel["urls"]["orders"])
        self.human.think()
        self.session.assert_not_challenged()

        items: list[LineItem] = []
        seen: set[str] = set()
        cards_seen = 0

        for page_index in range(self.discovery_max_pages):
            if progress.stop.requested or len(seen) >= max_orders:
                break
            if page_index:
                if not self._open_next_orders_page():
                    break
                self.human.think()

            self.human.scroll(400)
            cards = find_all(self.page, self.s("orders_page", "order_card"), timeout=10000)
            if cards is None:
                log.warning(
                    "No order cards matched on %s. Either the account has no "
                    "orders, or the orders_page.order_card selectors need a look.",
                    self.platform.value,
                )
                break

            for index in range(cards.count()):
                if len(seen) >= max_orders:
                    break
                cards_seen += 1
                card = cards.nth(index)
                found = self._line_items_from_card(card, today=today, within_days=within_days)
                if found is None or not found:
                    continue
                order_id = found[0].order_id
                if order_id in seen:
                    continue
                seen.add(order_id)
                items.extend(found)

        log.info(
            "Discovery on %s: %d order card(s) examined, %d order(s) kept, "
            "%d line item(s) to consider.",
            self.platform.value,
            cards_seen,
            len(seen),
            len(items),
        )
        return items

    def _line_items_from_card(
        self,
        card: Locator,
        *,
        today: dt.date,
        within_days: int,
    ) -> Optional[list[LineItem]]:
        """One order card -> its line items, or None when the card is not usable."""
        text = text_of(card)
        if not text:
            return None

        order_id = first_group(self.s("discovery", "order_id_patterns"), text)
        if not order_id:
            # The id is sometimes only in the link, not the visible copy.
            order_id = first_group(
                self.s("discovery", "order_id_patterns"), " ".join(self._hrefs_in(card))
            )
        if not order_id:
            return None

        if self._card_is_settled(text):
            return None
        if not self._card_looks_returnable(text):
            return None

        delivery_date, approx = self._delivery_date_from(text)
        if delivery_date and (today - delivery_date).days > within_days:
            # Older than the window we were asked to look at. Ordering on these
            # pages is newest-first, so this is also the natural stopping point.
            return None

        # The order list states a return window only sometimes ("7 days return
        # policy"). It feeds the same local pre-filter the spreadsheet's own
        # Return Window column feeds; when the page does not say, the note
        # records that rather than leaving a bare blank, and the platform's own
        # check on the order page remains the authority either way.
        window_days = self._return_window_from(text)
        notes = ["Discovered from the live orders page; no spreadsheet row."]
        if window_days is None:
            notes.append(
                "Return window not stated on the orders page; eligibility "
                "deferred to the platform."
            )

        # source_row is filled in later, when these orders are recorded into the
        # results workbook; discovery has no sheet to point back at.
        found: list[LineItem] = []
        for href in self._hrefs_in(card):
            if not is_product_url(href, self.platform):
                continue
            sku = sku_of(href)
            if not sku or any(i.sku == sku for i in found):
                continue
            found.append(
                LineItem(
                    source_row=0,
                    item_index=len(found),
                    order_id=order_id,
                    platform=self.platform,
                    sku=sku,
                    product_url=href,
                    title_hint=title_hint_of(href),
                    delivery_date=delivery_date,
                    delivery_date_is_approx=approx,
                    return_window_days=window_days,
                    parse_notes=notes,
                )
            )
        return found

    @staticmethod
    def _hrefs_in(card: Locator) -> list[str]:
        try:
            return [
                h
                for h in card.locator("a").evaluate_all(
                    "els => els.map(e => e.href || '')"
                )
                if h
            ]
        except Exception:  # noqa: BLE001
            return []

    def _card_looks_returnable(self, text: str) -> bool:
        markers = self.s("discovery", "returnable_markers")
        haystack = text.lower()
        return any(m.lower() in haystack for m in markers) if markers else True

    def _card_is_settled(self, text: str) -> bool:
        """True when the card already shows a refund, a return or a closed window."""
        haystack = text.lower()
        return any(m.lower() in haystack for m in self.s("discovery", "skip_markers"))

    def _return_window_from(self, text: str) -> Optional[int]:
        """Day count from copy like '10 days return policy', when the page says."""
        for pattern in self.s("discovery", "return_window_patterns"):
            match = re.search(pattern, text, re.I)
            if match:
                try:
                    return int(match.group(1))
                except (ValueError, IndexError):
                    continue
        return None

    def _delivery_date_from(self, text: str) -> tuple[Optional[dt.date], bool]:
        for pattern in self.s("discovery", "delivered_on_patterns"):
            match = re.search(pattern, text, re.I)
            if match:
                return parse_date(match.group(1))
        return None, False

    def _open_next_orders_page(self) -> bool:
        target = find(self.page, self.s("discovery", "next_page"), timeout=4000)
        if target is None:
            return False
        try:
            self.human.click(target)
            self.page.wait_for_load_state("domcontentloaded")
            self.session.assert_not_challenged()
            return True
        except AgentAbort:
            raise
        except Exception:  # noqa: BLE001
            return False

    # -------------------------------------------------------------------- login

    def log_in(self, login_url: str, *, timeout_s: int = 600) -> None:
        """Sign in, driving the OTP form when a phone number is configured.

        The agent fills the phone number and presses "request OTP" itself. The
        code cannot be automated - it is delivered out of band to the account
        holder - so it comes from the configured provider (a terminal prompt, or
        an input in the web panel) and the agent submits it.

        With no phone number configured, or if the form cannot be located, this
        falls back to :meth:`hand_off_login` so a run is never blocked by a
        login page that has changed shape.
        """
        phone = str(self.config.get("phone") or "").strip()
        if phone and self.otp_provider is not None:
            try:
                if self._otp_login(login_url, phone, timeout_s=timeout_s):
                    return
                log.warning(
                    "Automated OTP sign-in did not complete; handing over to the operator."
                )
            except AgentAbort:
                raise
            except Exception as exc:  # noqa: BLE001 - fall back, never fail the run
                log.warning("Automated OTP sign-in failed (%s); handing over.", exc)
        self.hand_off_login(login_url, timeout_s=timeout_s)

    def _otp_login(self, login_url: str, phone: str, *, timeout_s: int) -> bool:
        """Drive the phone + OTP form. False means fall back to a manual sign-in."""
        self.session.goto(login_url)

        phone_field = find(self.page, self.s("login", "phone_input"), timeout=8000)
        if phone_field is None:
            return False

        self.human.type_text(phone_field, phone)
        self.human.micro()

        request = find(self.page, self.s("login", "request_otp"), timeout=5000)
        if request is None:
            return False
        self.human.click(request)
        self.session.assert_not_challenged()
        self.human.think()

        # Only ask for a code once the field for it actually exists, so the
        # operator is not prompted for a code the site never sent.
        otp_field = find(self.page, self.s("login", "otp_input"), timeout=15000)
        if otp_field is None:
            return False

        code = self.otp_provider(  # type: ignore[misc]
            platform=self.platform.value,
            phone=phone,
            timeout_s=min(timeout_s, DEFAULT_TIMEOUT_S),
        )
        if not code:
            return False

        self.human.type_text(otp_field, code)
        submit = find(self.page, self.s("login", "submit"), timeout=4000)
        if submit is not None:
            self.human.click(submit)
        else:
            self.page.keyboard.press("Enter")

        self.page.wait_for_load_state("domcontentloaded")
        self.human.think(1.3)
        self.session.assert_not_challenged()

        # The site may take a moment to establish the session after submitting.
        for _ in range(10):
            if self.is_logged_in():
                return True
            self.page.wait_for_timeout(1500)
        return False

    def hand_off_login(self, login_url: str, *, timeout_s: int = 600) -> None:
        """Open the login page and wait for the operator to sign in by hand.

        The agent never types the phone number or the OTP. Credentials stay with
        the person, and the login form sees genuine human keystrokes - which is
        also the least detectable way to authenticate. Because the browser
        profile persists, this is a once-per-profile interruption rather than a
        per-run one.
        """
        import time

        self.session.goto(login_url)
        banner = (
            "\n"
            + "=" * 78
            + f"\n  ACTION NEEDED: sign in to {self.platform.value} in the Chrome window.\n"
            + "=" * 78
            + "\n"
            "  The agent does not handle your credentials or OTP - please type\n"
            "  them yourself in the browser window that just opened.\n"
            f"\n  For the Flipkart test account, request the OTP by calling the\n"
            f"  registered number, then enter it in the browser.\n"
            "\n  This profile is saved, so you should not need to do this again\n"
            "  on future runs unless the platform signs you out.\n"
            f"\n  Waiting up to {timeout_s // 60} minutes for sign-in to complete...\n"
        )
        print(banner, flush=True)

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            progress.check_stop("Stopped by the operator while waiting for sign-in.")
            self.page.wait_for_timeout(3000)
            try:
                if self.is_logged_in():
                    print("  Sign-in detected. Continuing.\n", flush=True)
                    self.human.think()
                    return
            except Exception:  # noqa: BLE001 - page may be navigating
                continue

        raise AgentAbort(
            f"Timed out after {timeout_s}s waiting for a manual {self.platform.value} "
            "sign-in. No returns were attempted; every item stays Pending."
        )
