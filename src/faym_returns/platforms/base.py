"""Platform adapter contract and shared selector plumbing."""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterable, Optional, Sequence

import yaml
from playwright.sync_api import Locator, Page

from .. import progress
from ..browser import Session
from ..models import AgentAbort, LineItem, Outcome, Platform, ReturnFlow
from ..otp import DEFAULT_TIMEOUT_S, OtpProvider

log = logging.getLogger(__name__)

SELECTOR_DIR = Path(__file__).resolve().parent.parent / "selectors"

_ROLE_RE = re.compile(r"^role:(?P<role>[a-z]+)(?:\[name=(?P<name>.+)\])?$", re.I)
_REGEX_RE = re.compile(r"^/(?P<body>.*)/(?P<flags>[a-z]*)$")


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
