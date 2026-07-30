"""Return-window pre-filter tests."""

from __future__ import annotations

import datetime as dt

from faym_returns import eligibility
from faym_returns.models import LineItem, Platform


def _item(**overrides) -> LineItem:
    base = dict(
        source_row=2,
        item_index=0,
        order_id="OD337915012166989100",
        platform=Platform.FLIPKART,
        sku="TSHG9FQZSSAUGKUP",
        product_url="https://www.flipkart.com/x/p/itm1?pid=TSHG9FQZSSAUGKUP",
        delivery_date=dt.date(2026, 6, 27),
        return_window_days=10,
    )
    base.update(overrides)
    return LineItem(**base)


def test_in_window_is_eligible():
    verdict = eligibility.check(_item(), today=dt.date(2026, 7, 1))
    assert verdict.eligible
    assert verdict.window_ends == dt.date(2026, 7, 7)
    assert verdict.days_left == 6


def test_clearly_past_window_is_rejected():
    verdict = eligibility.check(_item(), today=dt.date(2026, 7, 30))
    assert not verdict.eligible
    assert "Window closed" in verdict.reason


def test_grace_day_covers_the_boundary():
    """One day past the window still gets attempted; the platform decides."""
    verdict = eligibility.check(_item(), today=dt.date(2026, 7, 8), grace_days=1)
    assert verdict.eligible


def test_two_days_past_with_one_grace_day_is_rejected():
    verdict = eligibility.check(_item(), today=dt.date(2026, 7, 9), grace_days=1)
    assert not verdict.eligible


def test_na_item_is_never_eligible():
    verdict = eligibility.check(_item(ordered=False), today=dt.date(2026, 6, 28))
    assert not verdict.eligible
    assert "NA" in verdict.reason


def test_missing_inputs_defer_to_the_platform():
    """Unknown dates must not cause a silent skip - the site is authoritative."""
    verdict = eligibility.check(
        _item(delivery_date=None, return_window_days=None), today=dt.date(2026, 7, 30)
    )
    assert verdict.eligible
    assert verdict.confident is False
    assert "deferring to the platform" in verdict.reason


def test_approx_delivery_marks_verdict_unconfident():
    verdict = eligibility.check(
        _item(delivery_date=dt.date(2026, 7, 6), delivery_date_is_approx=True),
        today=dt.date(2026, 7, 10),
    )
    assert verdict.eligible
    assert verdict.confident is False


def test_short_window_row_is_handled():
    """Row 3 of the dataset has a 7-day window, not 10."""
    verdict = eligibility.check(
        _item(return_window_days=7), today=dt.date(2026, 7, 20)
    )
    assert not verdict.eligible
