"""Local return-window pre-filter.

This check exists to avoid pointless browser work, not to make the final call.
The platform is always the authority on whether an item can be returned: it
knows the true delivery timestamp, seller-specific policies and any extension
granted on the account. So this module only rejects an item when it is *clearly*
past the window, and stays out of the way whenever the inputs are fuzzy.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Optional

from .models import LineItem


@dataclass
class Eligibility:
    eligible: bool
    """False only when we are confident the window has closed."""

    reason: str
    window_ends: Optional[dt.date] = None
    days_left: Optional[int] = None
    confident: bool = True
    """False when the verdict rests on an approximated or missing date."""


def check(
    item: LineItem,
    *,
    today: Optional[dt.date] = None,
    grace_days: int = 1,
) -> Eligibility:
    """Decide whether it is worth opening a browser for ``item``.

    ``grace_days`` widens the window before we declare an item ineligible,
    absorbing timezone skew and the day-boundary ambiguity in delivery dates
    that were typed as a range.
    """
    today = today or dt.date.today()

    if not item.ordered:
        return Eligibility(False, "Item was marked NA in the source sheet - never ordered.")

    if item.return_window_days is None or item.delivery_date is None:
        missing = []
        if item.return_window_days is None:
            missing.append("return window")
        if item.delivery_date is None:
            missing.append("delivery date")
        return Eligibility(
            True,
            f"Cannot compute the window locally ({' and '.join(missing)} unknown); "
            "deferring to the platform.",
            confident=False,
        )

    window_ends = item.delivery_date + dt.timedelta(days=item.return_window_days)
    days_left = (window_ends - today).days
    confident = not item.delivery_date_is_approx

    if days_left + grace_days < 0:
        detail = (
            f"Window closed on {window_ends:%Y-%m-%d} "
            f"({abs(days_left)} day(s) ago; delivered {item.delivery_date:%Y-%m-%d} "
            f"+ {item.return_window_days} day window)."
        )
        if not confident:
            detail += " Delivery date was approximated from a date range."
        return Eligibility(False, detail, window_ends, days_left, confident)

    return Eligibility(
        True,
        f"In window until {window_ends:%Y-%m-%d} ({days_left} day(s) left).",
        window_ends,
        days_left,
        confident,
    )
