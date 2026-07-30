"""Amazon adapter - detects the batch flow, falls back to sequential.

Amazon's "Return or replace items" wizard lists every item on the order with its
own checkbox, so one pass can cover several line items at once. When those
checkboxes are present the adapter uses the batch path; otherwise it repeats the
per-item flow. Either way the result is recorded per line item.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

from playwright.sync_api import Locator

from ..models import (
    AgentAbort,
    LineItem,
    Outcome,
    Platform,
    ReturnFlow,
    ReturnStatus,
)
from .. import progress
from .base import (
    PlatformAdapter,
    find,
    find_all,
    first_group,
    parse_amount,
    publish_item_finished as _publish_item_finished,
    text_of,
)

log = logging.getLogger(__name__)


class AmazonAdapter(PlatformAdapter):
    platform = Platform.AMAZON
    selector_key = "amazon"

    # ------------------------------------------------------------------- login

    def is_logged_in(self) -> bool:
        return find(self.page, self.s("logged_in"), timeout=3000) is not None

    def ensure_logged_in(self) -> None:
        self.session.goto(self.sel["urls"]["orders"])
        if self.is_logged_in():
            log.info("Amazon session restored from the saved profile.")
            progress.publish("login_ok", platform=self.platform.value, restored=True)
            return
        self.log_in(self.sel["urls"]["login"])
        self.session.goto(self.sel["urls"]["orders"])
        if not self.is_logged_in():
            raise AgentAbort("Amazon still appears signed out after the manual login step.")

    # -------------------------------------------------------- flow detection

    def detect_flow(self, items: Sequence[LineItem]) -> ReturnFlow:
        """Batch when the wizard offers per-item checkboxes, else sequential.

        Called with the return wizard already open. A single-item order is
        treated as sequential regardless, since there is nothing to batch.
        """
        actionable = [i for i in items if i.ordered]
        if len(actionable) < 2:
            return ReturnFlow.SEQUENTIAL
        boxes = find_all(self.page, self.s("actions", "batch_item_checkbox"), timeout=4000)
        if boxes is not None and boxes.count() >= 2:
            log.info("Amazon batch flow detected (%d item checkboxes).", boxes.count())
            return ReturnFlow.BATCH
        return ReturnFlow.SEQUENTIAL

    # ---------------------------------------------------------------- ordering

    def open_order(self, order_id: str) -> bool:
        url = self.sel["urls"]["order_search"].format(order_id=order_id)
        self.session.goto(url)
        self.human.scroll(200)
        card = find(self.page, self.s("orders_page", "order_card"), timeout=8000)
        if card is None:
            log.warning("No Amazon order card matched %s", order_id)
            return False
        return True

    def _open_return_wizard(self) -> bool:
        entry = find(self.page, self.s("actions", "return_entry"), timeout=6000)
        if entry is None:
            return False
        self.human.click(entry)
        self.page.wait_for_load_state("domcontentloaded")
        self.human.think()
        self.session.assert_not_challenged()
        return True

    def _checkbox_for(self, item: LineItem) -> Optional[Locator]:
        """Find the wizard checkbox belonging to one ASIN."""
        if item.sku:
            anchored = self.page.locator(f"a[href*='{item.sku}']").first
            try:
                if anchored.count() > 0:
                    box = anchored.locator(
                        "xpath=ancestor::*[.//input[@type='checkbox']][1]"
                        "//input[@type='checkbox']"
                    ).first
                    if box.count() > 0:
                        return box
            except Exception:  # noqa: BLE001
                pass
        return None

    def classify_status(self, block_text: str) -> Optional[ReturnStatus]:
        haystack = block_text.lower()
        markers = self.sel.get("status_markers", {})

        def hit(key: str) -> bool:
            return any(m.lower() in haystack for m in markers.get(key, []))

        if hit("already_refunded"):
            return ReturnStatus.ALREADY_REFUNDED
        if hit("return_in_progress"):
            return ReturnStatus.PLACED
        if hit("window_closed"):
            return ReturnStatus.OUT_OF_WINDOW
        if hit("not_delivered") and not hit("delivered"):
            return ReturnStatus.NOT_DELIVERED
        return None

    # ------------------------------------------------------------- main entry

    def process_order(
        self,
        items: Sequence[LineItem],
        *,
        dry_run: bool = True,
    ) -> dict[str, Outcome]:
        outcomes: dict[str, Outcome] = {}
        actionable = [i for i in items if i.ordered]
        if not actionable:
            return outcomes

        order_id = actionable[0].order_id
        if not self.open_order(order_id):
            shot = self.session.screenshot(f"{order_id}-order-not-found")
            for item in actionable:
                outcomes[item.sku] = Outcome(
                    status=ReturnStatus.ITEM_NOT_FOUND,
                    log=f"Order {order_id} could not be found in this Amazon account.",
                    screenshots=[shot] if shot else [],
                    dry_run=dry_run,
                ).stamp()
            return outcomes

        if not self._open_return_wizard():
            page_text = text_of(self.page.locator("body"))
            classified = self.classify_status(page_text)
            shot = self.session.screenshot(f"{order_id}-no-return-entry")
            status = classified or ReturnStatus.SUPPORT_NEEDED
            note = (
                "No 'Return or replace items' entry point was available for this "
                "order."
            )
            for item in actionable:
                outcomes[item.sku] = Outcome(
                    status=status,
                    log=f"{note} Page status read as: {status.value}.",
                    screenshots=[shot] if shot else [],
                    dry_run=dry_run,
                ).stamp()
            return outcomes

        flow = self.detect_flow(actionable)
        progress.publish("flow_detected", order_id=order_id, flow=flow.value)
        if flow is ReturnFlow.BATCH:
            outcomes = self._process_batch(actionable, dry_run=dry_run)
            # The batch wizard covers every item in one pass, so results only
            # exist once it completes - announce them together at the end.
            for item in actionable:
                if item.sku in outcomes:
                    _publish_item_finished(order_id, item, outcomes[item.sku])
            return outcomes
        return self._process_sequential(actionable, order_id, dry_run=dry_run)

    # ------------------------------------------------------------- batch path

    def _process_batch(self, items: Sequence[LineItem], *, dry_run: bool) -> dict[str, Outcome]:
        """One wizard pass covering several line items."""
        outcomes: dict[str, Outcome] = {}
        selected: list[LineItem] = []

        for item in items:
            box = self._checkbox_for(item)
            if box is None:
                outcomes[item.sku] = Outcome(
                    status=ReturnStatus.ITEM_NOT_FOUND,
                    log=(
                        f"ASIN {item.sku} was not offered in the batch return "
                        "wizard; it may be outside its window or non-returnable."
                    ),
                    dry_run=dry_run,
                ).stamp()
                continue
            try:
                self.human.click(box)
                selected.append(item)
                self.human.micro()
            except Exception as exc:  # noqa: BLE001
                outcomes[item.sku] = Outcome(
                    status=ReturnStatus.FAILED,
                    log=f"Could not select this item in the batch wizard: {exc}",
                    dry_run=dry_run,
                ).stamp()

        if not selected:
            return outcomes

        # Amazon asks for a reason per selected item.
        reasons: dict[str, str] = {}
        dropdowns = find_all(self.page, self.s("actions", "reason_dropdown"), timeout=5000)
        count = dropdowns.count() if dropdowns is not None else 0
        for idx, item in enumerate(selected):
            if dropdowns is not None and idx < count:
                reasons[item.sku] = self._select_reason(dropdowns.nth(idx))
            else:
                reasons[item.sku] = ""

        self._select_refund_and_pickup()

        for _ in range(4):
            nxt = find(self.page, self.s("actions", "continue_button"), timeout=4000)
            if nxt is None:
                break
            self.human.click(nxt)
            self.session.assert_not_challenged()
            self.human.think(0.8)

        confirm = find(self.page, self.s("actions", "confirm_button"), timeout=6000)

        if dry_run:
            shot = self.session.screenshot("amazon-batch-dryrun-preconfirm")
            for item in selected:
                outcomes[item.sku] = Outcome(
                    status=ReturnStatus.PLACED if confirm else ReturnStatus.FAILED,
                    log=(
                        "DRY RUN: batch flow covered this item and "
                        f"{'reached' if confirm else 'did not reach'} the submit "
                        f"step. Reason: {reasons.get(item.sku) or 'none'}. "
                        "Nothing was submitted."
                    ),
                    screenshots=[shot] if shot else [],
                    dry_run=True,
                ).stamp()
            return outcomes

        if confirm is None:
            shot = self.session.screenshot("amazon-batch-no-confirm")
            for item in selected:
                outcomes[item.sku] = Outcome(
                    status=ReturnStatus.FAILED,
                    log="Batch flow never reached a submit button; nothing submitted.",
                    screenshots=[shot] if shot else [],
                ).stamp()
            return outcomes

        self.human.click(confirm)
        self.page.wait_for_load_state("domcontentloaded")
        self.human.think(1.4)
        self.session.assert_not_challenged()

        body = text_of(self.page.locator("body"))
        success = find(self.page, self.s("confirmation", "success_markers"), timeout=8000)
        shared_id = first_group(self.sel["confirmation"]["return_id_patterns"], body)
        shot = self.session.screenshot("amazon-batch-confirmation")

        for item in selected:
            if success is None and not shared_id:
                outcomes[item.sku] = Outcome(
                    status=ReturnStatus.FAILED,
                    log=(
                        "Batch submit was clicked but no confirmation appeared. "
                        "Verify by hand before retrying."
                    ),
                    screenshots=[shot] if shot else [],
                ).stamp()
            else:
                outcomes[item.sku] = Outcome(
                    status=ReturnStatus.PLACED,
                    return_id=shared_id or "N/A",
                    refund_amount=parse_amount(body),
                    log=(
                        "Return placed via Amazon's batch flow covering "
                        f"{len(selected)} item(s). Reason: "
                        f"{reasons.get(item.sku) or 'default'}. Return ID "
                        f"{shared_id or 'not shown'} is shared across the batch."
                    ),
                    screenshots=[shot] if shot else [],
                ).stamp()
        return outcomes

    # -------------------------------------------------------- sequential path

    def _process_sequential(
        self,
        items: Sequence[LineItem],
        order_id: str,
        *,
        dry_run: bool,
    ) -> dict[str, Outcome]:
        outcomes: dict[str, Outcome] = {}
        for index, item in enumerate(items):
            if index:
                self.human.between_items()
                self.human.browse_idle()
                if not (self.open_order(order_id) and self._open_return_wizard()):
                    outcomes[item.sku] = Outcome(
                        status=ReturnStatus.FAILED,
                        log=f"Could not reopen the return wizard for item {index + 1}.",
                        dry_run=dry_run,
                    ).stamp()
                    _publish_item_finished(order_id, item, outcomes[item.sku])
                    continue
            progress.publish(
                "item_started",
                order_id=order_id,
                sku=item.sku,
                title=item.title_hint,
                index=item.item_index,
            )
            try:
                outcomes[item.sku] = self._process_one(item, dry_run=dry_run)
            except AgentAbort:
                raise
            except Exception as exc:  # noqa: BLE001
                shot = self.session.screenshot(f"{order_id}-{item.sku}-error")
                log.exception("Amazon item %s failed", item.label)
                outcomes[item.sku] = Outcome(
                    status=ReturnStatus.FAILED,
                    log=f"Unexpected error while processing this item: {exc}",
                    screenshots=[shot] if shot else [],
                    dry_run=dry_run,
                ).stamp()
            _publish_item_finished(order_id, item, outcomes[item.sku])
        return outcomes

    def _process_one(self, item: LineItem, *, dry_run: bool) -> Outcome:
        box = self._checkbox_for(item)
        if box is not None:
            self.human.click(box)
        else:
            anchored = self.page.locator(f"a[href*='{item.sku}']").first
            if anchored.count() == 0:
                shot = self.session.screenshot(f"{item.order_id}-{item.sku}-not-matched")
                return Outcome(
                    status=ReturnStatus.ITEM_NOT_FOUND,
                    log=(
                        f"ASIN {item.sku} was not present in the return wizard for "
                        f"order {item.order_id}."
                    ),
                    screenshots=[shot] if shot else [],
                    dry_run=dry_run,
                ).stamp()

        dropdown = find(self.page, self.s("actions", "reason_dropdown"), timeout=5000)
        reason = self._select_reason(dropdown) if dropdown is not None else ""
        self._select_refund_and_pickup()

        for _ in range(4):
            nxt = find(self.page, self.s("actions", "continue_button"), timeout=4000)
            if nxt is None:
                break
            self.human.click(nxt)
            self.session.assert_not_challenged()
            self.human.think(0.8)

        confirm = find(self.page, self.s("actions", "confirm_button"), timeout=6000)

        if dry_run:
            shot = self.session.screenshot(f"{item.order_id}-{item.sku}-dryrun-preconfirm")
            return Outcome(
                status=ReturnStatus.PLACED if confirm else ReturnStatus.FAILED,
                log=(
                    "DRY RUN: walked Amazon's sequential return flow and "
                    f"{'reached' if confirm else 'did not reach'} the submit step. "
                    f"Reason: {reason or 'none'}. Nothing was submitted."
                ),
                screenshots=[shot] if shot else [],
                dry_run=True,
            ).stamp()

        if confirm is None:
            shot = self.session.screenshot(f"{item.order_id}-{item.sku}-no-confirm")
            return Outcome(
                status=ReturnStatus.FAILED,
                log="Never reached a submit button; nothing was submitted.",
                screenshots=[shot] if shot else [],
            ).stamp()

        self.human.click(confirm)
        self.page.wait_for_load_state("domcontentloaded")
        self.human.think(1.4)
        self.session.assert_not_challenged()

        body = text_of(self.page.locator("body"))
        success = find(self.page, self.s("confirmation", "success_markers"), timeout=8000)
        return_id = first_group(self.sel["confirmation"]["return_id_patterns"], body)
        shot = self.session.screenshot(f"{item.order_id}-{item.sku}-confirmation")

        if success is None and not return_id:
            return Outcome(
                status=ReturnStatus.FAILED,
                log=(
                    "Submit was clicked but no confirmation appeared. Verify by "
                    "hand before retrying so the return is not raised twice."
                ),
                screenshots=[shot] if shot else [],
            ).stamp()

        return Outcome(
            status=ReturnStatus.PLACED,
            return_id=return_id or "N/A",
            refund_amount=parse_amount(body),
            log=f"Return placed on Amazon. Reason: {reason or 'default'}.",
            screenshots=[shot] if shot else [],
        ).stamp()

    def _select_refund_and_pickup(self) -> None:
        """Take the default refund destination, then choose pickup if offered.

        Every element is optional - Amazon's wizard varies by item, seller and
        return method - so a missing one is not an error. The address already on
        the order is confirmed rather than changed.
        """
        for key in ("refund_mode", "pickup_option", "pickup_slot", "pickup_address_confirm"):
            element = find(self.page, self.s("actions", key), timeout=2200)
            if element is None:
                continue
            try:
                self.human.click(element)
            except Exception:  # noqa: BLE001 - already selected, or not clickable
                continue

    def _select_reason(self, dropdown: Locator) -> str:
        try:
            if (dropdown.evaluate("el => el.tagName") or "").lower() == "select":
                labels = dropdown.locator("option").all_inner_texts()
                for preferred in self.sel.get("preferred_reasons", []):
                    for label in labels:
                        if preferred.lower() in label.lower():
                            dropdown.select_option(label=label)
                            self.human.micro()
                            return label.strip()
                if len(labels) > 1:
                    dropdown.select_option(index=1)
                    return labels[1].strip()
                return ""
        except Exception:  # noqa: BLE001
            pass

        self.human.click(dropdown)
        options = find_all(self.page, self.s("actions", "reason_option"), timeout=5000)
        if options is None:
            return ""
        try:
            labels = options.all_inner_texts()
        except Exception:  # noqa: BLE001
            return ""
        for preferred in self.sel.get("preferred_reasons", []):
            for idx, label in enumerate(labels):
                if preferred.lower() in label.lower():
                    self.human.click(options.nth(idx))
                    return label.strip()
        if labels:
            self.human.click(options.first)
            return labels[0].strip()
        return ""
