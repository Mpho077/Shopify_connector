from datetime import date, datetime, timezone
import logging

from odoo import Command, fields, models

from .shopify_discount import (
    PARAM_KEY as DISCOUNT_PARAM_KEY,
    DISCOUNT_PRODUCT_SKU,
    DISCOUNT_PRODUCT_XMLID,
    SHIPPING_PRODUCT_SKU,
    SHIPPING_PRODUCT_XMLID,
    aggregate_by_sku,
    amounts_close,
    applied_discount_codes_csv,
    as_float,
    discount_line_name,
    discount_sync_allows_shopify_to_odoo,
    discounts_close,
    line_discount_percent,
    line_item_quantity,
    merchandise_discount_amount,
    order_shipping_amount,
    shipping_line_name,
    uses_line_percent_discount,
)
from .shopify_order_tags import (
    shopify_tag_names,
    tags_csv,
)
from .shopify_api import normalize_shopify_variant_id

# Legacy connector / Studio fields that store shop.myshopify.com:<REST id> instead of
# shopify_order_ops.shopify_order_id.
CONNECTOR_ORDER_ID_FIELDS = (
    "shopify_id",
    "x_studio_shopify_id",
    "x_shopify_id",
)


def parse_shopify_rest_order_id(value):
    """Numeric Admin REST order id from our field or a legacy binding key.

    Accepts ``7234616819861``, ``shop.myshopify.com:7234616819861``, or
    ``gid://shopify/Order/7234616819861``.
    """
    if value in (None, False, ""):
        return False
    text = str(value).strip()
    if not text:
        return False
    if text.isdigit():
        return text
    if "/" in text:
        tail = text.rsplit("/", 1)[-1].strip()
        if tail.isdigit():
            return tail
    if ":" in text:
        tail = text.rsplit(":", 1)[-1].strip()
        if tail.isdigit():
            return tail
    return False


PARAM_PREFIX = "shopify_order_ops."
# Shopify Admin REST page size for orders.json.
PAGE_SIZE = 250
# shopify.sync.log source tag for this subsystem.
LOG_SOURCE = "order_pull"
_logger = logging.getLogger(__name__)
ADDRESS_SYNC_SOURCE = "order_address_sync"
TAG_SYNC_SOURCE = "order_tag_sync"
CHARGE_SYNC_SOURCE = "order_charge_sync"
QTY_SYNC_SOURCE = "order_qty_sync"

SPLIT_UPDATE_JOBS = (
    "order_tag_sync",
    "order_address_sync",
    "order_charge_sync",
    "order_qty_sync",
)


class SaleOrder(models.Model):
    """Shopify-specific extra fields on sale orders (kept next to the pull
    engine that fills them)."""

    _inherit = "sale.order"

    shopify_tags = fields.Char(string="Shopify Tags", copy=False)


class ShopifyOrderPullEngine(models.AbstractModel):
    """Pulls Shopify orders into Odoo as confirmed sale orders.

    Contract (implemented by agent task H) — see method docstrings.

    Settings pack (task L): Shopify order numbering, payment/fulfillment/
    source/date-range filters, order note/tags fields, single-customer
    mode, skip-product-create lines and per-gateway payment journals —
    all driven by `shopify_order_ops.*` ir.config_parameter keys.
    """

    _name = "shopify.order.pull.engine"
    _description = "Shopify Order Pull Engine"

    # ------------------------------------------------------------------
    # public entry points
    # ------------------------------------------------------------------
    def process_order_pull(self, job):
        """Create SO -> confirm -> invoice -> register payment from a job.

        Job payload: {"order_id": <shopify id>, "topic": ..., "raw": {...}}.

        Required behaviour:
        - Respect config flag `order_pull_enabled`; when off, log info and
          return WITHOUT raising (job marks done, no retry storm).
        - Fetch the current order via api.get_order(payload['order_id']).
        - Idempotent: if a sale.order already exists with
          shopify_order_id == str(order id) OR client_order_ref == order name
          (with/without '#'), backfill refs, log 'already pulled', return.
        - Customer: res.partner matched by shopify_customer_id (order
          .customer.id), then normalized email; create a minimal partner
          (name, email, phone, default_address fields) when missing.
        - Lines: every line_item resolved by SKU via default_code then
          barcode; missing product -> raise RuntimeError (same rule as the
          edit engine: never auto-create products).
        - Create SO: client_order_ref=order name, shopify_order_id,
          shopify_order_name, order_date from created_at, lines with Shopify
          unit prices and quantities, then one negative discount line for
          the order's merchandise discount; let Odoo compute taxes from the
          product.
        - Confirm the SO (action_confirm).
        - If config `order_pull_invoice` is truthy (default on): create the
          invoice via order._create_invoices(), post it, backfill
          move.shopify_order_id. If order financial_status == 'paid' and
          config `order_pull_auto_paid` truthy: register full payment via the
          account.payment.register wizard with the configured payment journal
          (log warning + leave open when no journal configured).
        - Log each meaningful step via env['shopify.sync.log'].log_event
          (source='order_pull', job=job, shopify_order_ref=order name).
        - Raise on failure; the queue retries.
        """
        log = self.env["shopify.sync.log"]
        api = self.env["shopify.api.client"]

        payload = job.payload_dict()
        shopify_order_id = payload.get("order_id")

        if not shopify_order_id:
            raise RuntimeError(
                "Job %s: payload has no 'order_id' — cannot pull order." % job.name
            )

        pull_enabled = self._truthy(self._param("order_pull_enabled"))

        # When order pull is OFF (e.g. another connector still imports orders), still
        # fetch the current Shopify order and sync shipping/billing addresses
        # onto any already-linked Odoo SO. Do not create new sale orders.
        if not pull_enabled:
            order = api.get_order(shopify_order_id)
            if not order:
                log.log_event(
                    "info",
                    "Order pull job %s skipped: order_pull_enabled is off and "
                    "Shopify order %s was not found."
                    % (job.name, shopify_order_id),
                    source=LOG_SOURCE,
                    job=job,
                    shopify_order_ref=str(shopify_order_id),
                )
                return
            order_name = order.get("name") or str(shopify_order_id)

            def _log_off(level, message):
                log.log_event(
                    level, message, source=LOG_SOURCE, job=job,
                    shopify_order_ref=order_name,
                )

            if self.skip_order_before_cutoff(order, _log_off):
                return

            existing = self._find_existing_order(shopify_order_id, order.get("name"))
            if existing:
                vals = {}
                if not existing.shopify_order_id:
                    vals["shopify_order_id"] = str(shopify_order_id)
                if not existing.shopify_order_name and order.get("name"):
                    vals["shopify_order_name"] = order["name"]
                if vals:
                    existing.write(vals)
                _log_off(
                    "info",
                    "Order pull is off; queueing split Shopify updates for "
                    "existing SO %s (Shopify order %s)."
                    % (existing.name, order_name),
                )
                self.enqueue_split_order_updates(
                    shopify_order_id,
                    payload.get("topic") or "orders/updated",
                    payload=payload,
                    order=order,
                    process_now=True,
                )
            else:
                _log_off(
                    "info",
                    "Order pull job %s skipped: order_pull_enabled is off and "
                    "no linked Odoo sale order exists for Shopify order %s."
                    % (job.name, order_name),
                )
            return

        # 1. Always fetch the CURRENT order; never trust the webhook payload.
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

        _log(
            "info",
            "Processing order pull for Shopify order %s (id %s, "
            "financial_status=%s)."
            % (order_name, shopify_order_id, order.get("financial_status") or "unknown"),
        )

        # 1b. Settings filters (payment/fulfillment/source/date range) run
        # EARLY — after fetching the order, before anything is created. A
        # filtered-out order logs 'filtered out' and returns WITHOUT raising:
        # the job completes and nothing is created.
        if not self._passes_filters(order, _log):
            return

        # 2. Idempotency: an existing SO (Shopify id or reference) wins.
        existing = self._find_existing_order(shopify_order_id, order.get("name"))
        if existing:
            vals = {}
            if not existing.shopify_order_id:
                vals["shopify_order_id"] = str(shopify_order_id)
            if not existing.shopify_order_name and order.get("name"):
                vals["shopify_order_name"] = order["name"]
            if vals:
                existing.write(vals)
            _log(
                "info",
                "Order %s already pulled as %s — queueing split updates "
                "(tags, address, charges, quantities) instead of mixing "
                "them into this pull job." % (order_name, existing.name),
            )
            self.enqueue_split_order_updates(
                shopify_order_id,
                payload.get("topic") or "orders/updated",
                payload=payload,
                order=order,
                process_now=True,
            )
            return

        # 3-5. Customer -> lines -> sale order -> confirm.
        partner = self._resolve_partner(order, _log)
        sale_order = self._create_sale_order(order, shopify_order_id, partner, _log)
        # Shopify numbering (default-on): rename the SO while still draft.
        self._apply_shopify_order_name(sale_order, order, _log)
        self._sync_line_discounts(sale_order, order, _log, on_create=True)
        _log(
            "info",
            "Order %s: created sale order %s with %d line(s)."
            % (order_name, sale_order.name, len(sale_order.order_line)),
        )
        sale_order.action_confirm()
        _log("info", "Order %s: sale order %s confirmed." % (order_name, sale_order.name))

        # 6. Invoice + optional payment (both default-on unless disabled).
        if self._truthy_default_true(self._param("order_pull_invoice")):
            self._invoice_and_pay(sale_order, order, shopify_order_id, _log)
        else:
            _log(
                "info",
                "Order %s: order_pull_invoice is off — sale order %s left "
                "un-invoiced." % (order_name, sale_order.name),
            )

        _log("info", "Order %s: pull processed successfully." % order_name)

    def process_order_address_sync(self, job):
        """Shopify -> Odoo shipping/billing address only."""
        loaded = self._existing_order_from_job(job, ADDRESS_SYNC_SOURCE)
        if loaded is False:
            return
        if loaded is None:
            payload = job.payload_dict()
            if self._truthy(self._param("order_pull_enabled")):
                return self.process_order_pull(job)
            self.env["shopify.sync.log"].log_event(
                "info",
                "Address sync for Shopify order %s skipped: no linked Odoo "
                "sale order."
                % (payload.get("order_id") or job.name),
                source=ADDRESS_SYNC_SOURCE,
                job=job,
                shopify_order_ref=str(payload.get("order_id") or ""),
            )
            return
        order, sale_order, _log = loaded
        before_ship = sale_order.partner_shipping_id.id
        before_bill = sale_order.partner_invoice_id.id
        _log(
            "info",
            "Processing address sync for Shopify order %s -> SO %s."
            % (order.get("name") or sale_order.name, sale_order.name),
        )
        self._sync_shipping_address(sale_order, order, _log)
        self._sync_billing_address(sale_order, order, _log)
        changes = []
        if sale_order.partner_shipping_id.id != before_ship:
            changes.append("shipping address")
        if sale_order.partner_invoice_id.id != before_bill:
            changes.append("billing address")
        if changes:
            _log(
                "info",
                "Order %s: address sync applied %s."
                % (order.get("name") or sale_order.name, ", ".join(changes)),
            )
        else:
            _log(
                "info",
                "Order %s: address sync — no address change on SO %s."
                % (order.get("name") or sale_order.name, sale_order.name),
            )

    def process_order_tag_sync(self, job):
        """Shopify -> Odoo tags on the sale order."""
        loaded = self._existing_order_from_job(job, TAG_SYNC_SOURCE)
        if not loaded:
            return
        order, sale_order, _log = loaded
        before_tags = (sale_order.shopify_tags or "").strip()
        _log(
            "info",
            "Processing tag sync for Shopify order %s -> SO %s."
            % (order.get("name") or sale_order.name, sale_order.name),
        )
        self._apply_shopify_order_tags(sale_order, order, _log)
        names = tags_csv(shopify_tag_names(order)) or "cleared"
        after_tags = (sale_order.shopify_tags or "").strip()
        if before_tags == after_tags:
            _log(
                "info",
                "Order %s: tag sync — tags already match."
                % (order.get("name") or sale_order.name),
            )
        elif before_tags != after_tags:
            _log(
                "info",
                "Order %s: tag sync finished (%s)."
                % (order.get("name") or sale_order.name, names),
            )

    def process_order_charge_sync(self, job):
        """Shopify -> Odoo shipping/discount sale lines only."""
        loaded = self._existing_order_from_job(job, CHARGE_SYNC_SOURCE)
        if not loaded:
            return
        order, sale_order, _log = loaded
        _log(
            "info",
            "Processing charge sync for Shopify order %s -> SO %s."
            % (order.get("name") or sale_order.name, sale_order.name),
        )
        self._sync_line_discounts(sale_order, order, _log)

    def process_order_qty_sync(self, job):
        """Shopify -> Odoo remaining line quantities only."""
        loaded = self._existing_order_from_job(job, QTY_SYNC_SOURCE)
        if not loaded:
            return
        order, sale_order, _log = loaded
        _log(
            "info",
            "Processing quantity sync for Shopify order %s -> SO %s."
            % (order.get("name") or sale_order.name, sale_order.name),
        )
        changed = self.env["shopify.order.edit.engine"]._sync_quantities_from_shopify(
            sale_order, order, _log
        )
        if not changed:
            _log(
                "info",
                "Order %s: quantity sync — sale lines already match Shopify."
                % (order.get("name") or sale_order.name),
            )

    def _existing_order_from_job(self, job, source):
        """Fetch Shopify order + linked SO, or None/False.

        None = no linked SO. False = job should return (not found / cutoff).
        """
        log = self.env["shopify.sync.log"]
        api = self.env["shopify.api.client"]
        payload = job.payload_dict()
        shopify_order_id = payload.get("order_id")
        if not shopify_order_id:
            raise RuntimeError(
                "Job %s: payload has no 'order_id'." % job.name
            )
        order = api.get_order(shopify_order_id)
        if not order:
            log.log_event(
                "info",
                "%s job %s: Shopify order %s not found."
                % (source, job.name, shopify_order_id),
                source=source,
                job=job,
                shopify_order_ref=str(shopify_order_id),
            )
            return False
        order_name = order.get("name") or str(shopify_order_id)

        def _log(level, message):
            log.log_event(
                level, message, source=source, job=job,
                shopify_order_ref=order_name,
            )

        if self.skip_order_before_cutoff(order, _log):
            return False
        existing = self._find_existing_order(shopify_order_id, order.get("name"))
        if not existing:
            return None
        vals = {}
        if not existing.shopify_order_id:
            vals["shopify_order_id"] = str(shopify_order_id)
        if not existing.shopify_order_name and order.get("name"):
            vals["shopify_order_name"] = order["name"]
        if vals:
            existing.write(vals)
        return order, existing, _log

    def updated_order_job_types(self, sale_order, order):
        """Which split jobs this Shopify payload needs on an existing SO."""
        types = []
        if self._tags_need_sync(sale_order, order):
            types.append("order_tag_sync")
        if self._addresses_need_sync(sale_order, order):
            types.append("order_address_sync")
        if self._charges_need_sync(sale_order, order):
            types.append("order_charge_sync")
        if self._additions_need_sync(sale_order, order) or self._quantities_need_sync(
            sale_order, order
        ):
            # Always order_edit for line changes (not qty_sync alone). order_edit
            # fetches the live Shopify order, applies removals AND additions, and
            # only touches the invoice when there are new lines. A swap (remove
            # one SKU, add another) used to enqueue qty_sync from a thin
            # orders/updated payload and never create the new line.
            types.append("order_edit")
        if self.env["shopify.fulfillment.sync"].order_needs_fulfillment_pull(
            sale_order, order
        ):
            types.append("fulfillment_pull")
        return types

    def enqueue_split_order_updates(
        self, shopify_order_id, topic, payload=None, order=None, process_now=False
    ):
        """Enqueue separate tag/address/charge/edit jobs (never mixed)."""
        Job = self.env["shopify.sync.job"].sudo()
        payload = payload or {}
        order = order if order is not None else payload
        existing = self._find_existing_order(
            shopify_order_id, order.get("name") if order else None
        )
        created = Job.browse()
        if self.skip_order_before_cutoff(order):
            return created
        if not existing:
            if self._truthy(self._param("order_pull_enabled")):
                if not self._find_queued_job(
                    Job, "order_pull", shopify_order_id,
                    ["pending", "processing", "done"],
                ):
                    created |= self._enqueue_split_job(
                        Job,
                        "order_pull",
                        shopify_order_id,
                        topic,
                        payload,
                        process_now,
                    )
            return created
        # Webhook payloads (especially orders/updated) can omit or lag
        # line_items. Decide line jobs from the live Shopify order.
        order = self._order_for_split_decision(shopify_order_id, order)
        for job_type in self.updated_order_job_types(existing, order):
            if self._find_queued_job(
                Job, job_type, shopify_order_id, ["pending", "processing"]
            ):
                continue
            created |= self._enqueue_split_job(
                Job, job_type, shopify_order_id, topic, payload, process_now
            )
        return created

    def _order_for_split_decision(self, shopify_order_id, order):
        """Prefer a live Shopify GET so line add/remove detection is complete."""
        order = order if isinstance(order, dict) else {}
        try:
            live = self.env["shopify.api.client"].get_order(shopify_order_id)
        except Exception:  # noqa: BLE001 - fall back to webhook body
            _logger.exception(
                "Shopify Ops: could not refresh order %s for split decision; "
                "using webhook payload.",
                shopify_order_id,
            )
            return order
        if not live:
            return order
        # Keep webhook-only fields if the GET is sparse (should not happen).
        if "line_items" not in live and order.get("line_items") is not None:
            live = dict(live)
            live["line_items"] = order.get("line_items")
        return live

    def _enqueue_split_job(
        self, Job, job_type, shopify_order_id, topic, payload, process_now
    ):
        name = "%s %s" % (job_type.replace("_", " "), shopify_order_id)
        payload_dict = {
            "order_id": shopify_order_id,
            "topic": topic,
            "raw": payload,
        }
        if process_now:
            job = Job.enqueue(name, job_type, payload_dict)
            job._process_one()
            return job
        return Job.enqueue_and_process(name, job_type, payload_dict)

    def _tags_need_sync(self, sale_order, order):
        names = shopify_tag_names(order)
        if self._truthy_default_true(self._param("include_order_tags")):
            csv = tags_csv(names)
            previous = (sale_order.shopify_tags or "").strip()
            if previous != csv:
                return True
            if sale_order._fields.get("tag_ids"):
                have = {
                    (tag.name or "").casefold()
                    for tag in sale_order.tag_ids
                    if tag.name
                }
                want = {name.casefold() for name in names}
                if have != want:
                    return True
        return False

    def _addresses_need_sync(self, sale_order, order):
        if not self._address_sync_from_shopify():
            return False
        shipping = order.get("shipping_address") or {}
        billing = order.get("billing_address") or {}
        if self._usable_shopify_address(shipping) and not self._address_dict_matches(
            sale_order.partner_shipping_id, shipping
        ):
            return True
        if self._usable_shopify_address(billing) and not self._address_dict_matches(
            sale_order.partner_invoice_id, billing
        ):
            return True
        return False

    def _charges_need_sync(self, sale_order, order):
        allow_add = self._allow_add_charge_lines(order, False)
        if (sale_order.shopify_discount_codes or "").strip() != (
            applied_discount_codes_csv(order) or ""
        ):
            return True
        if self._discount_sync_from_shopify():
            if uses_line_percent_discount(order):
                by_sku = aggregate_by_sku(order.get("line_items"))
                for line in self._shopify_product_lines(sale_order):
                    want = self._line_percent_target(line, by_sku)
                    if not discounts_close(line.discount, want):
                        return True
                if sale_order.order_line.filtered("shopify_discount_line"):
                    return True
            else:
                amount = merchandise_discount_amount(order)
                want = -amount if amount > 0 else 0.0
                lines = sale_order.order_line.filtered("shopify_discount_line")
                if lines:
                    keeper = lines.sorted("id")[-1]
                    if not amounts_close(keeper.price_unit, want):
                        return True
                elif want and allow_add:
                    return True
        if self._shipping_charge_sync_enabled():
            want = order_shipping_amount(order)
            lines = sale_order.order_line.filtered("shopify_shipping_line")
            if lines:
                keeper = lines.sorted("id")[-1]
                if not amounts_close(keeper.price_unit, want):
                    return True
            elif want and allow_add:
                return True
        return False

    def _quantities_need_sync(self, sale_order, order):
        if not sale_order or sale_order.state == "cancel":
            return False
        if "line_items" not in (order or {}):
            return False
        edit = self.env["shopify.order.edit.engine"]
        shopify_lines = edit._shopify_line_map(order, lambda *a: None)
        odoo_by_sku = edit._odoo_product_lines_by_sku(sale_order)
        for sku, lines in odoo_by_sku.items():
            target = (shopify_lines.get(sku) or {}).get("qty") or 0.0
            current = sum(lines.mapped("product_uom_qty"))
            if target + 0.0001 < current:
                return True
        return False

    def _additions_need_sync(self, sale_order, order):
        """True when Shopify has a product line (or extra qty) Odoo does not.

        ``orders/updated`` used to only enqueue quantity *decreases*, so a
        swap (remove 1700 bath, add 1500 bath) reduced the old line and
        never created the new one unless ``orders/edited`` also arrived.
        """
        if not sale_order or sale_order.state == "cancel":
            return False
        if "line_items" not in (order or {}):
            return False
        if not self._truthy_default_true(self._param("order_edit_enabled")):
            return False
        edit = self.env["shopify.order.edit.engine"]
        shopify_lines = edit._shopify_line_map(order, lambda *a: None)
        odoo_by_sku = edit._odoo_product_lines_by_sku(sale_order)
        empty = self.env["sale.order.line"]
        for sku, sline in shopify_lines.items():
            shopify_qty = sline.get("qty") or 0.0
            odoo_qty = sum(
                odoo_by_sku.get(sku, empty).mapped("product_uom_qty")
            )
            if shopify_qty > odoo_qty + 0.0001:
                return True
        return False

    def enqueue_historical(self, date_from, limit=250):
        """Enqueue order_pull jobs for Shopify orders created since date_from.

        Called from the Settings button. Fetch orders.json with
        created_at_min=date_from (date or datetime), status='any', paginate
        by since_id; enqueue one order_pull job per order, skipping orders
        that already have a pending/processing/done pull job or an existing
        SO (client_order_ref match). Cap at `limit`. Return a summary string
        for the UI notification.
        """
        api = self.env["shopify.api.client"]
        log = self.env["shopify.sync.log"]
        Job = self.env["shopify.sync.job"]

        try:
            limit = max(int(limit), 1)
        except (TypeError, ValueError):
            limit = 250
        stamp = date_from
        if isinstance(stamp, datetime) and stamp.tzinfo is not None:
            stamp = stamp.astimezone(timezone.utc).replace(tzinfo=None)
        elif isinstance(stamp, date) and not isinstance(stamp, datetime):
            stamp = datetime.combine(stamp, datetime.min.time())
        cutoff = self._order_sync_after_cutoff()
        if cutoff and (not isinstance(stamp, datetime) or stamp < cutoff):
            stamp = cutoff
        created_at_min = self._shopify_created_at_min(stamp)

        enqueued = already_odoo = already_queued = 0
        since_id = 0
        while enqueued < limit:
            data = api.get(
                "orders.json",
                params={
                    "created_at_min": created_at_min,
                    "status": "any",
                    "limit": PAGE_SIZE,
                    "since_id": since_id,
                },
            )
            page = data.get("orders") or []
            if not page:
                break
            since_id = max(o.get("id") or 0 for o in page)
            for order in page:
                if enqueued >= limit:
                    break
                order_id = order.get("id")
                if not order_id:
                    continue
                if self.skip_order_before_cutoff(order):
                    continue
                if self._find_existing_order(order_id, order.get("name")):
                    already_odoo += 1
                    continue
                if self._find_queued_pull_job(Job, order_id):
                    already_queued += 1
                    continue
                Job.enqueue(
                    name="order_pull %s" % order_id,
                    job_type="order_pull",
                    payload_dict={
                        "order_id": order_id,
                        "topic": "historical",
                        "raw": {},
                    },
                )
                enqueued += 1
            if len(page) < PAGE_SIZE:
                break

        summary = "Enqueued %d orders (%d already in Odoo, %d already queued)" % (
            enqueued,
            already_odoo,
            already_queued,
        )
        log.log_event(
            "info",
            "Historical order import since %s: %s." % (created_at_min, summary),
            source=LOG_SOURCE,
        )
        return summary

    # ------------------------------------------------------------------
    # config helpers
    # ------------------------------------------------------------------
    def _param(self, key, default=None):
        return (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param(PARAM_PREFIX + key, default)
        )

    @staticmethod
    def _truthy(value):
        """Explicit-on semantics: only 'True'/'true'/'1' count as enabled."""
        return str(value).strip().lower() in ("true", "1")

    @staticmethod
    def _truthy_default_true(value):
        """Default-on semantics: unset (None) is True; only an explicit
        falsy string ('False'/'false'/'0') disables."""
        if value is None:
            return True
        return str(value).strip().lower() not in ("false", "0")

    # ------------------------------------------------------------------
    # order filters (settings pack)
    # ------------------------------------------------------------------
    @staticmethod
    def _shopify_created_at_min(date_from):
        """Odoo date/datetime (naive UTC) -> Shopify created_at_min ISO string."""
        if date_from is None or date_from is False:
            return ""
        if isinstance(date_from, datetime):
            if date_from.tzinfo is not None:
                date_from = date_from.astimezone(timezone.utc).replace(tzinfo=None)
            return date_from.strftime("%Y-%m-%dT%H:%M:%SZ")
        if isinstance(date_from, date):
            return date_from.isoformat()
        return str(date_from)

    @staticmethod
    def _csv_set(raw):
        """Comma-separated config string -> set of lowercase tokens.
        Empty/unset -> empty set (= 'no restriction' for optional filters)."""
        if not raw:
            return set()
        return {part.strip().lower() for part in str(raw).split(",") if part.strip()}

    @staticmethod
    def _order_created_datetime(created_at):
        """Shopify created_at -> naive UTC datetime, or None."""
        text = (created_at or "").strip() if isinstance(created_at, str) else ""
        if not text:
            return None
        try:
            stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone(timezone.utc).replace(tzinfo=None)
        return stamp

    @staticmethod
    def _order_created_date(created_at):
        """Date part of the Shopify created_at timestamp, taken as reported
        (shop-local offset, no UTC conversion) -> datetime.date or None."""
        text = (created_at or "").strip() if isinstance(created_at, str) else ""
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            return None

    def _order_sync_after_cutoff(self):
        """Naive UTC datetime after which orders may be pulled/updated, or None."""
        raw = (
            self._param("order_sync_after")
            or self._param("order_charge_sync_from")
            or ""
        ).strip()
        if not raw:
            return None
        text = raw.replace("T", " ").replace("Z", "")
        try:
            return fields.Datetime.to_datetime(text)
        except (TypeError, ValueError):
            return None

    def skip_order_before_cutoff(self, order, _log=None):
        """True when this Shopify order is older than Only pull / update after.

        Jobs should return without mutating Odoo. Unparsable created_at with
        a cutoff set is skipped (fail closed).
        """
        cutoff = self._order_sync_after_cutoff()
        if not cutoff:
            return False
        created = self._order_created_datetime(
            order.get("created_at") if order else None
        )
        order_name = (order or {}).get("name") or str(
            (order or {}).get("id") or ""
        )
        if created is None:
            if _log:
                _log(
                    "warning",
                    "Order %s skipped: only-sync-after is %s but created_at "
                    "'%s' is unparsable."
                    % (
                        order_name,
                        cutoff.strftime("%Y-%m-%d %H:%M:%S"),
                        (order or {}).get("created_at"),
                    ),
                )
            return True
        if created < cutoff:
            if _log:
                _log(
                    "info",
                    "Order %s skipped: created %s is before only-sync-after %s."
                    % (
                        order_name,
                        created.strftime("%Y-%m-%d %H:%M:%S"),
                        cutoff.strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )
            return True
        return False

    def _charge_sync_cutoff(self):
        """Same window as only-sync-after (legacy name used by charge lines)."""
        return self._order_sync_after_cutoff()

    def _allow_add_charge_lines(self, order, on_create):
        """Whether shipping/discount lines may be created (not merely updated).

        New pulls (on_create) always add unless a cutoff is set and this
        Shopify order is older. Existing sale orders are never backfilled
        unless they were created at or after only-sync-after.
        """
        cutoff = self._order_sync_after_cutoff()
        created = self._order_created_datetime(
            order.get("created_at") if order else None
        )
        if cutoff:
            if created is None:
                return bool(on_create)
            return created >= cutoff
        return bool(on_create)

    @staticmethod
    def _parse_date_param(raw):
        """ISO date string (or date) from ir.config_parameter -> date, None
        when unset or unparsable."""
        if not raw:
            return None
        try:
            return fields.Date.to_date(raw)
        except (TypeError, ValueError):
            return None

    def _passes_filters(self, order, _log):
        """Settings-pack filters. Returns True when the order should be
        pulled; otherwise logs an info 'filtered out' line and returns
        False — the caller returns WITHOUT raising, so the job completes
        and nothing is created."""
        order_name = order.get("name") or str(order.get("id") or "")

        if self.skip_order_before_cutoff(order, _log):
            return False

        # Payment status: default 'paid,partially_paid' when unset; an
        # explicitly blank value means 'all'.
        raw = self._param("order_payment_status_filter")
        allowed = self._csv_set(raw if raw is not None else "paid,partially_paid")
        if allowed:
            status = (order.get("financial_status") or "").strip().lower()
            if status not in allowed:
                _log(
                    "info",
                    "Order %s filtered out: financial_status '%s' is not in "
                    "order_payment_status_filter (%s)."
                    % (order_name, status or "unknown", raw),
                )
                return False

        # Fulfillment status: empty/unset filter = all. Shopify null maps to
        # 'unfulfilled'.
        allowed = self._csv_set(self._param("order_fulfillment_status_filter"))
        if allowed:
            status = (order.get("fulfillment_status") or "unfulfilled").strip().lower()
            if status not in allowed:
                _log(
                    "info",
                    "Order %s filtered out: fulfillment_status '%s' is not in "
                    "order_fulfillment_status_filter." % (order_name, status),
                )
                return False

        # Order source: empty/unset filter = all.
        allowed = self._csv_set(self._param("order_source_filter"))
        if allowed:
            source = (order.get("source_name") or "").strip().lower()
            if source not in allowed:
                _log(
                    "info",
                    "Order %s filtered out: source_name '%s' is not in "
                    "order_source_filter." % (order_name, source or "unknown"),
                )
                return False

        # Created-at date range: only when explicitly enabled; either bound
        # may be open.
        if self._truthy(self._param("order_date_range_enabled")):
            date_from = self._parse_date_param(self._param("order_sync_date_from"))
            date_to = self._parse_date_param(self._param("order_sync_date_to"))
            created_date = self._order_created_date(order.get("created_at"))
            if created_date is None:
                _log(
                    "warning",
                    "Order %s: order_date_range_enabled is on but created_at "
                    "'%s' is unparsable — letting the order through."
                    % (order_name, order.get("created_at")),
                )
            elif (date_from and created_date < date_from) or (
                date_to and created_date > date_to
            ):
                _log(
                    "info",
                    "Order %s filtered out: created %s is outside the "
                    "configured sync range [%s, %s]."
                    % (
                        order_name,
                        created_date.isoformat(),
                        date_from.isoformat() if date_from else "...",
                        date_to.isoformat() if date_to else "...",
                    ),
                )
                return False

        return True

    # ------------------------------------------------------------------
    # matching helpers
    # ------------------------------------------------------------------
    def rest_order_id_from_sale_order(self, sale_order):
        """Numeric REST id from our field, else a legacy/Studio Shopify Id."""
        parsed = parse_shopify_rest_order_id(sale_order.shopify_order_id)
        if parsed:
            return parsed
        for fname in CONNECTOR_ORDER_ID_FIELDS:
            if fname in sale_order._fields:
                parsed = parse_shopify_rest_order_id(sale_order[fname])
                if parsed:
                    return parsed
        return False

    def _find_by_connector_order_id(self, shopify_order_id):
        """Match legacy ``shop.myshopify.com:<id>`` when our id field is empty."""
        SaleOrder = self.env["sale.order"]
        target = str(shopify_order_id)
        for fname in CONNECTOR_ORDER_ID_FIELDS:
            if fname not in SaleOrder._fields:
                continue
            rec = SaleOrder.search([(fname, "=", target)], limit=1)
            if rec:
                return rec
            rec = SaleOrder.search([(fname, "=like", "%:" + target)], limit=1)
            if rec:
                return rec
        return SaleOrder.browse()

    def _find_existing_order(self, shopify_order_id, order_name):
        """Existing sale.order by shopify_order_id, client_order_ref, or SO
        name (each with/without '#'). Empty recordset when none.

        Matching on the SO name too matters for legacy-imported orders that
        carry the Shopify number as the order name but never got
        client_order_ref/shopify_order_id filled in."""
        SaleOrder = self.env["sale.order"]
        domain = [("shopify_order_id", "=", str(shopify_order_id))]
        name = (order_name or "").strip()
        if name:
            clean = name.lstrip("#")
            candidates = [name, clean, "#" + clean]
            domain = [
                "|",
                "|",
                ("shopify_order_id", "=", str(shopify_order_id)),
                ("client_order_ref", "in", candidates),
                ("name", "in", candidates),
            ]
        rec = SaleOrder.search(domain, limit=1)
        if rec:
            return rec
        return self._find_by_connector_order_id(shopify_order_id)

    def _find_queued_job(self, Job, job_type, order_id, states=None):
        """A job of this type already covers the Shopify order id."""
        states = list(states or ["pending", "processing"])
        candidates = Job.search(
            [
                ("job_type", "=", job_type),
                ("state", "in", states),
                ("payload", "ilike", '"order_id": %s,' % order_id),
            ],
        )
        target = str(order_id)
        for job in candidates:
            if str(job.payload_dict().get("order_id") or "") == target:
                return job
        return Job.browse()

    def _find_queued_pull_job(self, Job, order_id):
        """A non-failed order_pull job already exists for this Shopify order.

        Exact comparison of the parsed payload order id — a substring search
        for e.g. 1016 would also match a queued job for order 10160 and
        silently skip the order."""
        return self._find_queued_job(
            Job, "order_pull", order_id, ["pending", "processing", "done"]
        )

    # ------------------------------------------------------------------
    # customer
    # ------------------------------------------------------------------
    def _resolve_partner(self, order, _log):
        """Route to single-customer mode when configured, else the normal
        per-order customer matching/creation.

        single_customer_mode + scope 'all': EVERY order links to the single
        partner. Scope 'guest' (default): only orders without a Shopify
        customer account (order['customer'] empty) do. Raises RuntimeError
        when the configured partner does not exist — ops must create it."""
        if not self._truthy(self._param("single_customer_mode")):
            return self._get_or_create_customer(order, _log)
        order_name = order.get("name") or ""
        scope = (self._param("single_customer_scope") or "guest").strip().lower()
        is_guest = not (order.get("customer") or {})
        if scope != "all" and not is_guest:
            return self._get_or_create_customer(order, _log)
        email = (self._param("single_customer_email") or "").strip()
        if not email:
            raise RuntimeError(
                "Order %s: single_customer_mode is on but "
                "single_customer_email is empty — configure it in Settings "
                "-> Shopify Ops." % order_name
            )
        partner = self._find_partner_by_email(email)
        if not partner:
            raise RuntimeError(
                "Order %s: single_customer_mode is on but no res.partner "
                "has email '%s' — create that partner first, then retry the "
                "job." % (order_name, email.lower())
            )
        _log(
            "info",
            "Order %s: single-customer mode (scope=%s) — linked to partner "
            "%s (id %d)." % (order_name, scope, partner.display_name, partner.id),
        )
        return partner

    def _find_partner_by_email(self, email):
        """res.partner by normalized email (case-insensitive exact match);
        empty recordset when none."""
        Partner = self.env["res.partner"].sudo()
        target = (email or "").strip().lower()
        if not target:
            return Partner.browse()
        for candidate in Partner.search([("email", "ilike", target)], limit=20):
            if (candidate.email or "").strip().lower() == target:
                return candidate
        return Partner.browse()

    def _get_or_create_customer(self, order, _log):
        """Match the order's customer by shopify_customer_id, then normalized
        email; create a minimal partner when missing. Backfills
        shopify_customer_id on matched partners."""
        Partner = self.env["res.partner"].sudo()
        order_name = order.get("name") or ""
        customer = order.get("customer") or {}
        sid = str(customer.get("id") or "").strip()
        raw_email = (customer.get("email") or order.get("email") or "").strip()
        email = raw_email.lower()

        partner = Partner.browse()
        if sid:
            partner = Partner.search([("shopify_customer_id", "=", sid)], limit=1)
        if not partner and email:
            for candidate in Partner.search([("email", "ilike", email)], limit=10):
                if (candidate.email or "").strip().lower() == email:
                    partner = candidate
                    break

        if partner:
            if sid and not partner.shopify_customer_id:
                partner.write({"shopify_customer_id": sid})
            elif (
                sid
                and partner.shopify_customer_id
                and partner.shopify_customer_id != sid
            ):
                _log(
                    "warning",
                    "Order %s: matched partner %s is linked to a different "
                    "Shopify customer (%s, incoming %s); keeping the existing "
                    "link." % (order_name, partner.display_name,
                               partner.shopify_customer_id, sid),
                )
            _log(
                "info",
                "Order %s: matched existing customer %s (partner id %d)."
                % (order_name, partner.display_name, partner.id),
            )
            return partner

        # Create a minimal partner (name, email, phone, default address).
        default_address = (
            customer.get("default_address") or order.get("billing_address") or {}
        )
        first = (customer.get("first_name") or "").strip()
        last = (customer.get("last_name") or "").strip()
        name = (
            " ".join(p for p in (first, last) if p)
            or raw_email
            or (default_address.get("name") or "").strip()
            or "Shopify guest %s" % (order_name or sid or "order")
        )
        phone = (customer.get("phone") or "").strip() or (
            default_address.get("phone") or ""
        ).strip()
        vals = {
            "name": name,
            "email": raw_email or False,
            "phone": phone or False,
        }
        if sid:
            vals["shopify_customer_id"] = sid
        vals.update(self._address_vals(default_address))
        partner = Partner.create(vals)
        _log(
            "info",
            "Order %s: created customer %s (partner id %d, "
            "shopify_customer_id=%s)."
            % (order_name, partner.display_name, partner.id, sid or "none"),
        )
        return partner

    def _address_vals(self, address):
        """Map a Shopify address dict to res.partner address values
        (country/state resolved by code, defensively)."""
        if not address:
            return {}
        vals = {
            "street": (address.get("address1") or "").strip() or False,
            "street2": (address.get("address2") or "").strip() or False,
            "city": (address.get("city") or "").strip() or False,
            "zip": (address.get("zip") or "").strip() or False,
        }
        country_code = (address.get("country_code") or "").strip().upper()
        if country_code:
            country = (
                self.env["res.country"]
                .sudo()
                .search([("code", "=", country_code)], limit=1)
            )
            if country:
                vals["country_id"] = country.id
                province = (address.get("province_code") or "").strip().upper()
                if province:
                    state = (
                        self.env["res.country.state"]
                        .sudo()
                        .search(
                            [
                                ("country_id", "=", country.id),
                                ("code", "=", province),
                            ],
                            limit=1,
                        )
                    )
                    if state:
                        vals["state_id"] = state.id
        return vals

    # ------------------------------------------------------------------
    # shipping / billing address propagation
    # ------------------------------------------------------------------
    def _address_sync_from_shopify(self):
        raw = self._param("address_propagation_enabled")
        if raw is not None and not self._truthy_default_true(raw):
            return False
        direction = (self._param("address_sync_direction") or "two_way").strip()
        return direction in ("shopify_to_odoo", "two_way")

    @staticmethod
    def _usable_shopify_address(address):
        address = address or {}
        return bool(
            address.get("address1") or address.get("city") or address.get("zip")
        )

    def _sync_shipping_address(self, sale_order, order, _log):
        """Apply a changed Shopify shipping address to the Odoo SO, and to
        the customer's other unfulfilled Shopify-linked orders.

        Runs from the pull engine for existing SOs (including when order pull
        is OFF — external-import mode — so orders/updated webhooks still update
        delivery addresses). Only sibling orders whose delivery address still
        matches the OLD address are updated — an intentionally different
        address on a sibling order is never clobbered — and orders with a done
        delivery are never touched. Config: `address_propagation_enabled`
        (default ON) and `address_sync_direction` (shopify_to_odoo / two_way
        for this path). Odoo -> Shopify uses the same enable flag with
        odoo_to_shopify / two_way (see process_order_address_push).
        """
        if not self._address_sync_from_shopify():
            return
        address = order.get("shipping_address") or {}
        if not self._usable_shopify_address(address):
            return
        order_name = order.get("name") or ""
        old_shipping = sale_order.partner_shipping_id
        if self._address_dict_matches(old_shipping, address):
            return  # unchanged — nothing to do

        shipping = self._get_or_create_delivery_contact(
            sale_order.partner_id, address
        )

        targets = sale_order
        siblings = self.env["sale.order"].search(
            [
                ("id", "!=", sale_order.id),
                ("partner_id", "=", sale_order.partner_id.id),
                ("shopify_order_id", "!=", False),
                ("state", "=", "sale"),
            ]
        )
        skipped_fulfilled = skipped_different = 0
        for sibling in siblings:
            if sibling.picking_ids.filtered(
                lambda p: p.picking_type_code == "outgoing" and p.state == "done"
            ):
                skipped_fulfilled += 1
                continue
            if not self._same_partner_address(
                sibling.partner_shipping_id, old_shipping
            ):
                skipped_different += 1
                continue
            targets |= sibling

        for so in targets:
            so.with_context(shopify_sync_origin="shopify").write(
                {"partner_shipping_id": shipping.id}
            )
            _log(
                "info",
                "Order %s: delivery address on %s set to %s from the Shopify "
                "shipping address."
                % (order_name, so.name, shipping.display_name),
            )
        if skipped_fulfilled or skipped_different:
            _log(
                "info",
                "Order %s: address propagation skipped %d fulfilled and %d "
                "differently-addressed sibling order(s)."
                % (order_name, skipped_fulfilled, skipped_different),
            )

    def _sync_billing_address(self, sale_order, order, _log):
        """Apply a changed Shopify billing address to the Odoo SO invoice
        contact, and to sibling Shopify-linked orders that still share the
        old invoice address.

        Done deliveries are ignored here — billing is independent of
        fulfillment. Draft customer invoices follow via sale.order.write.
        Posted invoices are left alone.
        """
        if not self._address_sync_from_shopify():
            return
        address = order.get("billing_address") or {}
        if not self._usable_shopify_address(address):
            return
        order_name = order.get("name") or ""
        old_invoice = sale_order.partner_invoice_id
        if self._address_dict_matches(old_invoice, address):
            return

        invoice = self._get_or_create_address_contact(
            sale_order.partner_id, address, "invoice"
        )

        targets = sale_order
        siblings = self.env["sale.order"].search(
            [
                ("id", "!=", sale_order.id),
                ("partner_id", "=", sale_order.partner_id.id),
                ("shopify_order_id", "!=", False),
                ("state", "=", "sale"),
            ]
        )
        skipped_different = 0
        for sibling in siblings:
            if not self._same_partner_address(
                sibling.partner_invoice_id, old_invoice
            ):
                skipped_different += 1
                continue
            targets |= sibling

        for so in targets:
            so.with_context(shopify_sync_origin="shopify").write(
                {"partner_invoice_id": invoice.id}
            )
            _log(
                "info",
                "Order %s: invoice address on %s set to %s from the Shopify "
                "billing address."
                % (order_name, so.name, invoice.display_name),
            )
        if skipped_different:
            _log(
                "info",
                "Order %s: billing address propagation skipped %d "
                "differently-addressed sibling order(s)."
                % (order_name, skipped_different),
            )

    def _discount_sync_from_shopify(self):
        return discount_sync_allows_shopify_to_odoo(
            self._param(DISCOUNT_PARAM_KEY),
            self._param("discount_sync_direction"),
        )

    def _shipping_charge_sync_enabled(self):
        return self._truthy_default_true(self._param("shipping_charge_sync_enabled"))

    def _shopify_product_lines(self, sale_order):
        return sale_order.order_line.filtered(
            lambda l: not l.display_type
            and not l.shopify_discount_line
            and not l.shopify_shipping_line
        )

    def _sync_line_discounts(self, sale_order, order, _log, on_create=False):
        """Copy Shopify merchandise discounts and shipping onto Odoo sale lines.

        Percentage coupons land as Disc.% on the product
        line(s). Cart-wide fixed-amount codes become one negative
        SHOPIFY-DISCOUNT line. Shipping becomes one positive service line
        at the net amount the customer paid. Draft invoices follow; posted
        invoices are reset and re-posted like order-edit.

        Existing sale orders without a dedicated charge line are not
        backfilled with a new SHOPIFY-DISCOUNT / shipping line. Disc.% on
        existing product lines is still updated when Shopify sends a
        percentage coupon, and is never cleared unless a dedicated
        discount line is written in the same run.
        """
        if not sale_order or sale_order.state == "cancel":
            return
        self._apply_shopify_discount_codes(sale_order, order)
        do_discount = self._discount_sync_from_shopify()
        do_shipping = self._shipping_charge_sync_enabled()
        if not do_discount and not do_shipping:
            return

        percent_mode = do_discount and uses_line_percent_discount(order)
        allow_add = self._allow_add_charge_lines(order, on_create)
        has_discount = bool(sale_order.order_line.filtered("shopify_discount_line"))
        has_shipping = bool(sale_order.order_line.filtered("shopify_shipping_line"))
        skipped = []
        do_discount_line = do_discount and not percent_mode
        if do_discount_line and not has_discount and not allow_add:
            do_discount_line = False
            skipped.append("discount")
        if do_shipping and not has_shipping and not allow_add:
            do_shipping = False
            skipped.append("shipping")
        if skipped:
            order_name = order.get("name") or sale_order.name
            if _log:
                _log(
                    "info",
                    "Order %s: skipped %s backfill on existing sale order %s."
                    % (order_name, " and ".join(skipped), sale_order.name),
                )
        if not percent_mode and not do_discount_line and not do_shipping:
            return

        order_name = order.get("name") or sale_order.name
        product_lines = self._shopify_product_lines(sale_order)
        disc_amount = merchandise_discount_amount(order) if do_discount_line else 0.0
        ship_amount = order_shipping_amount(order) if do_shipping else 0.0
        disc_name = discount_line_name(order)
        ship_name = shipping_line_name(order)
        disc_product = (
            self._get_charge_product(
                DISCOUNT_PRODUCT_XMLID, DISCOUNT_PRODUCT_SKU, "Shopify Discount"
            )
            if do_discount_line
            else self.env["product.product"]
        )
        ship_product = (
            self._get_charge_product(
                SHIPPING_PRODUCT_XMLID, SHIPPING_PRODUCT_SKU, "Shopify Shipping"
            )
            if do_shipping
            else self.env["product.product"]
        )
        by_sku = (
            aggregate_by_sku(order.get("line_items")) if percent_mode else {}
        )

        need_unlock = False
        if percent_mode:
            need_unlock = any(
                not discounts_close(
                    line.discount, self._line_percent_target(line, by_sku)
                )
                for line in product_lines
            ) or bool(sale_order.order_line.filtered("shopify_discount_line"))
        if do_discount_line and any(line.discount for line in product_lines):
            need_unlock = True
        if do_discount_line and self._charge_line_needs_write(
            sale_order, "shopify_discount_line", -disc_amount if disc_amount > 0 else 0.0,
            disc_name, disc_product,
        ):
            need_unlock = True
        if do_shipping and self._charge_line_needs_write(
            sale_order, "shopify_shipping_line", ship_amount, ship_name, ship_product,
        ):
            need_unlock = True

        if sale_order.state == "done" and need_unlock:
            sale_order.action_unlock()
            _log(
                "info",
                "Order %s: unlocked locked sale order %s to apply order charges."
                % (order_name, sale_order.name),
            )

        if percent_mode:
            self._apply_percent_discounts(
                sale_order, order, product_lines, by_sku, _log
            )

        if do_discount_line:
            for line in product_lines:
                if line.discount:
                    old = line.discount
                    line.with_context(shopify_sync_origin="shopify").write(
                        {"discount": 0.0}
                    )
                    _log(
                        "info",
                        "Order %s: cleared Disc.%% on %s line %s (was %.4f%%); "
                        "Shopify discounts now use a dedicated line."
                        % (
                            order_name,
                            sale_order.name,
                            line.product_id.default_code or line.id,
                            old,
                        ),
                    )

        keep = self.env["sale.order.line"]
        to_remove = self.env["sale.order.line"]
        if percent_mode:
            to_remove |= sale_order.order_line.filtered("shopify_discount_line")
        elif do_discount_line:
            if disc_amount > 0:
                keep |= self._upsert_shopify_marked_line(
                    sale_order, order, _log,
                    "shopify_discount_line", -disc_amount, disc_name, disc_product,
                    "discount",
                )
            else:
                to_remove |= sale_order.order_line.filtered("shopify_discount_line")
        if do_shipping:
            if ship_amount > 0:
                keep |= self._upsert_shopify_marked_line(
                    sale_order, order, _log,
                    "shopify_shipping_line", ship_amount, ship_name, ship_product,
                    "shipping",
                )
            else:
                to_remove |= sale_order.order_line.filtered("shopify_shipping_line")

        self._apply_charge_lines_to_invoice(
            sale_order,
            order,
            keep,
            to_remove,
            _log,
            product_discount="copy" if percent_mode else "clear",
        )
        if to_remove:
            to_remove.unlink()
            _log(
                "info",
                "Order %s: removed Shopify charge line(s) from %s."
                % (order_name, sale_order.name),
            )

    def _apply_shopify_discount_codes(self, sale_order, order):
        csv = applied_discount_codes_csv(order)
        current = (sale_order.shopify_discount_codes or "").strip()
        if current == csv:
            return
        sale_order.with_context(shopify_sync_origin="shopify").write(
            {"shopify_discount_codes": csv or False}
        )

    def _line_percent_target(self, line, by_sku):
        sku = (
            (line.product_id.default_code or "").strip() if line.product_id else ""
        )
        if sku and sku in by_sku:
            return round(as_float((by_sku[sku] or {}).get("discount")), 2)
        name = (line.name or "").strip()
        for data in (by_sku or {}).values():
            if (data.get("name") or "").strip() == name:
                return round(as_float(data.get("discount")), 2)
        return 0.0

    def _apply_percent_discounts(self, sale_order, order, product_lines, by_sku, _log):
        """Set Disc.% on product lines from Shopify percentage allocations."""
        order_name = order.get("name") or sale_order.name
        for line in product_lines:
            want = self._line_percent_target(line, by_sku)
            if discounts_close(line.discount, want):
                continue
            line.with_context(shopify_sync_origin="shopify").write(
                {"discount": want}
            )
            _log(
                "info",
                "Order %s: Disc.%% on %s line %s set to %.2f (percentage coupon)."
                % (
                    order_name,
                    sale_order.name,
                    line.product_id.default_code or line.id,
                    want,
                ),
            )

    def _charge_line_needs_write(self, sale_order, flag, amount, name, product):
        lines = sale_order.order_line.filtered(flag)
        if abs(amount) <= 0.02:
            return bool(lines)
        if not lines:
            return True
        keeper = lines.sorted("id")[-1]
        if not amounts_close(keeper.price_unit, amount) or keeper.name != name:
            return True
        if product and keeper.product_id != product:
            return True
        return not keeper.product_id

    def _upsert_shopify_marked_line(
        self, sale_order, order, _log, flag, amount, name, product, kind
    ):
        """Create or update the marked charge line. ``amount`` is signed price_unit."""
        order_name = order.get("name") or sale_order.name
        vals = self._shopify_marked_line_vals(sale_order, name, amount, product, flag)
        lines = sale_order.order_line.filtered(flag)
        if lines:
            so_line = lines.sorted("id")[-1]
            extra = lines - so_line
            update = {}
            if so_line.name != vals["name"]:
                update["name"] = vals["name"]
            if not amounts_close(so_line.price_unit, vals["price_unit"]):
                update["price_unit"] = vals["price_unit"]
            if not amounts_close(so_line.product_uom_qty, 1.0):
                update["product_uom_qty"] = 1.0
            if so_line.discount:
                update["discount"] = 0.0
            if vals.get("product_id") and so_line.product_id.id != vals["product_id"]:
                update["product_id"] = vals["product_id"]
                update["name"] = vals["name"]
                update["price_unit"] = vals["price_unit"]
            if "tax_ids" in vals and set(so_line.tax_ids.ids) != set(
                vals["tax_ids"][0][2]
            ):
                update["tax_ids"] = vals["tax_ids"]
            if update:
                so_line.with_context(shopify_sync_origin="shopify").write(update)
                _log(
                    "info",
                    "Order %s: %s line on %s set to %.2f (%s)."
                    % (
                        order_name, kind, sale_order.name,
                        vals["price_unit"], vals["name"],
                    ),
                )
            if extra:
                extra.unlink()
            return so_line
        sale_order.with_context(shopify_sync_origin="shopify").write(
            {"order_line": [(0, 0, vals)]}
        )
        so_line = sale_order.order_line.filtered(flag).sorted("id")[-1]
        if so_line.name != vals["name"]:
            so_line.with_context(shopify_sync_origin="shopify").write(
                {"name": vals["name"]}
            )
        _log(
            "info",
            "Order %s: added %s line on %s for %.2f (%s)."
            % (order_name, kind, sale_order.name, vals["price_unit"], vals["name"]),
        )
        return so_line

    def _get_charge_product(self, xmlid, sku, name):
        """Service product for a dedicated Shopify charge sale line."""
        tmpl = self.env.ref(xmlid, raise_if_not_found=False)
        if tmpl and tmpl.product_variant_id:
            return tmpl.product_variant_id.sudo()
        Product = self.env["product.product"].sudo()
        existing = Product.search([("default_code", "=", sku)], limit=1)
        if existing:
            return existing
        return Product.with_context(shopify_sync_origin="shopify").create(
            {
                "name": name,
                "default_code": sku,
                "type": "service",
                "list_price": 0.0,
                "sale_ok": True,
                "purchase_ok": False,
                "invoice_policy": "order",
            }
        )

    def _shopify_marked_line_vals(self, sale_order, name, price_unit, product, flag):
        vals = {
            "product_id": product.id,
            "name": name,
            "product_uom_qty": 1.0,
            "price_unit": price_unit,
            "discount": 0.0,
            flag: True,
            "sequence": max(sale_order.order_line.mapped("sequence") or [10]) + 10,
        }
        if product.uom_id:
            vals["product_uom_id"] = product.uom_id.id
        tax_ids = self._charge_line_tax_ids(sale_order)
        if tax_ids:
            vals["tax_ids"] = [(6, 0, tax_ids)]
        return vals

    def _charge_line_tax_ids(self, sale_order):
        lines = self._shopify_product_lines(sale_order)
        tax_sets = {frozenset(line.tax_ids.ids) for line in lines}
        if len(tax_sets) == 1:
            return list(tax_sets.pop())
        return []

    def _apply_charge_lines_to_invoice(
        self, sale_order, order, keep_lines, to_remove, _log,
        product_discount="clear",
    ):
        """Mirror dedicated discount/shipping lines onto the target invoice.

        product_discount:
          - 'clear': zero Disc.% on product invoice lines (fixed-amount path)
          - 'copy': copy SO Disc.% onto product invoice lines (percentage coupons)
        """
        edit = self.env["shopify.order.edit.engine"]
        move = edit._target_invoice(sale_order)
        if not move or move.state == "cancel":
            return

        def _invoice_lines(so_line):
            if not so_line:
                return move.invoice_line_ids.browse()
            return move.invoice_line_ids.filtered(
                lambda l, sl=so_line: sl in l.sale_line_ids
            )

        product_lines = self._shopify_product_lines(sale_order)
        charge_inv = move.invoice_line_ids.filtered(
            lambda l: any(
                sl.shopify_discount_line or sl.shopify_shipping_line
                for sl in l.sale_line_ids
            )
        )

        need = False
        for so_line in keep_lines:
            inv = _invoice_lines(so_line)
            if not inv:
                need = True
                break
            if any(
                not amounts_close(l.price_unit, so_line.price_unit)
                or not amounts_close(l.quantity, 1.0)
                or l.name != so_line.name
                or l.product_id != so_line.product_id
                for l in inv
            ):
                need = True
                break
        if not need:
            for inv in charge_inv:
                if not (inv.sale_line_ids & keep_lines):
                    need = True
                    break
        if not need:
            for so_line in product_lines:
                inv_lines = _invoice_lines(so_line)
                if product_discount == "copy":
                    if any(
                        not discounts_close(inv.discount, so_line.discount)
                        for inv in inv_lines
                    ):
                        need = True
                        break
                elif inv_lines and any(inv.discount for inv in inv_lines):
                    need = True
                    break
        if not need and to_remove:
            if any(inv.sale_line_ids & to_remove for inv in charge_inv):
                need = True
        if not need:
            return

        order_name = order.get("name") or sale_order.name
        invoice_was_posted = move.state == "posted"
        captured_credit_ids = []
        if invoice_was_posted:
            captured_credit_ids = edit._capture_payments(move, _log, order_name)
            edit._reset_invoice(move, _log, order_name)
        elif move.state != "draft":
            _log(
                "warning",
                "Order %s: invoice %s is in state %s — cannot apply "
                "charge changes automatically."
                % (order_name, move.name, move.state),
            )
            return

        for so_line in product_lines:
            inv_lines = _invoice_lines(so_line)
            if not inv_lines:
                continue
            if product_discount == "copy":
                for inv in inv_lines:
                    if not discounts_close(inv.discount, so_line.discount):
                        inv.write({"discount": so_line.discount})
            elif any(inv.discount for inv in inv_lines):
                inv_lines.write({"discount": 0.0})

        for inv in charge_inv:
            if not (inv.sale_line_ids & keep_lines):
                inv.unlink()

        for so_line in keep_lines:
            inv = _invoice_lines(so_line)
            inv_vals = {
                "name": so_line.name,
                "quantity": 1.0,
                "price_unit": so_line.price_unit,
                "discount": 0.0,
                "tax_ids": [(6, 0, so_line.tax_ids.ids)],
            }
            if so_line.product_id:
                inv_vals["product_id"] = so_line.product_id.id
            if inv:
                inv[0].write(inv_vals)
                extra = inv[1:]
                if extra:
                    extra.unlink()
            else:
                inv_vals["sale_line_ids"] = [(4, so_line.id)]
                account = move.invoice_line_ids.filtered("account_id")[:1]
                if account:
                    inv_vals["account_id"] = account.account_id.id
                move.write({"invoice_line_ids": [(0, 0, inv_vals)]})

        if invoice_was_posted:
            move.action_post()
            _log(
                "info",
                "Order %s: invoice %s re-posted after charge sync."
                % (order_name, move.name),
            )
            shopify_order_id = order.get("id") or sale_order.shopify_order_id
            if shopify_order_id:
                edit._backfill_move_ref(move, shopify_order_id)
            api = self.env["shopify.api.client"]
            financial_status = (order.get("financial_status") or "").lower()
            edit._repay_invoice(
                move, captured_credit_ids, financial_status, api, _log, order_name,
            )

    @staticmethod
    def _norm_part(value):
        return (value or "").strip().casefold()

    def _address_key(self, street, street2, city, zip_code, country_code, state_code):
        return tuple(
            self._norm_part(v)
            for v in (street, street2, city, zip_code, country_code, state_code)
        )

    def _partner_address_key(self, partner):
        return self._address_key(
            partner.street,
            partner.street2,
            partner.city,
            partner.zip,
            partner.country_id.code,
            partner.state_id.code,
        )

    def _shopify_address_key(self, address):
        return self._address_key(
            address.get("address1"),
            address.get("address2"),
            address.get("city"),
            address.get("zip"),
            address.get("country_code"),
            address.get("province_code"),
        )

    def _address_dict_matches(self, partner, address):
        return bool(partner) and (
            self._partner_address_key(partner) == self._shopify_address_key(address)
        )

    def _same_partner_address(self, first, second):
        if not first or not second:
            return bool(first) == bool(second)
        return self._partner_address_key(first) == self._partner_address_key(second)

    def _get_or_create_delivery_contact(self, partner, address):
        """Existing child contact matching the address, else a new delivery
        contact under the customer."""
        return self._get_or_create_address_contact(partner, address, "delivery")

    def _get_or_create_address_contact(self, partner, address, contact_type):
        """Reuse a child with the same street/city/zip, else create one."""
        Partner = self.env["res.partner"].sudo()
        key = self._shopify_address_key(address)
        for child in partner.child_ids:
            if self._partner_address_key(child) == key:
                return child
        vals = {
            "name": (address.get("name") or "").strip() or partner.name,
            "parent_id": partner.id,
            "type": contact_type,
        }
        phone = (address.get("phone") or "").strip()
        if phone:
            vals["phone"] = phone
        vals.update(self._address_vals(address))
        return Partner.create(vals)

    # ------------------------------------------------------------------
    # sale order
    # ------------------------------------------------------------------
    def _match_product(self, sku, line_item=None, name=None, title=None, _log=None):
        """Match by Shopify variant id, import from Shopify if missing."""
        api = self.env["shopify.api.client"]
        if line_item and not name and not title:
            name = api.shopify_line_item_name(line_item)
            title = (line_item.get("title") or "").strip() or None
        product = self.env["shopify.product.sync"].match_or_import_for_order_line(
            sku,
            line_item=line_item,
            name=name,
            title=title,
            _log=_log,
        )
        if not product:
            variant_id = (
                line_item.get("variant_id")
                if isinstance(line_item, dict)
                else None
            )
            raise RuntimeError(
                "No Odoo product found for Shopify SKU '%s' (variant id %s; "
                "line %r). Searched shopify_variant_id, SKU, and name; "
                "automatic import runs when product sync is on and create "
                "mode is not update only."
                % (
                    sku,
                    normalize_shopify_variant_id(variant_id) or variant_id or "",
                    (name or title or "").strip(),
                )
            )
        return product

    def _create_sale_order(self, order, shopify_order_id, partner, _log):
        """Build the sale.order (draft) with one line per Shopify line_item.

        tax_id is deliberately omitted on lines so Odoo applies the product /
        fiscal-position taxes normally. Zero-quantity lines are skipped;
        SKU-less lines are skipped with a warning (mirrors the edit engine).
        """
        order_name = order.get("name") or str(shopify_order_id)
        skip_products = self._truthy(self._param("skip_product_create"))
        line_vals = []
        for item in order.get("line_items") or []:
            qty = line_item_quantity(item)
            if qty <= 0:
                continue  # zero-qty lines carry nothing to sell or invoice
            try:
                price = float(item.get("price") or 0)
            except (TypeError, ValueError):
                price = 0.0
            if skip_products:
                # Skip-product-create mode: no product lookup at all — lines
                # are built purely from Shopify data (Odoo allows product-less
                # sale lines; invoice lines created from them inherit this).
                title = (item.get("title") or "").strip()
                variant = (item.get("variant_title") or "").strip()
                line_name = (
                    " — ".join(part for part in (title, variant) if part)
                    or (item.get("sku") or "").strip()
                    or "Shopify line %s" % (item.get("id") or "")
                )
                line_vals.append(
                    (
                        0,
                        0,
                        {
                            "name": line_name,
                            "product_uom_qty": qty,
                            "price_unit": price,
                            "discount": (
                                round(line_discount_percent(item), 2)
                                if uses_line_percent_discount(order)
                                else 0.0
                            ),
                            # tax_id omitted: taxes come from the fiscal position.
                        },
                    )
                )
                continue
            sku = (item.get("sku") or "").strip()
            if not sku:
                _log(
                    "warning",
                    "Order %s: Shopify line '%s' (id %s) has no SKU — skipped."
                    % (order_name, item.get("title"), item.get("id")),
                )
                continue
            product = self._match_product(sku, line_item=item, _log=_log)
            line_vals.append(
                    (
                        0,
                        0,
                        {
                            "product_id": product.id,
                            "name": item.get("title") or product.name or sku,
                            "product_uom_qty": qty,
                            "price_unit": price,
                            "discount": (
                                round(line_discount_percent(item), 2)
                                if uses_line_percent_discount(order)
                                else 0.0
                            ),
                            # tax_id omitted on purpose: Odoo computes it from the
                            # product and fiscal position.
                        },
                    )
            )
        if not line_vals:
            if skip_products:
                raise RuntimeError(
                    "Shopify order %s (id %s) has no pullable lines — every "
                    "line is zero-quantity." % (order_name, shopify_order_id)
                )
            raise RuntimeError(
                "Shopify order %s (id %s) has no pullable lines — every line "
                "is zero-quantity or missing a SKU." % (order_name, shopify_order_id)
            )

        vals = {
            "partner_id": partner.id,
            "client_order_ref": order.get("name") or order_name,
            "shopify_order_id": str(shopify_order_id),
            "shopify_order_name": order_name,
            "shopify_discount_codes": applied_discount_codes_csv(order) or False,
            "order_line": line_vals,
        }
        # Prefer the order's shipping address as delivery contact when present.
        shipping_address = order.get("shipping_address") or {}
        if self._usable_shopify_address(shipping_address):
            shipping = self._get_or_create_delivery_contact(partner, shipping_address)
            vals["partner_shipping_id"] = shipping.id
            _log(
                "info",
                "Order %s: delivery address set from Shopify shipping address "
                "(%s)." % (order_name, shipping.display_name),
            )
        billing_address = order.get("billing_address") or {}
        if self._usable_shopify_address(billing_address):
            invoice = self._get_or_create_address_contact(
                partner, billing_address, "invoice"
            )
            vals["partner_invoice_id"] = invoice.id
            _log(
                "info",
                "Order %s: invoice address set from Shopify billing address "
                "(%s)." % (order_name, invoice.display_name),
            )
        date_order = self._parse_shopify_datetime(order.get("created_at"))
        if date_order:
            vals["date_order"] = date_order
        # Order fields pack: note + tags (both default-on unless disabled).
        if self._truthy_default_true(self._param("include_order_note")):
            note = (order.get("note") or "").strip()
            if note:
                vals["note"] = note
        if skip_products:
            _log(
                "info",
                "Order %s: skip_product_create is on — %d line(s) built "
                "without products." % (order_name, len(line_vals)),
            )
        sale_order = self.env["sale.order"].with_context(
            shopify_sync_origin="shopify"
        ).create(vals)
        self._apply_shopify_order_tags(sale_order, order, _log)
        return sale_order

    def _apply_shopify_order_tags(self, sale_order, order, _log=None):
        """Copy Shopify order tags onto shopify_tags and sale.order tag_ids.

        The Tags widget on the sale order is tag_ids (crm.tag), not the
        Char field this module used to write only at create time.
        """
        if not sale_order:
            return
        names = shopify_tag_names(order)
        vals = {}
        csv = tags_csv(names)
        if self._truthy_default_true(self._param("include_order_tags")):
            previous = (sale_order.shopify_tags or "").strip()
            if names or previous:
                if previous != csv:
                    vals["shopify_tags"] = csv or False
                tag_field = sale_order._fields.get("tag_ids")
                if tag_field is not None:
                    Tag = self.env[tag_field.comodel_name].sudo()
                    ids = []
                    for name in names:
                        tag = Tag.search([("name", "=ilike", name)], limit=1)
                        if not tag:
                            tag = Tag.create({"name": name})
                        ids.append(tag.id)
                    if set(sale_order.tag_ids.ids) != set(ids):
                        vals["tag_ids"] = [Command.set(ids)]
        if not vals:
            return
        sale_order.with_context(shopify_sync_origin="shopify").write(vals)
        if _log and ("shopify_tags" in vals or "tag_ids" in vals):
            _log(
                "info",
                "Order %s: tags synced (%s)."
                % (sale_order.name, csv or "cleared"),
            )

    def _apply_shopify_order_name(self, sale_order, order, _log):
        """When use_shopify_order_numbers is on (default when unset), rename
        the draft SO to order_prefix + Shopify order_number (e.g. '#1011').
        Runs right after create, while the SO is still draft; client_order_ref
        keeps order['name'] either way."""
        if not self._truthy_default_true(self._param("use_shopify_order_numbers")):
            return
        order_name = order.get("name") or ""
        prefix = self._param("order_prefix")
        if prefix is None:
            prefix = "#"
        number = order.get("order_number")
        if number in (None, ""):
            # Fallback: derive the number from the order name ('#1011').
            number = order_name.lstrip("#").strip()
        if str(number).strip() == "":
            _log(
                "warning",
                "Order %s: no Shopify order_number found — keeping the Odoo "
                "sequence name %s." % (order_name, sale_order.name),
            )
            return
        new_name = "%s%s" % (prefix, str(number).strip())
        if sale_order.name != new_name:
            old_name = sale_order.name
            sale_order.write({"name": new_name})
            _log(
                "info",
                "Order %s: sale order named %s (was %s) per "
                "use_shopify_order_numbers." % (order_name, new_name, old_name),
            )

    @staticmethod
    def _parse_shopify_datetime(value):
        """Shopify ISO 8601 ('2025-01-02T03:04:05Z' or with offset) ->
        fields.Datetime string (naive UTC), False when unparsable."""
        text = (value or "").strip() if isinstance(value, str) else ""
        if not text:
            return False
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return False
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return fields.Datetime.to_string(parsed)

    # ------------------------------------------------------------------
    # invoicing / payment
    # ------------------------------------------------------------------
    def _invoice_and_pay(self, sale_order, order, shopify_order_id, _log):
        """Create + post the invoice, backfill shopify_order_id, and register
        a full payment when Shopify says the order is paid."""
        order_name = order.get("name") or sale_order.client_order_ref or ""
        invoices = sale_order._create_invoices()
        if not invoices:
            _log(
                "warning",
                "Order %s: no invoice generated from sale order %s; skipping "
                "invoicing/payment." % (order_name, sale_order.name),
            )
            return
        invoices.action_post()
        for move in invoices:
            if not move.shopify_order_id:
                move.write({"shopify_order_id": str(shopify_order_id)})
        _log(
            "info",
            "Order %s: invoice(s) %s created and posted."
            % (order_name, ", ".join(invoices.mapped("name"))),
        )

        financial_status = (order.get("financial_status") or "").lower()
        if financial_status != "paid":
            _log(
                "info",
                "Order %s: Shopify financial_status is '%s' — invoice left "
                "open." % (order_name, financial_status or "unknown"),
            )
            return
        if not self._truthy_default_true(self._param("order_pull_auto_paid")):
            _log(
                "info",
                "Order %s: order_pull_auto_paid is off — invoice left open."
                % order_name,
            )
            return
        map_gateways = self._truthy(self._param("map_payment_gateways"))
        journal_id = None
        journal_label = "the wizard default journal"
        if map_gateways:
            journal = self._gateway_journal(order, _log)
            if journal:
                journal_id = journal.id
                journal_label = "journal '%s' (id %d)" % (journal.name, journal.id)
        if journal_id is None:
            journal_raw = (self._param("payment_journal_id") or "").strip()
            if journal_raw:
                try:
                    journal_id = int(journal_raw)
                    journal_label = "journal %d" % journal_id
                except (TypeError, ValueError):
                    _log(
                        "warning",
                        "Order %s: configured payment journal '%s' is not a "
                        "valid id — %s."
                        % (
                            order_name,
                            journal_raw,
                            "falling back to the wizard default journal"
                            if map_gateways
                            else "leaving the invoice open",
                        ),
                    )
                    if not map_gateways:
                        return
            elif not map_gateways:
                _log(
                    "warning",
                    "Order %s: Shopify order is paid but no payment journal is "
                    "configured (shopify_order_ops.payment_journal_id) — leaving "
                    "the invoice open." % order_name,
                )
                return

        wizard_vals = {"journal_id": journal_id} if journal_id else {}
        wizard = (
            self.env["account.payment.register"]
            .with_context(active_model="account.move", active_ids=invoices.ids)
            .create(wizard_vals)
        )
        wizard.action_create_payments()
        _log(
            "info",
            "Order %s: registered full payment on %s via %s."
            % (order_name, ", ".join(invoices.mapped("name")), journal_label),
        )

    def _gateway_journal(self, order, _log):
        """map_payment_gateways: account.journal (type 'bank') named
        'Card (<gateway>)' for the order's first payment gateway, falling
        back to 'Card (manual)' when Shopify lists none. Created with
        minimal values when missing. Empty recordset on any failure — the
        caller then falls back to the configured journal / wizard default."""
        order_name = order.get("name") or ""
        gateways = order.get("payment_gateway_names") or []
        gateway = str(gateways[0]).strip() if gateways else ""
        if not gateway:
            gateway = "manual"
        journal_name = "Card (%s)" % gateway
        Journal = self.env["account.journal"].sudo()
        try:
            journal = Journal.search(
                [("name", "=", journal_name), ("type", "=", "bank")], limit=1
            )
            if journal:
                return journal
            journal = Journal.create({"name": journal_name, "type": "bank"})
            _log(
                "info",
                "Order %s: created bank journal '%s' (id %d) for Shopify "
                "gateway '%s'." % (order_name, journal_name, journal.id, gateway),
            )
            return journal
        except Exception as exc:  # fall back to configured/default journal
            _log(
                "warning",
                "Order %s: could not resolve/create gateway journal '%s' "
                "(%s) — falling back." % (order_name, journal_name, exc),
            )
            return Journal.browse()
