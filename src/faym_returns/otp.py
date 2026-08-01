"""Getting a one-time code to the agent mid-login.

The agent can enter the phone number and press "request OTP" on its own, but the
code itself arrives out of band - by call or SMS to the account holder - so a
person has to hand it over. That is the only manual step in the login, and it
exists because of how OTP works, not because of a gap in the automation.

The provider is an injection point rather than a hard-coded ``input()`` so the
sign-in flow does not have to assume a terminal is attached; a scheduled or
supervised run can supply the code another way without touching the adapters.

Codes are held in memory only for the moment between entry and submission. They
are never logged, written to the workbook, or persisted.
"""

from __future__ import annotations

from typing import Optional, Protocol

#: Longest an OTP is worth waiting for. Codes typically expire well inside this.
DEFAULT_TIMEOUT_S = 300


class OtpProvider(Protocol):
    """Supplies a one-time code, or ``None`` if it could not be obtained."""

    def __call__(
        self, *, platform: str, phone: str, timeout_s: int = DEFAULT_TIMEOUT_S
    ) -> Optional[str]:
        ...


def clean(code: object) -> Optional[str]:
    """Keep only the digits an OTP field will accept."""
    digits = "".join(ch for ch in str(code or "") if ch.isdigit())
    return digits or None


def console_otp(
    *, platform: str, phone: str, timeout_s: int = DEFAULT_TIMEOUT_S
) -> Optional[str]:
    """Ask for the code on stdin. Blocks until the operator answers.

    Returning ``None`` - on an empty line, EOF or Ctrl-C - tells the caller to
    fall back to a fully manual sign-in rather than failing the run.
    """
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
        return clean(input("  OTP: "))
    except (EOFError, KeyboardInterrupt):
        return None
