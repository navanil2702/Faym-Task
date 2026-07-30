"""Turn messy order-level spreadsheet rows into clean per-SKU line items.

The source sheet is maintained by hand, so the ``Product Link`` cell arrives in
whatever shape the operator pasted it: a single tidy URL, several URLs on their
own lines, or a raw WhatsApp export complete with timestamps, chat names,
promotional blurbs for other marketplaces, and size notes.

Two conventions in that cell carry real meaning and are handled explicitly:

``NA`` marker
    A bare ``NA`` token after a product URL means that item was *not* actually
    ordered. In the test dataset two rows carry five links but declare
    ``No of Product = 4``, and in both the extra link is the one trailed by
    ``NA``. Treating those as ordered would file returns for items the customer
    never bought, so they are parsed out and recorded as NOT_ORDERED.

``pid`` parameter
    Flipkart's ``pid=`` query param is the stable line-item identity. The sheet
    has no SKU column, so the pid is what lets us match a spreadsheet row to a
    specific item inside a multi-item order on the website.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Iterable, Optional

from dateutil import parser as date_parser

from .models import LineItem, Platform

#: Any http(s) run of non-whitespace. Deliberately greedy on query strings -
#: Flipkart tracking params contain ``=``, ``&``, ``%3D`` and base64 padding.
URL_RE = re.compile(r"https?://[^\s<>\"']+")

#: A product URL we can act on must identify a marketplace item.
FLIPKART_HOST_RE = re.compile(r"(?:^|\.)flipkart\.com$", re.I)

PID_RE = re.compile(r"[?&]pid=([A-Za-z0-9]+)", re.I)
ITM_RE = re.compile(r"/p/(itm[A-Za-z0-9]+)", re.I)
SLUG_RE = re.compile(r"/(?:dl/)?([a-z0-9][a-z0-9-]{3,})/p/itm", re.I)

#: Amazon line items are identified by ASIN in /dp/ or /gp/product/ paths.
ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", re.I)
AMAZON_HOST_RE = re.compile(r"(?:^|\.)amazon\.[a-z.]+$", re.I)

#: Standalone uppercase NA, not part of a longer token.
NA_MARKER_RE = re.compile(r"(?<![A-Za-z0-9])NA(?![A-Za-z0-9])")

#: "10 Days", "7 Days ", "10 Day", "10day" all mean the same thing.
WINDOW_RE = re.compile(r"(\d+)\s*day", re.I)

#: Fuzzy delivery cells: "5-6 July", "8-9 July", "6 - 7 Jul".
DATE_RANGE_RE = re.compile(
    r"(\d{1,2})\s*(?:-|–|—|to)\s*(\d{1,2})\s*([A-Za-z]{3,})", re.I
)
SINGLE_DAY_MONTH_RE = re.compile(r"(\d{1,2})\s*([A-Za-z]{3,})")


def _host_of(url: str) -> str:
    match = re.match(r"https?://([^/]+)", url)
    return match.group(1).lower() if match else ""


def is_product_url(url: str, platform: Optional[Platform]) -> bool:
    host = _host_of(url)
    if FLIPKART_HOST_RE.search(host):
        return bool(PID_RE.search(url) or ITM_RE.search(url))
    if AMAZON_HOST_RE.search(host):
        return bool(ASIN_RE.search(url))
    return False


def sku_of(url: str) -> str:
    """Best available stable identity for the line item behind ``url``."""
    for pattern in (PID_RE, ASIN_RE, ITM_RE):
        match = pattern.search(url)
        if match:
            return match.group(1).upper()
    return ""


def title_hint_of(url: str) -> str:
    """Human-readable product words from the URL slug, for UI matching."""
    match = SLUG_RE.search(url)
    if match:
        return match.group(1).replace("-", " ").strip()
    # Amazon: /Brand-Product-Name/dp/ASIN
    parts = [p for p in re.split(r"/+", url) if p]
    for idx, part in enumerate(parts):
        if part.lower() in {"dp", "product"} and idx > 0:
            candidate = parts[idx - 1]
            if "-" in candidate and not candidate.startswith("http"):
                return candidate.replace("-", " ").strip()
    return ""


def parse_return_window(raw: object) -> Optional[int]:
    """Extract a day count from cells like '10 Days', '7 Days ', '10 Day'."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return int(raw) if raw > 0 else None
    match = WINDOW_RE.search(str(raw))
    return int(match.group(1)) if match else None


def parse_date(raw: object, year_hint: Optional[int] = None) -> tuple[Optional[dt.date], bool]:
    """Parse a date cell that may be a real date or loose text.

    Returns ``(date, is_approx)``. For a range like "5-6 July" the *later* day
    is used: the local window check is only a pre-filter, and the platform is
    the final authority on eligibility, so erring long avoids skipping an item
    that is in fact still returnable.
    """
    if raw is None:
        return None, False
    if isinstance(raw, dt.datetime):
        return raw.date(), False
    if isinstance(raw, dt.date):
        return raw, False

    text = str(raw).strip()
    if not text:
        return None, False

    match = DATE_RANGE_RE.search(text)
    if match:
        day = int(match.group(2))
        month_word = match.group(3)
        parsed = _compose(day, month_word, year_hint)
        if parsed:
            return parsed, True

    match = SINGLE_DAY_MONTH_RE.search(text)
    if match:
        parsed = _compose(int(match.group(1)), match.group(2), year_hint)
        if parsed:
            return parsed, True

    try:
        default = dt.datetime(year_hint or dt.date.today().year, 1, 1)
        return date_parser.parse(text, default=default, dayfirst=True).date(), True
    except (ValueError, OverflowError, TypeError):
        return None, False


def _compose(day: int, month_word: str, year_hint: Optional[int]) -> Optional[dt.date]:
    try:
        month = date_parser.parse(month_word).month
    except (ValueError, OverflowError, TypeError):
        return None
    year = year_hint or dt.date.today().year
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def _clean_cell(raw: object) -> str:
    """Strip the stray wrapping quotes that hand-pasted cells pick up."""
    if raw is None:
        return ""
    return str(raw).replace("\r", "\n").strip().strip('"').strip()


def extract_items(cell: object, platform: Optional[Platform]) -> list[tuple[str, bool]]:
    """Pull ``(url, was_ordered)`` pairs out of one Product Link cell.

    URLs are returned in the order they appear, deduplicated by SKU. An item is
    flagged not-ordered when a bare ``NA`` token sits in the text between its
    URL and the next URL (or the end of the cell).
    """
    text = _clean_cell(cell)
    if not text:
        return []

    matches = [m for m in URL_RE.finditer(text) if is_product_url(m.group(0), platform)]
    results: list[tuple[str, bool]] = []
    seen: set[str] = set()

    for idx, match in enumerate(matches):
        url = match.group(0)
        sku = sku_of(url)
        if not sku or sku in seen:
            continue
        seen.add(sku)

        # Text from the end of this URL up to the start of the next one is the
        # annotation zone for this item.
        gap_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        gap = text[match.end():gap_end]
        ordered = not NA_MARKER_RE.search(gap)
        results.append((url, ordered))

    return results


def explode_row(
    row: dict,
    source_row: int,
    *,
    default_platform: Optional[Platform] = None,
) -> list[LineItem]:
    """Fan one order-level sheet row out into per-SKU line items."""
    order_id = _clean_cell(row.get("Order Id"))
    if not order_id:
        return []

    platform = Platform.parse(row.get("Platform")) or default_platform
    order_date, _ = parse_date(row.get("Order date"))
    year_hint = order_date.year if order_date else None
    delivery_date, delivery_approx = parse_date(row.get("Delivery date"), year_hint)
    window_days = parse_return_window(row.get("Return Window"))

    qty_declared = row.get("No of Product")
    try:
        qty_declared = int(float(qty_declared)) if qty_declared not in (None, "") else None
    except (TypeError, ValueError):
        qty_declared = None

    order_total = row.get("Amount")
    try:
        order_total = float(order_total) if order_total not in (None, "") else None
    except (TypeError, ValueError):
        order_total = None

    pairs = extract_items(row.get("Product Link"), platform)
    shared_notes: list[str] = []

    if not pairs:
        shared_notes.append(
            "No usable product URL found in the Product Link cell; "
            "cannot identify which line items to return."
        )

    ordered_count = sum(1 for _, ordered in pairs if ordered)
    if qty_declared is not None and pairs and ordered_count != qty_declared:
        shared_notes.append(
            f"Count mismatch: {ordered_count} product link(s) look ordered "
            f"({len(pairs)} found, {len(pairs) - ordered_count} marked NA) but "
            f"'No of Product' says {qty_declared}. Verify the row by hand."
        )
    if window_days is None:
        shared_notes.append("Return Window cell could not be parsed into a day count.")
    if delivery_date is None:
        shared_notes.append("Delivery date cell could not be parsed.")

    items: list[LineItem] = []
    for index, (url, ordered) in enumerate(pairs):
        notes = list(shared_notes)
        if delivery_approx:
            notes.append(
                f"Delivery date approximated from {row.get('Delivery date')!r}; "
                "the platform's own eligibility check is authoritative."
            )
        if not ordered:
            notes.append("Marked 'NA' in the source cell - treated as never ordered.")
        items.append(
            LineItem(
                source_row=source_row,
                item_index=index,
                order_id=order_id,
                platform=platform,
                sku=sku_of(url),
                product_url=url,
                title_hint=title_hint_of(url),
                order_date=order_date,
                delivery_date=delivery_date,
                delivery_date_is_approx=delivery_approx,
                return_window_days=window_days,
                order_total=order_total,
                qty_declared=qty_declared,
                ordered=ordered,
                parse_notes=notes,
            )
        )

    if not items and order_id:
        # Keep an unresolvable row visible rather than dropping it silently.
        items.append(
            LineItem(
                source_row=source_row,
                item_index=0,
                order_id=order_id,
                platform=platform,
                sku="",
                product_url="",
                order_date=order_date,
                delivery_date=delivery_date,
                delivery_date_is_approx=delivery_approx,
                return_window_days=window_days,
                order_total=order_total,
                qty_declared=qty_declared,
                parse_notes=shared_notes,
            )
        )
    return items


def explode_rows(rows: Iterable[tuple[int, dict]], **kwargs) -> list[LineItem]:
    items: list[LineItem] = []
    for source_row, row in rows:
        items.extend(explode_row(row, source_row, **kwargs))
    return items
