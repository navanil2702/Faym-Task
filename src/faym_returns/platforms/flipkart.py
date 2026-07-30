"""Flipkart adapter - sequential per-line-item return flow.

Flipkart has no multi-item return wizard: each SKU on an order carries its own
Return control and its own window, so the micro-flow is repeated once per line
item. Every item is attempted independently and one item's failure never stops
the rest of the order.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

from playwright.sync_api import Locator

from .. import progress
from ..models import (
    AgentAbort,
    LineItem,
    Outcome,
    Platform,
    ReturnFlow,
    ReturnStatus,
)
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


class FlipkartAdapter(PlatformAdapter):
    platform = Platform.FLIPKART
    selector_key = "flipkart"

    # ------------------------------------------------------------------- login

    def is_logged_in(self) -> bool:
        return find(self.page, self.s("logged_in"), timeout=3000) is not None

    def ensure_logged_in(self) -> None:
        self.session.goto(self.sel["urls"]["orders"])
        if self.is_logged_in():
            log.info("Flipkart session restored from the saved profile.")
            progress.publish("login_ok", platform=self.platform.value, restored=True)
            return
        self.log_in(self.sel["urls"]["login"])
        self.session.goto(self.sel["urls"]["orders"])
        if not self.is_logged_in():
            raise AgentAbort("Flipkart still appears signed out after the manual login step.")

    def detect_flow(self, items: Sequence[LineItem]) -> ReturnFlow:
        """Flipkart is always sequential - one return micro-flow per SKU."""
        return ReturnFlow.SEQUENTIAL

    # ----------------------------------------------------------------- ordering

    def open_order(self, order_id: str) -> bool:
        """Navigate to the detail view for ``order_id``. False if not found."""
        url = self.sel["urls"]["order_search"].format(order_id=order_id)
        self.session.goto(url)
        self.human.scroll(220)

        card = find(self.page, self.s("orders_page", "order_card"), timeout=8000)
        if card is None:
            log.warning("No order card matched for %s", order_id)
            return False

        # Prefer a link that names the order id outright.
        link = self.page.locator(f"a[href*='{order_id}']").first
        try:
            if link.count() > 0:
                self.human.click(link)
            else:
                self.human.click(card)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not open order %s: %s", order_id, exc)
            return False

        self.page.wait_for_load_state("domcontentloaded")
        self.human.think()
        self.session.assert_not_challenged()
        return True

    def find_item_block(self, item: LineItem) -> Optional[Locator]:
        """Locate the on-page block for one SKU inside the open order.

        Matching is by ``pid`` first - it is the platform's own line-item
        identity and is unambiguous even when an order holds four near-identical
        shoulder bags. The product-title slug is only a fallback.
        """
        if item.sku:
            by_pid = self.page.locator(
                f"a[href*='pid={item.sku}'], a[href*='{item.sku}']"
            ).first
            try:
                if by_pid.count() > 0:
                    block = by_pid.locator(
                        "xpath=ancestor::div[.//button or .//a][1]"
                    ).first
                    return block if block.count() > 0 else by_pid
            except Exception:  # noqa: BLE001
                pass

        if item.title_hint:
            words = [w for w in item.title_hint.split() if len(w) > 3][:4]
            if words:
                try:
                    candidate = self.page.locator(
                        f"xpath=//*[contains(translate(., "
                        f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                        f"'abcdefghijklmnopqrstuvwxyz'), '{words[0].lower()}')]"
                    ).first
                    if candidate.count() > 0:
                        return candidate.locator("xpath=ancestor::div[.//button][1]").first
                except Exception:  # noqa: BLE001
                    pass
        return None

    def classify_status(self, block_text: str) -> Optional[ReturnStatus]:
        """Map the item's visible status copy onto an outcome, if decisive."""
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
        # "Delivered" must be checked last: an in-transit item may also carry
        # copy mentioning a delivery estimate.
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
        """Run the return micro-flow once per line item on this order."""
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
                    log=(
                        f"Order {order_id} could not be opened from the Flipkart "
                        "orders list. Verify the order id and that it belongs to "
                        "this account."
                    ),
                    screenshots=[shot] if shot else [],
                    dry_run=dry_run,
                ).stamp()
            return outcomes

        for index, item in enumerate(actionable):
            if index:
                # Space out consecutive returns on the same account.
                self.human.between_items()
                self.human.browse_idle()
                # Each micro-flow navigates away, so return to the order view.
                if not self.open_order(order_id):
                    outcomes[item.sku] = Outcome(
                        status=ReturnStatus.FAILED,
                        log=f"Lost the order view for {order_id} before item {index + 1}.",
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
                outcomes[item.sku] = self._process_item(item, dry_run=dry_run)
            except AgentAbort:
                raise
            except Exception as exc:  # noqa: BLE001 - isolate this item only
                shot = self.session.screenshot(f"{order_id}-{item.sku}-error")
                log.exception("Item %s failed", item.label)
                outcomes[item.sku] = Outcome(
                    status=ReturnStatus.FAILED,
                    log=f"Unexpected error while processing this item: {exc}",
                    screenshots=[shot] if shot else [],
                    dry_run=dry_run,
                ).stamp()
            _publish_item_finished(order_id, item, outcomes[item.sku])
        return outcomes

    def _process_item(self, item: LineItem, *, dry_run: bool) -> Outcome:
        block = self.find_item_block(item)
        if block is None:
            shot = self.session.screenshot(f"{item.order_id}-{item.sku}-not-matched")
            return Outcome(
                status=ReturnStatus.ITEM_NOT_FOUND,
                log=(
                    f"Could not match SKU {item.sku} to any item shown on order "
                    f"{item.order_id}. The order may list the item under a "
                    "different product id, or the link in the sheet may be wrong."
                ),
                screenshots=[shot] if shot else [],
                dry_run=dry_run,
            ).stamp()

        self.human.click(block, settle=False)
        self.human.think()
        block_text = text_of(block)
        page_text = text_of(self.page.locator("body"))

        # A terminal state visible on the page beats attempting anything.
        classified = self.classify_status(block_text)
        if classified is ReturnStatus.ALREADY_REFUNDED:
            refund_id = first_group(self.sel["confirmation"]["refund_id_patterns"], page_text)
            amount = parse_amount(block_text) or parse_amount(page_text)
            return Outcome(
                status=ReturnStatus.ALREADY_REFUNDED,
                return_id=refund_id or "N/A",
                refund_amount=amount,
                log=(
                    "Item was already cancelled and refunded on Flipkart; no new "
                    f"return was needed. Refund ID: {refund_id or 'not shown'}."
                ),
                dry_run=dry_run,
            ).stamp()

        if classified is ReturnStatus.NOT_DELIVERED:
            return Outcome(
                status=ReturnStatus.NOT_DELIVERED,
                log=(
                    "Item is not yet delivered, so a return cannot be raised. "
                    "Re-run once it has been delivered."
                ),
                dry_run=dry_run,
            ).stamp()

        if classified is ReturnStatus.PLACED:
            return_id = first_group(self.sel["confirmation"]["return_id_patterns"], page_text)
            return Outcome(
                status=ReturnStatus.PLACED,
                return_id=return_id or "N/A",
                refund_amount=parse_amount(block_text),
                log="A return was already in progress for this item on Flipkart.",
                dry_run=dry_run,
            ).stamp()

        if classified is ReturnStatus.OUT_OF_WINDOW:
            return Outcome(
                status=ReturnStatus.OUT_OF_WINDOW,
                log="Flipkart reports the return window for this item has closed.",
                dry_run=dry_run,
            ).stamp()

        return self._run_return_flow(item, block, dry_run=dry_run)

    def _run_return_flow(self, item: LineItem, block: Locator, *, dry_run: bool) -> Outcome:
        """The actual return wizard for a single delivered, returnable item."""
        return_button = find(self.page, self.s("actions", "return_button"), timeout=6000)

        if return_button is None:
            exchange = find(self.page, self.s("actions", "exchange_only"), timeout=2500)
            shot = self.session.screenshot(f"{item.order_id}-{item.sku}-no-return-button")
            detail = (
                "only an Exchange/Replace option is offered, not a return"
                if exchange is not None
                else "no return control was found on the item page"
            )
            return Outcome(
                status=ReturnStatus.SUPPORT_NEEDED,
                log=(
                    f"Support needed: {detail}. This usually means the item is "
                    "non-returnable or needs Flipkart chat support to raise the "
                    "request. Escalate to a human."
                ),
                screenshots=[shot] if shot else [],
                dry_run=dry_run,
            ).stamp()

        self.human.click(return_button)
        self.session.assert_not_challenged()

        reason = self._select_reason()
        self._maybe_add_comment()
        self._select_refund_mode()
        self._select_pickup_option()

        # Advance through however many intermediate Continue steps there are.
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
            reached = "reached the final confirm step" if confirm else "did not reach a confirm step"
            return Outcome(
                status=ReturnStatus.FAILED if confirm is None else ReturnStatus.PLACED,
                log=(
                    f"DRY RUN: walked the return flow for this item and {reached}. "
                    f"Reason selected: {reason or 'none available'}. Nothing was "
                    "submitted - re-run with --live to actually place the return."
                ),
                screenshots=[shot] if shot else [],
                dry_run=True,
            ).stamp()

        if confirm is None:
            shot = self.session.screenshot(f"{item.order_id}-{item.sku}-no-confirm")
            return Outcome(
                status=ReturnStatus.FAILED,
                log=(
                    "Walked the return flow but never reached a confirm button; "
                    "nothing was submitted. The flow layout may have changed."
                ),
                screenshots=[shot] if shot else [],
            ).stamp()

        self.human.click(confirm)
        self.page.wait_for_load_state("domcontentloaded")
        self.human.think(1.4)
        self.session.assert_not_challenged()

        confirmation_text = text_of(self.page.locator("body"))
        success = find(self.page, self.s("confirmation", "success_markers"), timeout=8000)
        return_id = first_group(self.sel["confirmation"]["return_id_patterns"], confirmation_text)
        amount = parse_amount(confirmation_text)
        shot = self.session.screenshot(f"{item.order_id}-{item.sku}-confirmation")

        if success is None and not return_id:
            return Outcome(
                status=ReturnStatus.FAILED,
                log=(
                    "Confirm was clicked but no success confirmation or return id "
                    "appeared. Verify by hand before retrying, so the return is "
                    "not raised twice."
                ),
                screenshots=[shot] if shot else [],
            ).stamp()

        return Outcome(
            status=ReturnStatus.PLACED,
            return_id=return_id or "N/A",
            refund_amount=amount,
            log=(
                f"Return placed on Flipkart. Reason: {reason or 'default'}. "
                f"Return ID: {return_id or 'not shown on confirmation'}."
            ),
            screenshots=[shot] if shot else [],
        ).stamp()

    # ------------------------------------------------------------ wizard steps

    def _select_reason(self) -> str:
        """Pick the first preferred reason the dropdown actually offers."""
        dropdown = find(self.page, self.s("actions", "reason_dropdown"), timeout=5000)
        if dropdown is None:
            return ""

        # Native <select> is handled directly.
        try:
            if (dropdown.evaluate("el => el.tagName") or "").lower() == "select":
                labels = dropdown.locator("option").all_inner_texts()
                for preferred in self.sel.get("preferred_reasons", []):
                    for label in labels:
                        if preferred.lower() in label.lower():
                            dropdown.select_option(label=label)
                            self.human.micro()
                            return label
                if len(labels) > 1:
                    dropdown.select_option(index=1)
                    return labels[1]
                return ""
        except Exception:  # noqa: BLE001 - not a native select, fall through
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
                    self._select_sub_reason()
                    return label.strip()

        if labels:
            self.human.click(options.first)
            self._select_sub_reason()
            return labels[0].strip()
        return ""

    def _select_sub_reason(self) -> None:
        sub = find(self.page, self.s("actions", "sub_reason_dropdown"), timeout=2500)
        if sub is None:
            return
        self.human.click(sub)
        options = find_all(self.page, self.s("actions", "reason_option"), timeout=3000)
        if options is not None and options.count() > 0:
            self.human.click(options.first)

    def _maybe_add_comment(self) -> None:
        comment = self.config.get("return_comment")
        if not comment:
            return
        box = find(self.page, self.s("actions", "comment_box"), timeout=2500)
        if box is not None:
            self.human.type_text(box, comment)

    def _select_refund_mode(self) -> None:
        """Take the default refund destination unless one is configured.

        Refund routing is a money decision, so the agent does not invent one: it
        selects the platform default (refund to the original payment source)
        rather than picking a bank account.
        """
        mode = find(self.page, self.s("actions", "refund_mode"), timeout=3000)
        if mode is not None:
            try:
                self.human.click(mode)
            except Exception:  # noqa: BLE001 - default is already selected
                pass

    def _select_pickup_option(self) -> None:
        """Choose the pickup option and confirm the address already on the order.

        Flipkart collects returned items, so the flow can ask where to collect
        from and occasionally for a slot. Each element is optional - many items
        skip straight to confirmation - so a missing one is not an error.

        The existing address is confirmed rather than re-entered or changed:
        redirecting a courier is not a decision to take unattended, and the
        address on the order is the one the customer already gave.
        """
        option = find(self.page, self.s("actions", "pickup_option"), timeout=2500)
        if option is not None:
            try:
                self.human.click(option)
            except Exception:  # noqa: BLE001 - already selected, or not clickable
                pass

        slot = find(self.page, self.s("actions", "pickup_slot"), timeout=2000)
        if slot is not None:
            try:
                # First offered slot: the earliest collection Flipkart proposes.
                self.human.click(slot)
            except Exception:  # noqa: BLE001
                pass

        confirm_address = find(
            self.page, self.s("actions", "pickup_address_confirm"), timeout=2500
        )
        if confirm_address is not None:
            try:
                self.human.click(confirm_address)
            except Exception:  # noqa: BLE001
                pass
