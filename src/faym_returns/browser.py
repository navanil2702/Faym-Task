"""Browser session management with a legitimate-session posture.

Design choices that matter for not getting flagged:

Real Chrome, real profile
    Launched via ``channel="chrome"`` against the installed Google Chrome, not
    bundled Chromium, and with a persistent ``user_data_dir``. The profile keeps
    cookies, localStorage and device trust, so the OTP login happens once rather
    than every run - repeated fresh logins from a clean fingerprint are
    themselves a strong bot signal.

Headful only
    Headless Chrome is trivially detectable and is refused here outright.

Consistent India-resident fingerprint
    ``en-IN`` locale, ``Asia/Kolkata`` timezone and matching geolocation, so the
    fingerprint agrees with the account's real-world context every run.

Quiet hours and throughput caps
    A human does not file returns at 04:00, nor forty of them in ten minutes.
    Both are enforced before any work starts.

Challenge handling
    When a captcha or an unexpected login wall appears the session aborts rather
    than retrying. Hammering a challenge is what converts a soft flag into a
    hard block; unfinished items simply stay Pending for a later run.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from playwright.sync_api import BrowserContext, Page, sync_playwright

from .humanize import Human, Pacing
from .models import AgentAbort

log = logging.getLogger(__name__)

#: Injected before any page script runs.
#:
#: Deliberately minimal. Running against real Chrome with --enable-automation
#: suppressed already produces a genuine fingerprint: navigator.webdriver is
#: ``false``, window.chrome exists, and the native PluginArray and
#: navigator.languages are real. Over-patching those is counterproductive -
#: forcing navigator.webdriver to ``undefined`` is itself a tell, because no
#: real browser reports undefined, and faking navigator.languages desynchronises
#: it from the Accept-Language header that Playwright derives from the locale.
#:
#: So the only correction applied is the one case that is genuinely wrong: when
#: webdriver reports ``true``, which happens on the bundled-Chromium fallback
#: path. It is normalised to ``false`` - the real-Chrome value - not removed.
STEALTH_SCRIPT = """
if (navigator.webdriver === true) {
  try {
    Object.defineProperty(navigator, 'webdriver', {get: () => false, configurable: true});
  } catch (e) { /* non-configurable in this build; nothing safe to do */ }
}
if (!window.chrome) {
  window.chrome = {runtime: {}};
}
"""

#: Text that means the platform is challenging us rather than serving the page.
CHALLENGE_MARKERS = [
    "unusual traffic",
    "are you a robot",
    "verify you are human",
    "enter the characters you see",
    "solve this puzzle",
    "access denied",
    "request blocked",
    "captcha",
]


@dataclass
class SessionConfig:
    profile_dir: Path
    artifacts_dir: Path
    headless: bool = False
    """Kept for signature completeness; a True value is rejected."""

    locale: str = "en-IN"
    timezone: str = "Asia/Kolkata"
    geolocation: tuple[float, float] = (28.4595, 77.0266)  # Gurgaon
    viewport: tuple[int, int] = (1440, 900)
    quiet_hours: tuple[int, int] = (1, 7)
    """Local-time hour range [start, end) during which no run may begin."""

    seed: Optional[int] = None
    pacing: Pacing = field(default_factory=Pacing)
    slow_mo_ms: int = 0


class Session:
    """A single supervised browser session against one platform account."""

    def __init__(self, config: SessionConfig):
        if config.headless:
            raise ValueError(
                "Headless mode is refused: headless Chrome is trivially "
                "detectable and would get the account flagged."
            )
        self.config = config
        self._playwright = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.human: Optional[Human] = None

    # --------------------------------------------------------------- lifecycle

    def check_quiet_hours(self, now: Optional[dt.datetime] = None) -> None:
        now = now or dt.datetime.now()
        start, end = self.config.quiet_hours
        in_quiet = start <= now.hour < end if start <= end else (now.hour >= start or now.hour < end)
        if in_quiet:
            raise AgentAbort(
                f"Refusing to start at {now:%H:%M}: inside configured quiet hours "
                f"{start:02d}:00-{end:02d}:00. Automated activity while the account "
                "holder would plausibly be asleep is an obvious bot signal."
            )

    def __enter__(self) -> "Session":
        self.check_quiet_hours()
        self.config.profile_dir.mkdir(parents=True, exist_ok=True)
        self.config.artifacts_dir.mkdir(parents=True, exist_ok=True)

        self._playwright = sync_playwright().start()
        width, height = self.config.viewport

        launch_kwargs = dict(
            user_data_dir=str(self.config.profile_dir),
            headless=False,
            locale=self.config.locale,
            timezone_id=self.config.timezone,
            geolocation={
                "latitude": self.config.geolocation[0],
                "longitude": self.config.geolocation[1],
            },
            permissions=["geolocation"],
            viewport={"width": width, "height": height},
            slow_mo=self.config.slow_mo_ms,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--start-maximized",
                "--no-default-browser-check",
                "--no-first-run",
            ],
            ignore_default_args=["--enable-automation"],
        )

        try:
            self.context = self._playwright.chromium.launch_persistent_context(
                channel="chrome", **launch_kwargs
            )
            log.info("Launched installed Google Chrome with persistent profile.")
        except Exception as exc:  # noqa: BLE001 - fall back to bundled Chromium
            log.warning(
                "Could not launch installed Chrome (%s); falling back to bundled "
                "Chromium. Detection risk is higher - install Chrome if possible.",
                exc,
            )
            self.context = self._playwright.chromium.launch_persistent_context(**launch_kwargs)

        self.context.add_init_script(STEALTH_SCRIPT)
        self.context.set_default_timeout(30000)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.human = Human(self.page, pacing=self.config.pacing, seed=self.config.seed)
        return self

    def __exit__(self, *exc_info) -> None:
        try:
            if self.context:
                self.context.close()
        finally:
            if self._playwright:
                self._playwright.stop()

    # ----------------------------------------------------------------- helpers

    def new_page(self, *, close_previous: bool = False) -> Page:
        """Open a fresh tab and make it current.

        The workflow calls for a new tab per record, so ``close_previous`` lets
        the caller retire a finished record's tab instead of accumulating one per
        order across the whole run. The browser context - and therefore the
        logged-in session - is shared between tabs, so a new tab never means
        signing in again.
        """
        assert self.context is not None
        previous = self.page
        page = self.context.new_page()
        self.page = page
        self.human = Human(page, pacing=self.config.pacing, seed=self.config.seed)

        if close_previous and previous is not None and previous is not page:
            try:
                previous.close()
            except Exception as exc:  # noqa: BLE001 - a stale tab is not fatal
                log.debug("Could not close the previous tab: %s", exc)
        return page

    def goto(self, url: str, *, wait: str = "domcontentloaded") -> None:
        assert self.page is not None and self.human is not None
        self.page.goto(url, wait_until=wait, timeout=60000)
        self.human.think()
        self.assert_not_challenged()

    def assert_not_challenged(self) -> None:
        """Abort the whole session if the platform is showing a challenge."""
        assert self.page is not None
        try:
            body = (self.page.inner_text("body", timeout=5000) or "").lower()
        except Exception:  # noqa: BLE001 - page may be mid-navigation
            return
        # Only treat a challenge marker as real if the page is mostly *just*
        # that; product pages legitimately mention words like "captcha".
        hit = next((m for m in CHALLENGE_MARKERS if m in body), None)
        if hit and len(body) < 2500:
            path = self.screenshot("challenge")
            raise AgentAbort(
                f"Bot-detection challenge detected (matched {hit!r}). Stopping the "
                f"session instead of retrying, to avoid escalating to a hard block. "
                f"Screenshot: {path}. Resume later from a supervised session."
            )

    def screenshot(self, label: str) -> str:
        assert self.page is not None
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in label)[:80]
        path = self.config.artifacts_dir / f"{stamp}-{safe}.png"
        try:
            self.page.screenshot(path=str(path), full_page=False)
        except Exception as exc:  # noqa: BLE001 - never fail a run over a screenshot
            log.warning("Screenshot %s failed: %s", label, exc)
            return ""
        return str(path)
