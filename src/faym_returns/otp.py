"""Getting a one-time code to the agent mid-login.

The agent can enter the phone number and press "request OTP" on its own, but the
code itself arrives out of band - by call or SMS to the account holder - so a
person has to hand it over. That is the only manual step, and it exists because
of how OTP works, not because of a gap in the automation.

Two providers cover the two front ends:

``console_otp``
    Prompts on stdin. Used by the CLI, where an operator is at the terminal.

``OtpBroker``
    Used by the web panel: the agent blocks on :meth:`OtpBroker.request` while
    the browser is shown an input, and :meth:`OtpBroker.supply` releases it.

Codes are held in memory for the moments between entry and submission, and are
never logged, written to the workbook, or persisted.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional, Protocol

from . import progress

log = logging.getLogger(__name__)

#: Longest an OTP is worth waiting for. Codes typically expire well inside this.
DEFAULT_TIMEOUT_S = 300


class OtpProvider(Protocol):
    """Supplies a one-time code, or ``None`` if it could not be obtained."""

    def __call__(
        self, *, platform: str, phone: str, timeout_s: int = DEFAULT_TIMEOUT_S
    ) -> Optional[str]:
        ...


def _clean(code: object) -> Optional[str]:
    """Keep only the digits an OTP field will accept."""
    digits = "".join(ch for ch in str(code or "") if ch.isdigit())
    return digits or None


def console_otp(
    *, platform: str, phone: str, timeout_s: int = DEFAULT_TIMEOUT_S
) -> Optional[str]:
    """Ask for the code on stdin. Blocks until the operator answers."""
    print(
        "\n"
        + "=" * 78
        + f"\n  {platform} has sent a one-time code to {phone}.\n"
        + "=" * 78
        + "\n  Enter it below and the agent will submit it and carry on.\n"
        "  Press Enter with nothing typed to sign in by hand instead.\n",
        flush=True,
    )
    try:
        return _clean(input("  OTP: "))
    except (EOFError, KeyboardInterrupt):
        return None


class OtpBroker:
    """Hands a code from a web request to the thread running the agent."""

    def __init__(self) -> None:
        self._ready = threading.Event()
        self._code: Optional[str] = None
        self._pending: Optional[dict] = None

    @property
    def pending(self) -> Optional[dict]:
        """Details of the code currently being waited on, if any."""
        return dict(self._pending) if self._pending else None

    def request(
        self, *, platform: str, phone: str, timeout_s: int = DEFAULT_TIMEOUT_S
    ) -> Optional[str]:
        """Block until a code is supplied, the wait times out, or a stop lands."""
        self._code = None
        self._ready.clear()
        self._pending = {"platform": platform, "phone": phone, "timeout_s": timeout_s}
        progress.publish("otp_required", platform=platform, phone=phone, timeout_s=timeout_s)

        try:
            # Wake periodically so a stop request doesn't have to wait out the
            # whole timeout.
            waited = 0.0
            while waited < timeout_s:
                if self._ready.wait(1.0):
                    return self._code
                progress.check_stop("Stopped by the operator while waiting for an OTP.")
                waited += 1.0
            log.warning("Timed out waiting for an OTP for %s", platform)
            progress.publish("otp_timeout", platform=platform)
            return None
        finally:
            self._pending = None

    def supply(self, code: str) -> bool:
        """Provide the code the agent is waiting for. False if nothing is waiting."""
        if self._pending is None:
            return False
        self._code = _clean(code)
        self._ready.set()
        progress.publish("otp_received")
        return True

    def cancel(self) -> None:
        """Give up on the code; the agent falls back to a manual sign-in."""
        self._code = None
        self._ready.set()


#: Shared broker for the web panel, matching the single-active-run invariant.
broker = OtpBroker()
