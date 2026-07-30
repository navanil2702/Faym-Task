"""Core domain types for the returns agent.

The unit of work is a LineItem, never an order. An order row in the source
workbook fans out into one LineItem per SKU, and every LineItem gets its own
independently recorded outcome.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Platform(str, Enum):
    FLIPKART = "Flipkart"
    AMAZON = "Amazon"

    @classmethod
    def parse(cls, raw: object) -> Optional["Platform"]:
        if raw is None:
            return None
        text = str(raw).strip().lower()
        if not text:
            return None
        if "flipkart" in text:
            return cls.FLIPKART
        if "amazon" in text:
            return cls.AMAZON
        return None


class ReturnFlow(str, Enum):
    """How a platform lets us cover the items of one order.

    BATCH   - a single return flow can cover several line items at once.
    SEQUENTIAL - the return micro-flow is repeated once per line item.
    """

    BATCH = "batch"
    SEQUENTIAL = "sequential"


#: The only values the spec permits in the ``Return status`` column.
SPEC_PLACED = "Placed"
SPEC_FAILED = "Failed"
SPEC_OUT_OF_WINDOW = "Out of window"
SPEC_RETURN_STATUSES = (SPEC_PLACED, SPEC_FAILED, SPEC_OUT_OF_WINDOW)


class ReturnStatus(str, Enum):
    """Per-line-item outcome, at full precision.

    The spec fixes ``Return status`` to Placed / Failed / Out of window, which
    cannot express everything a platform actually reports. So these richer states
    drive the agent's own logic and are written to a ``Detail`` column, while
    :attr:`spec_status` collapses each one onto the permitted vocabulary for the
    ``Return status`` column itself. Nothing is lost and the column stays
    conformant.
    """

    PLACED = "Placed"
    ALREADY_REFUNDED = "Already Cancelled & Refunded"
    OUT_OF_WINDOW = "Out of window"
    NOT_DELIVERED = "Not yet delivered"
    SUPPORT_NEEDED = "Support Needed"
    NOT_RETURNABLE = "Not returnable"
    ITEM_NOT_FOUND = "Item not found on order"
    NOT_ORDERED = "Not ordered (NA)"
    FAILED = "Failed"
    PLANNED = "Planned (not attempted)"
    """Offline planning only - eligible and queued, but no browser was launched."""

    @property
    def is_final(self) -> bool:
        """Whether this outcome needs no further agent attempt."""
        return self not in {ReturnStatus.FAILED, ReturnStatus.PLANNED}

    @property
    def needs_human(self) -> bool:
        return self in {
            ReturnStatus.SUPPORT_NEEDED,
            ReturnStatus.FAILED,
            ReturnStatus.ITEM_NOT_FOUND,
            ReturnStatus.NOT_RETURNABLE,
            # Never ordered, so a human should confirm the sheet is right rather
            # than have the row quietly disappear.
            ReturnStatus.NOT_ORDERED,
        }

    @property
    def spec_status(self) -> str:
        """This outcome expressed in the spec's three permitted values.

        Two mappings deserve stating outright:

        ``Already Cancelled & Refunded`` -> ``Placed``
            A return and refund do exist for the line item; the agent simply did
            not have to create them. ``Failed`` would be plainly wrong, and the
            Detail column records that it pre-existed.

        ``Not yet delivered`` / ``Support Needed`` / ``Not ordered`` -> ``Failed``
            No return was placed and none can be, so the only truthful value left
            is ``Failed``. Each is flagged for human review with the reason in the
            log, per the spec's requirement not to drop them silently.
        """
        if self is ReturnStatus.PLANNED:
            # Nothing was attempted, so none of the three values is truthful -
            # "Failed" would assert a failure that never happened. Blank, paired
            # with a Task status of Pending, says exactly what is true.
            return ""
        if self in {ReturnStatus.PLACED, ReturnStatus.ALREADY_REFUNDED}:
            return SPEC_PLACED
        if self is ReturnStatus.OUT_OF_WINDOW:
            return SPEC_OUT_OF_WINDOW
        return SPEC_FAILED


class TaskStatus(str, Enum):
    PENDING = "Pending"
    DONE = "Done"
    NEEDS_REVIEW = "Needs human review"


#: Values in the source ``Status`` column that mean "not yet processed".
PENDING_STATUS_VALUES = {"to do", "todo", "pending", ""}


@dataclass
class LineItem:
    """One SKU on one order - the atomic unit of work and of write-back."""

    source_row: int
    """1-based row in the source sheet this item was exploded from."""

    item_index: int
    """0-based position of this item within its order row."""

    order_id: str
    platform: Optional[Platform]
    sku: str
    """Platform line-item identity. For Flipkart this is the ``pid`` param."""

    product_url: str
    title_hint: str = ""
    """Human-readable fragment from the URL slug, used for fuzzy UI matching."""

    order_date: Optional[dt.date] = None
    delivery_date: Optional[dt.date] = None
    delivery_date_is_approx: bool = False
    """True when the delivery date came from a fuzzy cell like "5-6 July"."""

    return_window_days: Optional[int] = None
    order_total: Optional[float] = None
    """Order-level amount. NOT per item - never split this to guess a refund."""

    qty_declared: Optional[int] = None
    """The order row's ``No of Product`` value, for cross-checking."""

    ordered: bool = True
    """False when the source cell marked this link ``NA`` - never ordered."""

    parse_notes: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.order_id}::{self.sku}"

    @property
    def label(self) -> str:
        return f"{self.order_id} item {self.item_index + 1} ({self.sku})"


@dataclass
class Outcome:
    """What the agent achieved for one LineItem."""

    status: ReturnStatus
    return_id: str = "N/A"
    refund_amount: Optional[float] = None
    """As reported by the platform. Never computed locally."""

    log: str = ""
    timestamp: Optional[dt.datetime] = None
    screenshots: list[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def task_status(self) -> TaskStatus:
        if self.status is ReturnStatus.PLANNED:
            # Nothing was attempted, so the task is still outstanding - neither
            # done nor a problem for a human to look at.
            return TaskStatus.PENDING
        if self.status.needs_human:
            return TaskStatus.NEEDS_REVIEW
        return TaskStatus.DONE

    @property
    def spec_status(self) -> str:
        """Value for the spec's ``Return status`` column."""
        return self.status.spec_status

    @property
    def refund_cell(self) -> object:
        return "N/A" if self.refund_amount is None else self.refund_amount

    def stamp(self) -> "Outcome":
        if self.timestamp is None:
            self.timestamp = dt.datetime.now()
        return self


class AgentAbort(Exception):
    """Raised when the session must stop entirely rather than continue.

    Used for bot-detection challenges and lost authentication - conditions
    where retrying would make things worse. Remaining items stay Pending so a
    later supervised run can pick them up.
    """


class LoginRequired(AgentAbort):
    """The platform needs an interactive login the agent cannot complete alone."""
