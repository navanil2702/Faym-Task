"""Normalizer tests, driven by the real cell contents from the test dataset.

The awkward strings below are copied verbatim from
"Faym Status Test Orders.xlsx" - WhatsApp export prefixes, NA markers, a Meesho
promo blurb and a stray size note - because those are exactly the shapes that
break naive parsing.
"""

from __future__ import annotations

import datetime as dt

import pytest

from faym_returns.models import Platform
from faym_returns.normalize import (
    explode_row,
    extract_items,
    parse_date,
    parse_return_window,
    sku_of,
    title_hint_of,
)

FK = Platform.FLIPKART


# --------------------------------------------------------------- window parsing


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("10 Days", 10),
        ("7 Days ", 7),  # trailing space, row 3
        ("10 Day", 10),  # singular, row 5
        ("  30 days  ", 30),
        ("7day", 7),
        (None, None),
        ("", None),
        ("no return", None),
        (10, 10),
    ],
)
def test_parse_return_window(raw, expected):
    assert parse_return_window(raw) == expected


# ----------------------------------------------------------------- date parsing


def test_parse_date_real_datetime():
    value, approx = parse_date(dt.datetime(2026, 6, 27))
    assert value == dt.date(2026, 6, 27)
    assert approx is False


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("5-6 July", dt.date(2026, 7, 6)),
        ("6-7 July", dt.date(2026, 7, 7)),
        ("8-9 July", dt.date(2026, 7, 9)),
    ],
)
def test_parse_date_range_takes_later_day(raw, expected):
    """Ranges resolve to the later day: the local check is only a pre-filter,
    so erring long avoids skipping an item that is still returnable."""
    value, approx = parse_date(raw, year_hint=2026)
    assert value == expected
    assert approx is True


def test_parse_date_unparseable():
    assert parse_date("sometime soon")[0] is None


# ------------------------------------------------------------------ sku / title


def test_sku_prefers_pid():
    url = (
        "https://www.flipkart.com/wearza-colorblock-men-round-neck-black-yellow-t-shirt"
        "/p/itm709cb9948322a?pid=TSHG9FQZSSAUGKUP&lid=LSTTSHG9FQZSSAUGKUPDCX8GY"
    )
    assert sku_of(url) == "TSHG9FQZSSAUGKUP"


def test_sku_falls_back_to_itm():
    assert sku_of("https://www.flipkart.com/gulab-thar/p/itm076ca9134ae67") == "ITM076CA9134AE67"


def test_title_hint_from_slug():
    url = "https://dl.flipkart.com/dl/carrylux-women-pink-shoulder-bag/p/itm3bb9bd58cf9d4?pid=X"
    assert title_hint_of(url) == "carrylux women pink shoulder bag"


# -------------------------------------------------------------- NA marker logic

ROW6_CELL = """
https://dl.flipkart.com/dl/dolsia-regular-women-blue-jeans/p/itme00ed7c7bdab3?pid=JEAHJHY3CBJYNZNW&lid=LSTJEAHJHY3CBJYNZNW40T4CZ&marketplace=FLIPKART

https://dl.flipkart.com/dl/tokyo-talkies-loose-fit-women-blue-jeans/p/itmba1701124098b?pid=JEAH87B2GRCCS3DZ&lid=LSTJEAH87B2GRCCS3DZ7RS6HF&marketplace=FLIPKART  NA

https://dl.flipkart.com/dl/pinklit-wide-leg-women-blue-jeans/p/itm4bf6efd95191a?pid=JEAHZGYAXWNUKNZU&lid=LSTJEAHZGYAXWNUKNZUZXHXPG&marketplace=FLIPKART

https://dl.flipkart.com/dl/vasan-women-a-line-maroon-midi-calf-length-dress/p/itm9dd3d2688a45a?pid=DREHHF5SKMVFGUKU&lid=LSTDREHHF5SKMVFGUKUTKT71H&marketplace=FLIPKART

https://dl.flipkart.com/dl/shivanshcloset-women-fit-flare-blue-beige-maxi-full-length-dress/p/itm30fb421b5f582?pid=DREHK6H2PN8XK8ZM&lid=LSTDREHK6H2PN8XK8ZMHQ1UOC&marketplace=FLIPKART
"""

ROW8_CELL = """"[5:59 pm, 26/06/2026] Promo Faym: https://dl.flipkart.com/dl/carrylux-women-pink-shoulder-bag/p/itm3bb9bd58cf9d4?pid=HMBH8MV7VA3PCJDQ&lid=LSTHMBH8MV7VA3PCJDQQRJ3XX&marketplace=FLIPKART
[5:59 pm, 26/06/2026] Promo Faym: https://dl.flipkart.com/dl/carrylux-women-beige-shoulder-bag/p/itm1c650952660b7?pid=HMBH8KTQFP5BPSZG&marketplace=FLIPKART
[6:00 pm, 26/06/2026] Promo Faym: https://dl.flipkart.com/dl/carrylux-women-red-shoulder-bag/p/itm675b5c4a02b55?pid=HMBH7MYANGQFUKJH&marketplace=FLIPKART
[6:00 pm, 26/06/2026] Promo Faym: https://dl.flipkart.com/dl/carrylux-women-black-shoulder-bag/p/itm258e66d38470b?pid=HMBH8BNZ6JXFJCD9&marketplace=FLIPKART  NA
[6:01 pm, 26/06/2026] Promo Faym: https://dl.flipkart.com/dl/liziqi-women-maroon-shoulder-bag/p/itm814f5b6342c72?pid=HMBHGNTPSZPQ48QG&marketplace=FLIPKART
[6:02 pm, 26/06/2026] Promo Faym: Hey, check out this product on Meesho!

Get upto 25% OFF on your first order. Also grab extra 25% on new products every 3 hours!

size xxl\""""

ROW7_CELL = (
    "[8:23 pm, 26/06/2026] Arti Faym C: Take a look at this Embroidered Bollywood "
    "Satin, Silk Blend Saree on Flipkart https://dl.flipkart.com/dl/clothclick-"
    "embroidered-bollywood-satin-silk-blend-saree/p/itm5553591988105?pid="
    "SARHMZB7GBADA4FK&lid=LSTSARHMZB7GBADA4FKYMPPEH\n"
    "[8:25 pm, 26/06/2026] Arti Faym C: Take a look at this Embroidered Bollywood "
    "Georgette Saree on Flipkart https://dl.flipkart.com/dl/brahmani-creation-"
    "embroidered-bollywood-georgette-saree/p/itm847bb75fda558?pid=SARHKVNUD2PSCGY6"
)


def test_na_marker_excludes_only_the_marked_item():
    """Five links, one trailed by NA -> four ordered. Row 6 declares qty 4."""
    items = extract_items(ROW6_CELL, FK)
    assert len(items) == 5
    ordered = {sku_of(url) for url, is_ordered in items if is_ordered}
    not_ordered = {sku_of(url) for url, is_ordered in items if not is_ordered}
    assert not_ordered == {"JEAH87B2GRCCS3DZ"}  # tokyo-talkies
    assert len(ordered) == 4


def test_whatsapp_noise_and_other_marketplaces_are_discarded():
    """Row 8: five Flipkart links, one NA, plus a Meesho blurb and a size note."""
    items = extract_items(ROW8_CELL, FK)
    assert len(items) == 5, "the Meesho promo text must not produce a line item"
    not_ordered = [sku_of(u) for u, ok in items if not ok]
    assert not_ordered == ["HMBH8BNZ6JXFJCD9"]  # black shoulder bag
    assert sum(1 for _, ok in items if ok) == 4


def test_urls_embedded_in_chat_sentences_are_extracted():
    items = extract_items(ROW7_CELL, FK)
    assert [sku_of(u) for u, _ in items] == ["SARHMZB7GBADA4FK", "SARHKVNUD2PSCGY6"]
    assert all(ok for _, ok in items)


def test_duplicate_pids_are_deduplicated():
    url = "https://www.flipkart.com/x-shirt/p/itm1?pid=ABC123"
    assert len(extract_items(f"{url}\n{url}", FK)) == 1


def test_na_inside_a_word_is_not_a_marker():
    """'NATURAL' must not be read as the NA marker."""
    cell = "https://www.flipkart.com/x/p/itm1?pid=ABC123 NATURAL cotton"
    items = extract_items(cell, FK)
    assert items[0][1] is True


# ----------------------------------------------------------------- row explode


def _row(**overrides):
    base = {
        "Order Id": "OD337974610559997100",
        "Platform": "Flipkart",
        "Product Link": ROW6_CELL,
        "Amount": 2579.0,
        "No of Product": 4.0,
        "Order date": dt.datetime(2026, 7, 1),
        "Delivery date": "5-6 July",
        "Return Window": "10 Days",
        "Status": "Pending",
    }
    base.update(overrides)
    return base


def test_explode_row_produces_one_item_per_sku():
    items = explode_row(_row(), source_row=6)
    assert len(items) == 5
    assert sum(1 for i in items if i.ordered) == 4
    assert all(i.source_row == 6 for i in items)
    assert [i.item_index for i in items] == [0, 1, 2, 3, 4]


def test_explode_row_reconciles_with_no_of_product():
    """Counts agree, so no mismatch warning is raised."""
    items = explode_row(_row(), source_row=6)
    assert not any("Count mismatch" in n for n in items[0].parse_notes)


def test_explode_row_flags_count_mismatch():
    items = explode_row(_row(**{"No of Product": 2.0}), source_row=6)
    assert any("Count mismatch" in n for n in items[0].parse_notes)


def test_order_total_is_not_split_per_item():
    """The Amount column is order-level; refunds must come from the platform."""
    items = explode_row(_row(), source_row=6)
    assert all(i.order_total == 2579.0 for i in items)


def test_explode_row_without_order_id_is_dropped():
    assert explode_row(_row(**{"Order Id": None}), source_row=9) == []


def test_unparseable_product_cell_still_yields_a_flagged_item():
    items = explode_row(_row(**{"Product Link": "size xxl, will send later"}), source_row=6)
    assert len(items) == 1
    assert items[0].sku == ""
    assert any("No usable product URL" in n for n in items[0].parse_notes)


def test_approx_delivery_is_flagged_in_notes():
    items = explode_row(_row(), source_row=6)
    assert items[0].delivery_date_is_approx is True
    assert any("approximated" in n for n in items[0].parse_notes)


def test_na_item_carries_an_explanatory_note():
    items = explode_row(_row(), source_row=6)
    na_item = next(i for i in items if not i.ordered)
    assert any("never ordered" in n for n in na_item.parse_notes)
