import logging

from odoo import models

_logger = logging.getLogger(__name__)

LOG_SOURCE = "order_cancel"


class ShopifyCancelSync(models.AbstractModel):
    """Order cancellation sync. Each direction is independently togglable."""

    _name = "shopify.cancel.sync"
    _description = "Shopify Order Cancellation Sync"

    # ------------------------------------------------------------------
    # Shopify -> Odoo
    # ------------------------------------------------------------------
    def process_order_cancel(self, job):
        """Shopify -> Odoo: cancel the Odoo sale order for a cancelled order.

        Job payload: {"order_id": <shopify id>, "topic": "orders/cancelled", ...}.

        Behaviour:
        - Respects `cancel_shopify_to_odoo_enabled` (default ON when unset);
          when off, logs info and returns without raising.
        - Confirms the order via api.get_order(payload['order_id']) (defensive
          check on cancelled_at; the topic alone is a strong signal).
        - Finds the Odoo sale.order (pull-engine match rules). Not found ->
          log warning and RETURN (the order may never have been pulled).
        - Already cancelled -> log info, return (idempotent).
        - Cancels posted UNPAID invoices via button_cancel(); PAID invoices
          are left untouched with a credit-note warning.
        - Cancels the SO with shopify_cancel_origin='shopify' in context so
          the sale.order override stays silent (no ping-pong).
        - Raises on unexpected failure so the queue retries.
        """
        log = self.env["shopify.sync.log"]
        api = self.env["shopify.api.client"]

        payload = job.payload_dict()
        shopify_order_id = payload.get("order_id")
        ref = str(shopify_order_id) if shopify_order_id else None

        # Default-OFF gate: cancellation sync must be opted into in Settings
        # before any Shopify cancellation touches Odoo.
        if not self._enabled_explicit_on(
            api._param("cancel_shopify_to_odoo_enabled")
        ):
            log.log_event(
                "info",
                "Order cancel job %s skipped: cancel_shopify_to_odoo_enabled "
                "is off (Settings -> Shopify Ops)." % job.name,
                source=LOG_SOURCE,
                job=job,
                shopify_order_ref=ref,
            )
            return

        if not shopify_order_id:
            raise RuntimeError(
                "Job %s: payload has no 'order_id' — cannot process "
                "cancellation." % job.name
            )

        # Always fetch the current order; never trust the webhook payload.
        order = api.get_order(shopify_order_id)
        if not order:
            raise RuntimeError(
                "Job %s: Shopify order %s not found via API."
                % (job.name, shopify_order_id)
            )
        order_name = order.get("name") or str(shopify_order_id)

        def _log(level, message):
            log.log_event(
                level, message, source=LOG_SOURCE, job=job,
                shopify_order_ref=order_name,
            )

        if not order.get("cancelled_at"):
            _log(
                "info",
                "Order %s: API shows cancelled_at empty; proceeding anyway "
                "(the orders/cancelled topic is authoritative)." % order_name,
            )

        sale_order = self._find_sale_order(shopify_order_id, order.get("name"))
        if not sale_order:
            _log(
                "warning",
                "Order %s: no matching Odoo sale order — nothing to cancel "
                "(the order may never have been pulled)." % order_name,
            )
            return

        if sale_order.state == "cancel":
            _log(
                "info",
                "Order %s: sale order %s is already cancelled — idempotent "
                "no-op." % (order_name, sale_order.name),
            )
            return

        _log(
            "info",
            "Order %s: cancelling sale order %s (state %s)."
            % (order_name, sale_order.name, sale_order.state),
        )

        # Invoices first: posted UNPAID -> cancel; anything paid-like -> leave
        # and flag for a credit note (manual or refund sync).
        invoices = sale_order.invoice_ids.filtered(
            lambda m: m.move_type == "out_invoice" and m.state == "posted"
        )
        for move in invoices:
            if move.payment_state in ("paid", "in_payment", "partial"):
                _log(
                    "warning",
                    "Order %s: invoice %s is %s — left untouched; a credit "
                    "note is required (manual or refund sync)."
                    % (order_name, move.name, move.payment_state),
                )
                continue
            move.button_cancel()
            _log(
                "info",
                "Order %s: cancelled unpaid posted invoice %s."
                % (order_name, move.name),
            )

        # Cancel the SO. The context flag keeps the action_cancel override
        # from pushing the cancellation back to Shopify (no ping-pong).
        sale_order.with_context(shopify_cancel_origin="shopify").action_cancel()
        _log(
            "info",
            "Order %s: sale order %s cancelled (origin Shopify; no push-back)."
            % (order_name, sale_order.name),
        )

    # ------------------------------------------------------------------
    # config helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _enabled_explicit_on(value):
        """Explicit-on semantics: only 'True'/'true'/'1' count as enabled
        (unset means OFF)."""
        return str(value).strip().lower() in ("true", "1")

    # ------------------------------------------------------------------
    # matching helpers (same rules as the order pull engine)
    # ------------------------------------------------------------------
    def _find_sale_order(self, shopify_order_id, order_name):
        """sale.order by shopify_order_id, else client_order_ref/name with
        and without '#'. Empty recordset when none."""
        SaleOrder = self.env["sale.order"]
        sid = str(shopify_order_id)
        name = (order_name or "").strip()
        if not name:
            return SaleOrder.search([("shopify_order_id", "=", sid)], limit=1)
        clean = name.lstrip("#")
        candidates = [name, clean, "#" + clean]
        return SaleOrder.search(
            [
                "|",
                ("shopify_order_id", "=", sid),
                "|",
                ("client_order_ref", "in", candidates),
                ("name", "in", candidates),
            ],
            limit=1,
        )


class SaleOrder(models.Model):
    """Odoo → Shopify: push cancellations when the outbound setting is on."""

    _inherit = "sale.order"

    def action_cancel(self):
        # Cancel in Odoo FIRST; the Shopify push below must never block it.
        res = super().action_cancel()
        self._shopify_push_cancel()
        return res

    def _shopify_push_cancel(self):
        # Suppressed when the cancellation originated from Shopify.
        if self.env.context.get("shopify_cancel_origin") == "shopify":
            return
        orders = self.filtered(lambda o: o.shopify_order_id)
        if not orders:
            return

        sync = self.env["shopify.cancel.sync"]
        api = self.env["shopify.api.client"]
        if not sync._enabled_explicit_on(
            api._param("cancel_odoo_to_shopify_enabled")
        ):
            return

        log = self.env["shopify.sync.log"]
        for order in orders:
            ref = order.shopify_order_name or order.client_order_ref or order.name
            try:
                # Pre-check: Shopify refuses (422) to cancel orders that are
                # already cancelled, fulfilled, or partially fulfilled.
                remote = api.get_order(order.shopify_order_id)
                if remote.get("cancelled_at"):
                    log.log_event(
                        "info",
                        "Shopify order %s (id %s) is already cancelled — "
                        "nothing to push." % (ref, order.shopify_order_id),
                        source=LOG_SOURCE,
                        shopify_order_ref=ref,
                    )
                    continue
                fulfillment = remote.get("fulfillment_status")
                if fulfillment in ("fulfilled", "partial"):
                    log.log_event(
                        "warning",
                        "Cannot push cancellation of %s to Shopify: the order "
                        "is %s in Shopify, and fulfilled orders cannot be "
                        "cancelled via the API. Odoo is cancelled; handle the "
                        "Shopify side manually (return/refund flow)."
                        % (ref, fulfillment),
                        source=LOG_SOURCE,
                        shopify_order_ref=ref,
                    )
                    continue
                api.post("orders/%s/cancel.json" % order.shopify_order_id, {})
                log.log_event(
                    "info",
                    "Odoo cancellation of %s pushed to Shopify order id %s."
                    % (order.name, order.shopify_order_id),
                    source=LOG_SOURCE,
                    shopify_order_ref=ref,
                )
            except Exception as exc:
                # Shopify errors never block the Odoo cancellation.
                log.log_event(
                    "error",
                    "Failed to cancel Shopify order id %s after Odoo "
                    "cancellation of %s: %s. Shopify and Odoo may now "
                    "disagree — cancel manually in Shopify."
                    % (order.shopify_order_id, order.name, exc),
                    source=LOG_SOURCE,
                    shopify_order_ref=ref,
                )


class SaleOrderCancelGuard(models.AbstractModel):
    _name = "shopify.cancel.outbound"
    _description = "Odoo → Shopify cancellation push"

    # After a normal (non-Shopify-originated) cancellation of a sale order
    # that has shopify_order_id set, if cancel_odoo_to_shopify_enabled is
    # on, POST orders/{id}/cancel.json. Off by default so warehouse
    # cancel/reconfirm automations do not cancel live Shopify orders.

