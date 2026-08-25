import logging

from odoo import fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    _inherit = "stock.picking"

    shopify_fulfillment_id = fields.Char(
        string="Shopify Fulfillment ID", copy=False, index=True
    )
    shopify_fulfillment_pushed = fields.Boolean(default=False, copy=False, index=True)

    def write(self, vals):
        res = super().write(vals)
        # Tracking added/changed AFTER the fulfillment was pushed: update it
        # on Shopify. Defensive — a sync problem must never break the write.
        if vals.get("carrier_tracking_ref"):
            for picking in self:
                try:
                    picking._maybe_push_tracking_update()
                except Exception:
                    _logger.exception(
                        "Shopify Ops: tracking update push failed for %s",
                        picking.name,
                    )
        return res

    def _maybe_push_tracking_update(self):
        """Push a tracking update when this picking already has a Shopify
        fulfillment. Silent no-op otherwise."""
        self.ensure_one()
        if (
            self.state != "done"
            or not self.shopify_fulfillment_pushed
            or not self.shopify_fulfillment_id
        ):
            return
        sync = self.env["shopify.fulfillment.sync"]
        if not sync._fulfillment_sync_enabled() or not sync._odoo_to_shopify_enabled():
            return
        sync._push_tracking_update(
            self.env["shopify.api.client"], self.env["shopify.sync.log"], self
        )

    def action_push_fulfillment_to_shopify(self):
        """Manual 'Push to Shopify' button on the picking form.

        Runs the same single-picking flow the cron uses, but surfaces the
        outcome immediately: success notification, or a dialog pointing at
        the sync log when the push could not complete. When the fulfillment
        was already pushed, the button instead updates the tracking number
        on the existing Shopify fulfillment."""
        self.ensure_one()
        if self.state != "done":
            raise UserError("Only done deliveries can be pushed to Shopify.")
        sync = self.env["shopify.fulfillment.sync"]
        if not sync._odoo_to_shopify_enabled():
            raise UserError(
                "Fulfillment push is off. Set Fulfillment sync direction to "
                "Odoo -> Shopify or Two-way in Settings -> Shopify Ops."
            )
        client = self.env["shopify.api.client"]
        log = self.env["shopify.sync.log"]
        if self.shopify_fulfillment_pushed:
            if not (self.carrier_tracking_ref or "").strip():
                raise UserError(
                    "This delivery was already pushed (Shopify fulfillment %s) "
                    "and has no tracking number to update."
                    % (self.shopify_fulfillment_id or "?")
                )
            sync._push_tracking_update(client, log, self)
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Shopify fulfillment",
                    "message": "Tracking updated on Shopify fulfillment %s."
                    % self.shopify_fulfillment_id,
                    "type": "success",
                    "sticky": False,
                },
            }
        sync._push_one_fulfillment(client, log, self)
        if not self.shopify_fulfillment_pushed:
            raise UserError(
                "Push did not complete. The reason is in Shopify Ops > Sync Logs "
                "(source: fulfillment) for picking %s — most commonly: no matching "
                "Shopify order, no open fulfillment order, or SKU not found on it."
                % self.name
            )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Shopify fulfillment",
                "message": "Fulfillment %s pushed to Shopify."
                % self.shopify_fulfillment_id,
                "type": "success",
                "sticky": False,
            },
        }


class ShopifyFulfillmentSync(models.AbstractModel):
    _name = "shopify.fulfillment.sync"
    _description = "Syncs Odoo deliveries with Shopify fulfillments"

    # --- helpers ----------------------------------------------------------
    def _fulfillment_sync_enabled(self):
        raw = self.env["shopify.api.client"]._param("fulfillment_sync_enabled")
        return raw is True or str(raw or "").strip().lower() in ("true", "1", "on")

    def _fulfillment_sync_direction(self):
        raw = self.env["shopify.api.client"]._param("fulfillment_sync_direction")
        return (raw or "odoo_to_shopify").strip().lower()

    def _odoo_to_shopify_enabled(self):
        if not self._fulfillment_sync_enabled():
            return False
        return self._fulfillment_sync_direction() in (
            "odoo_to_shopify",
            "two_way",
        )

    def _shopify_to_odoo_enabled(self):
        if not self._fulfillment_sync_enabled():
            return False
        return self._fulfillment_sync_direction() in (
            "shopify_to_odoo",
            "two_way",
        )

    def order_needs_fulfillment_pull(self, sale_order, order):
        """True when Shopify shows fulfillment data Odoo should mirror."""
        if not self._shopify_to_odoo_enabled() or not sale_order or not order:
            return False
        status = (order.get("fulfillment_status") or "").strip().lower()
        if status not in ("fulfilled", "partial"):
            return False
        pickings = sale_order.picking_ids.filtered(
            lambda p: p.picking_type_code == "outgoing" and p.state != "cancel"
        )
        if not pickings:
            return False
        open_pickings = pickings.filtered(lambda p: p.state != "done")
        if open_pickings:
            return True
        tracking = self._tracking_from_fulfillments(order.get("fulfillments") or [])
        if not tracking:
            return False
        return any(
            (p.carrier_tracking_ref or "").strip() != tracking
            for p in pickings.filtered("shopify_fulfillment_pushed")
        )

    def _tracking_from_fulfillments(self, fulfillments):
        for fulfillment in fulfillments or []:
            if not isinstance(fulfillment, dict):
                continue
            tracking = (fulfillment.get("tracking_number") or "").strip()
            if tracking:
                return tracking
            numbers = fulfillment.get("tracking_numbers") or []
            if numbers:
                return str(numbers[0]).strip()
        return ""

    def _notify_customer(self):
        """fulfillment_notify_customer setting (default OFF): whether Shopify
        emails the customer a shipping confirmation for pushed fulfillments
        and tracking updates."""
        raw = self.env["shopify.api.client"]._param("fulfillment_notify_customer")
        return str(raw or "").strip().lower() in ("true", "1")

    def _resolve_shopify_order(self, client, so):
        """Return (shopify_order_id, order_name) or (None, None) when no match."""
        if so.shopify_order_id:
            return so.shopify_order_id, so.shopify_order_name or so.shopify_order_id

        match_field = (
            client._param("order_match_field", "client_order_ref") or "client_order_ref"
        )
        if match_field == "shopify_order_id":
            # Nothing stored on the SO -> no way to match.
            return None, None
        if match_field == "name":
            lookup = so.name
        else:  # client_order_ref (default)
            lookup = so.client_order_ref or so.name
        if not lookup:
            return None, None

        order = client.find_order_by_name(lookup)
        if not order:
            return None, None
        order_id = str(order.get("id") or "")
        return (order_id or None), order.get("name")

    def _open_fulfillment_orders(self, client, order_id):
        """Fulfillment orders with status 'open', falling back to 'in_progress'."""
        data = client.get(f"orders/{order_id}/fulfillment_orders.json")
        fos = data.get("fulfillment_orders") or []
        open_fos = [fo for fo in fos if fo.get("status") == "open"]
        if not open_fos:
            open_fos = [fo for fo in fos if fo.get("status") == "in_progress"]
        return open_fos

    def _sku_to_fo_line_items(self, client, order_id, fulfillment_orders):
        """Map SKU -> list of {'fo_id', 'foi_id', 'remaining'} across the FO list.

        'remaining' is the fulfillable quantity Shopify reports for the FO
        line item (None when not exposed). FO line items usually expose
        `sku`; when they don't, fall back to the order's line_items (FO line
        item `line_item_id` -> order line item id).
        """
        mapping = {}
        missing_sku = False

        def _add(sku, fo, li):
            mapping.setdefault(sku, []).append(
                {
                    "fo_id": fo.get("id"),
                    "foi_id": li.get("id"),
                    "remaining": li.get("fulfillable_quantity") if li.get("fulfillable_quantity") is not None else li.get("quantity"),
                }
            )

        for fo in fulfillment_orders:
            for li in fo.get("line_items") or []:
                sku = li.get("sku")
                if not sku:
                    missing_sku = True
                    continue
                _add(sku, fo, li)
        if missing_sku:
            order = client.get_order(order_id)
            li_sku = {
                li.get("id"): li.get("sku") for li in order.get("line_items") or []
            }
            for fo in fulfillment_orders:
                for li in fo.get("line_items") or []:
                    if li.get("sku"):
                        continue
                    sku = li_sku.get(li.get("line_item_id"))
                    if sku:
                        _add(sku, fo, li)
        return mapping

    def _delivered_qty_by_sku(self, picking):
        """Group done quantities (stock.move.quantity) by product SKU."""
        delivered = {}
        for move in picking.move_ids:
            sku = (move.product_id.default_code or "").strip()
            if not sku:
                continue
            qty = move.quantity or 0.0
            if qty <= 0:
                continue
            delivered[sku] = delivered.get(sku, 0.0) + qty
        return delivered

    # --- cron entry point ---------------------------------------------------
    def cron_push_fulfillments(self, limit=50):
        """Sync fulfillments per configured direction."""
        if not self._fulfillment_sync_enabled():
            self.env["shopify.sync.log"].log_event(
                "info",
                "Fulfillment sync skipped: fulfillment_sync_enabled is off.",
                source="fulfillment",
            )
            return
        if self._odoo_to_shopify_enabled():
            self._cron_push_fulfillments(limit=limit)

    def _cron_push_fulfillments(self, limit=50):
        """Create Shopify fulfillments for done Odoo deliveries."""
        log = self.env["shopify.sync.log"]
        client = self.env["shopify.api.client"]
        pickings = self.env["stock.picking"].search(
            [
                ("state", "=", "done"),
                ("sale_id", "!=", False),
                ("shopify_fulfillment_pushed", "=", False),
                # Only Shopify-pulled orders — old/local Odoo orders never
                # have a Shopify counterpart to fulfill against.
                ("sale_id.shopify_order_id", "!=", False),
            ],
            order="date_done, id",
            limit=limit,
        )
        for picking in pickings:
            try:
                self._push_one_fulfillment(client, log, picking)
            except Exception as exc:  # keep the cron alive on bad records
                log.log_event(
                    "error",
                    f"Fulfillment push failed for picking {picking.name}: {exc}",
                    source="fulfillment",
                    shopify_order_ref=picking.sale_id.shopify_order_name or None,
                )

    def _push_one_fulfillment(self, client, log, picking):
        so = picking.sale_id
        order_id, order_name = self._resolve_shopify_order(client, so)
        if not order_id:
            log.log_event(
                "warning",
                f"No Shopify order match for sale order {so.name}; "
                f"skipping picking {picking.name}.",
                source="fulfillment",
                shopify_order_ref=so.shopify_order_name or None,
            )
            return
        order_ref = order_name or f"id:{order_id}"

        fulfillment_orders = self._open_fulfillment_orders(client, order_id)
        if not fulfillment_orders:
            log.log_event(
                "warning",
                f"No open/in-progress fulfillment orders on Shopify order "
                f"{order_ref} for picking {picking.name}.",
                source="fulfillment",
                shopify_order_ref=order_ref,
            )
            return

        sku_map = self._sku_to_fo_line_items(client, order_id, fulfillment_orders)
        delivered = self._delivered_qty_by_sku(picking)

        # Group FO line items under their own fulfillment_order_id,
        # distributing each SKU's delivered quantity across its FO lines
        # without exceeding the fulfillable quantity Shopify reports.
        groups = {}
        unmatched = []
        over_delivery = []
        for sku, qty in delivered.items():
            qty_int = int(qty)
            if qty_int <= 0:
                continue
            entries = sku_map.get(sku)
            if not entries:
                unmatched.append(sku)
                continue
            left = qty_int
            for entry in entries:
                if left <= 0:
                    break
                remaining = entry.get("remaining")
                take = left if remaining is None else min(int(remaining), left)
                if take <= 0:
                    continue
                groups.setdefault(entry["fo_id"], []).append(
                    {"id": entry["foi_id"], "quantity": take}
                )
                left -= take
            if left > 0:
                # Delivered more than Shopify still considers fulfillable —
                # pushing the excess would be rejected (422). Log it; the
                # fulfillable part is pushed normally.
                over_delivery.append("%s (+ %s not fulfillable)" % (sku, left))

        if over_delivery:
            log.log_event(
                "warning",
                f"Picking {picking.name}: delivered quantity exceeds the "
                f"fulfillable quantity on Shopify order {order_ref} for: "
                f"{', '.join(over_delivery)}. Only the fulfillable part was "
                "pushed; check the order in Shopify.",
                source="fulfillment",
                shopify_order_ref=order_ref,
            )

        if unmatched:
            log.log_event(
                "warning",
                f"Picking {picking.name}: SKUs not found among open fulfillment "
                f"orders of {order_ref}: {', '.join(sorted(unmatched))}.",
                source="fulfillment",
                shopify_order_ref=order_ref,
            )
        if not groups:
            already_fulfilled = bool(over_delivery or unmatched)
            if already_fulfilled:
                picking.shopify_fulfillment_pushed = True
                log.log_event(
                    "warning",
                    f"Picking {picking.name}: nothing left to fulfill on "
                    f"Shopify order {order_ref} (already fulfilled on "
                    "Shopify side). Marking as pushed to stop retries.",
                    source="fulfillment",
                    shopify_order_ref=order_ref,
                )
            else:
                log.log_event(
                    "warning",
                    f"Picking {picking.name}: nothing to fulfill on Shopify "
                    f"order {order_ref}; leaving pushed flag unset.",
                    source="fulfillment",
                    shopify_order_ref=order_ref,
                )
            return

        fulfillment = {
            "line_items_by_fulfillment_order": [
                {
                    "fulfillment_order_id": fo_id,
                    "fulfillment_order_line_items": items,
                }
                for fo_id, items in groups.items()
            ],
            "notify_customer": self._notify_customer(),
        }
        tracking_number = (picking.carrier_tracking_ref or "").strip()
        if tracking_number:
            fulfillment["tracking_info"] = {
                "number": tracking_number,
                "company": picking.carrier_id.name if picking.carrier_id else None,
            }
        else:
            log.log_event(
                "warning",
                f"Picking {picking.name}: no tracking number set — pushing "
                f"the fulfillment for {order_ref} without tracking info. "
                "Set the tracking reference on the delivery before validating "
                "to have it sent to Shopify.",
                source="fulfillment",
                shopify_order_ref=order_ref,
            )

        response = client.post("fulfillments.json", {"fulfillment": fulfillment})
        fid = (response.get("fulfillment") or {}).get("id")
        if not fid:
            raise ValueError(
                f"Shopify returned no fulfillment id for picking {picking.name}."
            )

        picking.shopify_fulfillment_id = str(fid)
        picking.shopify_fulfillment_pushed = True
        log.log_event(
            "info",
            f"Created Shopify fulfillment {fid} for picking {picking.name} "
            f"on order {order_ref}.",
            source="fulfillment",
            shopify_order_ref=order_ref,
        )

    # --- Shopify -> Odoo --------------------------------------------------
    def process_fulfillment_pull(self, job):
        """Apply Shopify fulfillments onto linked Odoo deliveries."""
        if not self._shopify_to_odoo_enabled():
            return
        log = self.env["shopify.sync.log"]
        client = self.env["shopify.api.client"]
        pull = self.env["shopify.order.pull.engine"]
        payload = job.payload_dict()
        shopify_order_id = payload.get("order_id")
        if not shopify_order_id:
            raise RuntimeError(
                "Job %s: fulfillment pull has no order_id." % job.name
            )
        order = client.get_order(shopify_order_id)
        if not order:
            raise RuntimeError(
                "Job %s: Shopify order %s not found."
                % (job.name, shopify_order_id)
            )
        order_name = order.get("name") or str(shopify_order_id)

        def _log(level, message):
            log.log_event(
                level,
                message,
                source="fulfillment",
                job=job,
                shopify_order_ref=order_name,
            )

        if pull.skip_order_before_cutoff(order, _log):
            return
        sale_order = pull._find_existing_order(shopify_order_id, order.get("name"))
        if not sale_order:
            _log(
                "warning",
                "Fulfillment pull skipped: no Odoo sale order for Shopify "
                "order %s." % order_name,
            )
            return
        fulfillments = []
        raw = payload.get("raw_fulfillment")
        if isinstance(raw, dict) and raw.get("id"):
            fulfillments = [raw]
        else:
            fulfillments = order.get("fulfillments") or client.get_order_fulfillments(
                shopify_order_id
            )
        if not fulfillments:
            _log(
                "info",
                "Order %s: no Shopify fulfillments to pull." % order_name,
            )
            return
        applied = 0
        for fulfillment in fulfillments:
            if self._apply_shopify_fulfillment(
                sale_order, order, fulfillment, _log, order_name
            ):
                applied += 1
        if applied:
            _log(
                "info",
                "Order %s: applied %d Shopify fulfillment(s) in Odoo."
                % (order_name, applied),
            )

    def _apply_shopify_fulfillment(
        self, sale_order, order, fulfillment, _log, order_name
    ):
        """Copy tracking / fulfillment id onto an Odoo delivery; validate when safe."""
        if not isinstance(fulfillment, dict):
            return False
        fid = str(fulfillment.get("id") or "").strip()
        tracking = self._tracking_from_fulfillments([fulfillment])
        pickings = sale_order.picking_ids.filtered(
            lambda p: p.picking_type_code == "outgoing" and p.state != "cancel"
        )
        if not pickings:
            _log(
                "warning",
                "Order %s: Shopify fulfillment %s has no outgoing Odoo "
                "delivery to update."
                % (order_name, fid or "?"),
            )
            return False
        picking = pickings.filtered(lambda p: not p.shopify_fulfillment_pushed)[:1]
        if not picking:
            picking = pickings.filtered(lambda p: p.state != "done")[:1]
        if not picking:
            picking = pickings.sorted("id", reverse=True)[:1]
        picking = picking[:1]
        vals = {}
        if fid:
            vals["shopify_fulfillment_id"] = fid
            vals["shopify_fulfillment_pushed"] = True
        if tracking and (picking.carrier_tracking_ref or "").strip() != tracking:
            vals["carrier_tracking_ref"] = tracking
        if vals:
            picking.with_context(shopify_sync_origin="shopify").write(vals)
        status = (order.get("fulfillment_status") or "").lower()
        if picking.state != "done" and status == "fulfilled":
            if picking.state == "assigned":
                picking.with_context(shopify_sync_origin="shopify").button_validate()
                _log(
                    "info",
                    "Order %s: validated Odoo delivery %s from Shopify "
                    "fulfillment %s."
                    % (order_name, picking.name, fid or "?"),
                )
            else:
                _log(
                    "warning",
                    "Order %s: Shopify is fulfilled but Odoo delivery %s "
                    "is %s — set to Ready and retry, or validate manually."
                    % (order_name, picking.name, picking.state),
                )
        elif picking.state == "done":
            _log(
                "info",
                "Order %s: updated delivery %s from Shopify fulfillment %s."
                % (order_name, picking.name, fid or "?"),
            )
        return True

    def _push_tracking_update(self, client, log, picking):
        """Update tracking on an EXISTING Shopify fulfillment (tracking was
        added to the picking after it was pushed)."""
        tracking_number = (picking.carrier_tracking_ref or "").strip()
        if not tracking_number:
            return
        order_ref = (
            picking.sale_id.shopify_order_name or picking.sale_id.name
            if picking.sale_id
            else picking.name
        )
        client.post(
            f"fulfillments/{picking.shopify_fulfillment_id}/update_tracking.json",
            {
                "fulfillment": {
                    "tracking_info": {
                        "number": tracking_number,
                        "company": (
                            picking.carrier_id.name if picking.carrier_id else None
                        ),
                    },
                    "notify_customer": self._notify_customer(),
                }
            },
        )
        log.log_event(
            "info",
            f"Updated tracking on Shopify fulfillment "
            f"{picking.shopify_fulfillment_id} (picking {picking.name}, order "
            f"{order_ref}): {tracking_number}.",
            source="fulfillment",
            shopify_order_ref=order_ref,
        )
