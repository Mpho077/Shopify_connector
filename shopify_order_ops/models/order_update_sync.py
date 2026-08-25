import logging
import re

from odoo import api, models

from .shopify_discount import (
    DIRECTION_KEY as DISCOUNT_DIRECTION_KEY,
    PARAM_KEY as DISCOUNT_PARAM_KEY,
    as_float,
    clamp_percent,
    discount_sync_allows_odoo_to_shopify,
    discounts_close,
    is_charge_line,
    is_charge_sku,
    line_discount_amount,
    line_discount_percent,
)

_logger = logging.getLogger(__name__)

SOURCE = "order_update_push"
ADDRESS_SOURCE = "order_address_push"
DISCOUNT_SOURCE = "order_discount_push"
PARAM_PREFIX = "shopify_order_ops."

_ADDRESS_PARTNER_FIELDS = (
    "name",
    "street",
    "street2",
    "city",
    "zip",
    "country_id",
    "state_id",
    "phone",
)

# --- GraphQL -------------------------------------------------------------
# Verified against shopify.dev Admin API docs (2025-01 and latest):
# - orderEditBegin(id: ID!) -> calculatedOrder { id } + userErrors
# - orderEditAddVariant(id: ID!, variantId: ID!, quantity: Int!,
#   locationId: ID, allowDuplicates: Boolean) -> calculatedOrder,
#   calculatedLineItem, userErrors. NOTE: the documented schema has NO
#   `price` argument — the docs state the mutation "respects the variant's
#   contextual pricing" (i.e. the store catalog price is used). We still
#   attempt the contract's price: MoneyInput first (harmless if a newer API
#   version supports it) and transparently retry without it when the schema
#   rejects the argument (GraphQL validation error naming `price`).
# - orderEditCommit(id: ID!, notifyCustomer: Boolean, staffNote: String)
#   -> order { id } + userErrors.
_ORDER_EDIT_BEGIN = """
mutation orderEditBegin($id: ID!) {
  orderEditBegin(id: $id) {
    calculatedOrder { id }
    userErrors { field message }
  }
}
"""

_ORDER_EDIT_ADD_VARIANT = """
mutation orderEditAddVariant($id: ID!, $variantId: ID!, $quantity: Int!, $price: MoneyInput) {
  orderEditAddVariant(id: $id, variantId: $variantId, quantity: $quantity, price: $price) {
    calculatedOrder { id }
    calculatedLineItem { id }
    userErrors { field message }
  }
}
"""

_ORDER_EDIT_ADD_VARIANT_NO_PRICE = """
mutation orderEditAddVariant($id: ID!, $variantId: ID!, $quantity: Int!) {
  orderEditAddVariant(id: $id, variantId: $variantId, quantity: $quantity) {
    calculatedOrder { id }
    calculatedLineItem { id }
    userErrors { field message }
  }
}
"""

_ORDER_EDIT_COMMIT = """
mutation orderEditCommit($id: ID!, $notifyCustomer: Boolean!, $staffNote: String!) {
  orderEditCommit(id: $id, notifyCustomer: $notifyCustomer, staffNote: $staffNote) {
    order { id }
    userErrors { field message }
  }
}
"""

_VARIANT_BY_SKU = """
query variantBySku($skuQuery: String!) {
  productVariants(first: 5, query: $skuQuery) {
    edges { node { id sku } }
  }
}
"""

# userErrors that mean the line is already on the order (idempotent retry).
_ALREADY_PRESENT_RE = re.compile(r"already|duplicate|exists", re.I)
# GraphQL schema-validation rejection of the `price` argument.
_PRICE_ARG_RE = re.compile(r"argument[^\n]{0,80}price|price[^\n]{0,80}argument", re.I)


def _is_truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _format_user_errors(errors):
    parts = []
    for err in errors or []:
        field = ".".join(str(f) for f in (err.get("field") or []) if f is not None)
        message = err.get("message") or "unknown error"
        parts.append("%s: %s" % (field, message) if field else message)
    return "; ".join(parts)


_ORDER_EDIT_BEGIN_WITH_LINES = """
mutation($id: ID!) {
  orderEditBegin(id: $id) {
    calculatedOrder {
      id
      lineItems(first: 100) {
        edges { node { id quantity sku variant { id } } }
      }
    }
    userErrors { field message }
  }
}
"""

_ORDER_EDIT_SET_QTY = """
mutation($id: ID!, $lineItemId: ID!, $quantity: Int!) {
  orderEditSetQuantity(id: $id, lineItemId: $lineItemId, quantity: $quantity) {
    calculatedOrder { id }
    userErrors { field message }
  }
}
"""

_ORDER_EDIT_BEGIN_WITH_DISCOUNTS = """
mutation($id: ID!) {
  orderEditBegin(id: $id) {
    calculatedOrder {
      id
      lineItems(first: 100) {
        edges {
          node {
            id
            quantity
            sku
            variant { id }
            calculatedDiscountAllocations {
              discountApplication { id }
            }
          }
        }
      }
    }
    userErrors { field message }
  }
}
"""

_ORDER_EDIT_ADD_LINE_DISCOUNT = """
mutation($id: ID!, $lineItemId: ID!, $discount: OrderEditAppliedDiscountInput!) {
  orderEditAddLineItemDiscount(id: $id, lineItemId: $lineItemId, discount: $discount) {
    calculatedOrder { id }
    userErrors { field message }
  }
}
"""

_ORDER_EDIT_UPDATE_DISCOUNT = """
mutation($id: ID!, $discountApplicationId: ID!, $discount: OrderEditAppliedDiscountInput!) {
  orderEditUpdateDiscount(id: $id, discountApplicationId: $discountApplicationId, discount: $discount) {
    calculatedOrder { id }
    userErrors { field message }
  }
}
"""

_ORDER_UPDATE = """
mutation orderUpdate($input: OrderInput!) {
  orderUpdate(input: $input) {
    order {
      id
      shippingAddress {
        address1
        address2
        city
        zip
        provinceCode
        countryCode
      }
    }
    userErrors { field message }
  }
}
"""


class SaleOrder(models.Model):
    """Enqueue a Shopify address push when delivery or invoice address changes."""

    _inherit = "sale.order"

    def write(self, vals):
        res = super().write(vals)
        if "partner_shipping_id" in vals:
            try:
                self._propagate_shipping_address_to_pickings()
            except Exception:  # noqa: BLE001 - sync must never break SO edits
                _logger.exception(
                    "order_address_sync: picking propagation failed"
                )
        if "partner_invoice_id" in vals:
            try:
                self._propagate_billing_address_to_draft_invoices()
            except Exception:  # noqa: BLE001 - sync must never break SO edits
                _logger.exception(
                    "order_address_sync: draft invoice partner update failed"
                )
        if "partner_shipping_id" in vals or "partner_invoice_id" in vals:
            try:
                self._enqueue_shopify_address_pushes()
            except Exception:  # noqa: BLE001 - sync must never break SO edits
                _logger.exception("order_address_push: SO enqueue pass failed")
        return res

    def _propagate_shipping_address_to_pickings(self):
        """Update open outgoing deliveries when the SO delivery address changes.

        Odoo core deliberately does NOT touch existing pickings — it only
        schedules a warning activity ("You should probably update the partner
        on this document"). With address sync enabled we do what that activity
        asks for: set the new address on every not-yet-done outgoing delivery
        and dismiss the now-obsolete warning. Runs for both directions
        (Shopify -> Odoo writes carry shopify_sync_origin; manual Odoo edits
        don't), gated only by the feature toggle."""
        if not _address_propagation_enabled(self.env):
            return
        log = self.env["shopify.sync.log"].sudo()
        for order in self:
            shipping = order.partner_shipping_id
            if not shipping:
                continue
            pickings = order.picking_ids.filtered(
                lambda p: p.picking_type_code == "outgoing"
                and p.state not in ("done", "cancel")
                and p.partner_id != shipping
            )
            if not pickings:
                continue
            pickings.write({"partner_id": shipping.id})
            self._dismiss_address_warning_activities(pickings)
            if order.shopify_order_id or order.client_order_ref:
                log.log_event(
                    "info",
                    "Delivery address on %s applied to open delivery "
                    "order(s) %s."
                    % (order.name, ", ".join(pickings.mapped("name"))),
                    source="order_address_sync",
                    shopify_order_ref=order.shopify_order_name or order.name,
                )

    def _propagate_billing_address_to_draft_invoices(self):
        """Keep draft customer invoices on the SO's invoice address.

        Posted invoices are left alone (same idea as not touching done
        deliveries)."""
        if not _address_propagation_enabled(self.env):
            return
        for order in self:
            invoice_partner = order.partner_invoice_id
            if not invoice_partner:
                continue
            drafts = order.invoice_ids.filtered(
                lambda m: m.move_type == "out_invoice"
                and m.state == "draft"
                and m.partner_id != invoice_partner
            )
            if drafts:
                drafts.write({"partner_id": invoice_partner.id})

    def _dismiss_address_warning_activities(self, pickings):
        """Best-effort removal of core's 'delivery address has been changed'
        warning activities on pickings we just updated — the advice they give
        has been carried out automatically."""
        try:
            warning_type = self.env.ref(
                "mail.mail_activity_data_warning", raise_if_not_found=False
            )
            if not warning_type:
                return
            activities = self.env["mail.activity"].sudo().search(
                [
                    ("res_model", "=", "stock.picking"),
                    ("res_id", "in", pickings.ids),
                    ("activity_type_id", "=", warning_type.id),
                    ("note", "ilike", "delivery address has been changed"),
                ]
            )
            activities.unlink()
        except Exception:  # noqa: BLE001 - cosmetic cleanup only
            _logger.exception(
                "order_address_sync: could not dismiss address warning "
                "activities"
            )

    def _enqueue_shopify_address_pushes(self):
        if self.env.context.get("shopify_sync_origin") == "shopify":
            return
        if not _address_sync_allows_odoo_to_shopify(self.env):
            return
        log = self.env["shopify.sync.log"]
        for order in self:
            try:
                _enqueue_address_push_for_order(order, log)
            except Exception as exc:  # noqa: BLE001 - per-order isolation
                _logger.exception(
                    "order_address_push: enqueue failed for SO %s: %s",
                    order.id, exc,
                )


class ResPartner(models.Model):
    """When a delivery or invoice contact's address is edited in place, push to Shopify."""

    _inherit = "res.partner"

    def write(self, vals):
        res = super().write(vals)
        if not any(field in vals for field in _ADDRESS_PARTNER_FIELDS):
            return res
        try:
            self._enqueue_shopify_address_pushes_from_partner()
        except Exception:  # noqa: BLE001 - sync must never break partner edits
            _logger.exception("order_address_push: partner enqueue pass failed")
        return res

    def _enqueue_shopify_address_pushes_from_partner(self):
        if self.env.context.get("shopify_sync_origin") == "shopify":
            return
        if not _address_sync_allows_odoo_to_shopify(self.env):
            return
        log = self.env["shopify.sync.log"]
        SaleOrder = self.env["sale.order"].sudo()
        for partner in self:
            orders = SaleOrder.search(
                [
                    "|",
                    ("partner_shipping_id", "=", partner.id),
                    ("partner_invoice_id", "=", partner.id),
                    ("shopify_order_id", "!=", False),
                    ("state", "in", ("sale", "done")),
                ]
            )
            for order in orders:
                try:
                    _enqueue_address_push_for_order(order, log)
                except Exception as exc:  # noqa: BLE001
                    _logger.exception(
                        "order_address_push: partner enqueue failed for SO %s: %s",
                        order.id, exc,
                    )


def _address_propagation_enabled(env):
    raw = env["ir.config_parameter"].sudo().get_param(
        PARAM_PREFIX + "address_propagation_enabled"
    )
    # Default ON when unset (matches Shopify -> Odoo pull engine).
    if raw is None or str(raw).strip() == "":
        return True
    return _is_truthy(raw)


def _address_sync_direction(env):
    """Return configured direction; default two_way when unset."""
    raw = (
        env["ir.config_parameter"]
        .sudo()
        .get_param(PARAM_PREFIX + "address_sync_direction")
        or ""
    ).strip()
    if raw in ("shopify_to_odoo", "odoo_to_shopify", "two_way"):
        return raw
    return "two_way"


def _address_sync_allows_shopify_to_odoo(env):
    if not _address_propagation_enabled(env):
        return False
    return _address_sync_direction(env) in ("shopify_to_odoo", "two_way")


def _address_sync_allows_odoo_to_shopify(env):
    if not _address_propagation_enabled(env):
        return False
    return _address_sync_direction(env) in ("odoo_to_shopify", "two_way")


def _discount_sync_allows_odoo_to_shopify(env):
    icp = env["ir.config_parameter"].sudo()
    return discount_sync_allows_odoo_to_shopify(
        icp.get_param(PARAM_PREFIX + DISCOUNT_PARAM_KEY),
        icp.get_param(PARAM_PREFIX + DISCOUNT_DIRECTION_KEY),
    )


def _order_has_done_outgoing(order):
    return bool(
        order.picking_ids.filtered(
            lambda p: p.picking_type_code == "outgoing" and p.state == "done"
        )
    )


def _address_push_pending(Job, order):
    candidates = Job.search(
        [
            ("job_type", "=", "order_address_push"),
            ("state", "in", ["pending", "processing"]),
            ("payload", "ilike", '"so_id": %s' % order.id),
        ]
    )
    for job in candidates:
        if job.payload_dict().get("so_id") == order.id:
            return True
    return False


def _enqueue_address_push_for_order(order, log):
    if (
        not order
        or not order.shopify_order_id
        or order.state not in ("sale", "done")
    ):
        return
    can_push_shipping = bool(order.partner_shipping_id) and not _order_has_done_outgoing(
        order
    )
    can_push_billing = bool(order.partner_invoice_id)
    if not can_push_shipping and not can_push_billing:
        if order.partner_shipping_id and _order_has_done_outgoing(order):
            log.log_event(
                "warning",
                "Address push skipped for SO %s: already has a done "
                "outgoing delivery and no invoice address to push."
                % order.name,
                source=ADDRESS_SOURCE,
                shopify_order_ref=order.shopify_order_name or order.name,
            )
        return
    Job = order.env["shopify.sync.job"].sudo()
    if _address_push_pending(Job, order):
        log.log_event(
            "info",
            "Address push for SO %s skipped: a pending/processing "
            "order_address_push job already covers this order." % order.name,
            source=ADDRESS_SOURCE,
            shopify_order_ref=order.shopify_order_name or order.name,
        )
        return
    payload = {
        "shopify_order_id": order.shopify_order_id,
        "so_id": order.id,
        "so_name": order.name,
        "push_shipping": can_push_shipping,
        "push_billing": can_push_billing,
    }
    if order.partner_shipping_id:
        payload["partner_shipping_id"] = order.partner_shipping_id.id
    if order.partner_invoice_id:
        payload["partner_invoice_id"] = order.partner_invoice_id.id
    job = Job.enqueue_and_process(
        name="order_address_push %s" % (order.shopify_order_name or order.name),
        job_type="order_address_push",
        payload_dict=payload,
    )
    log.log_event(
        "info",
        "Queued Shopify address push for SO %s (job %s, shipping=%s, "
        "billing=%s, real-time processing triggered)."
        % (order.name, job.id, can_push_shipping, can_push_billing),
        source=ADDRESS_SOURCE,
        shopify_order_ref=order.shopify_order_name or order.name,
        job=job,
    )


class SaleOrderLine(models.Model):
    """Enqueue a Shopify order-update push for lines added in Odoo.

    Fires only for genuine product lines on confirmed, Shopify-linked sale
    orders, and never for Shopify-originated writes (echo guard). The hook
    must never raise into the caller: enqueue problems are logged, not
    propagated, so a user's SO edit can never be broken by sync bookkeeping.
    """

    _inherit = "sale.order.line"

    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        try:
            lines._enqueue_shopify_order_update_push()
        except Exception:  # noqa: BLE001 - sync must never break SO edits
            _logger.exception("order_update_push: enqueue pass failed")
        return lines

    def write(self, vals):
        """Qty increases enqueue a qty push; Disc.% changes enqueue a discount push."""
        pre_qty = (
            {line.id: line.product_uom_qty for line in self}
            if "product_uom_qty" in vals
            else None
        )
        pre_disc = (
            {line.id: line.discount for line in self}
            if "discount" in vals
            else None
        )
        res = super().write(vals)
        if pre_qty is not None:
            try:
                self._enqueue_shopify_qty_pushes(pre_qty)
            except Exception:  # noqa: BLE001 - sync must never break SO edits
                _logger.exception("order_update_push: qty enqueue pass failed")
        if pre_disc is not None:
            try:
                self._enqueue_shopify_discount_pushes(pre_disc)
            except Exception:  # noqa: BLE001 - sync must never break SO edits
                _logger.exception("order_discount_push: enqueue pass failed")
        return res

    def _enqueue_shopify_qty_pushes(self, pre_quantities):
        """After a qty change: enqueue a 'qty' push per increased line."""
        if self.env.context.get("shopify_sync_origin") == "shopify":
            return
        enabled = self.env["ir.config_parameter"].sudo().get_param(
            PARAM_PREFIX + "order_update_push_enabled"
        )
        if not _is_truthy(enabled):
            return
        log = self.env["shopify.sync.log"]
        for line in self:
            order = line.order_id
            if (
                not order
                or not order.shopify_order_id
                or order.state not in ("sale", "done")
                or line.display_type
                or is_charge_line(line)
                or not line.product_id
            ):
                continue
            delta = line.product_uom_qty - (pre_quantities.get(line.id) or 0.0)
            if delta <= 0:
                if delta < 0:
                    log.log_event(
                        "info",
                        "SO %s line %s quantity decreased in Odoo — Shopify "
                        "quantity decreases are not auto-pushed; adjust in "
                        "Shopify if needed." % (order.name, line.id),
                        source=SOURCE,
                        shopify_order_ref=order.shopify_order_name or order.name,
                    )
                continue
            try:
                if line._order_update_push_pending(
                    self.env["shopify.sync.job"].sudo(), order
                ):
                    continue
                self.env["shopify.sync.job"].sudo().enqueue(
                    name="order_update_push %s"
                    % (order.shopify_order_name or order.name),
                    job_type="order_update_push",
                    payload_dict={
                        "mode": "qty",
                        "shopify_order_id": order.shopify_order_id,
                        "so_id": order.id,
                        "so_name": order.name,
                        "so_line_id": line.id,
                        "product_id": line.product_id.id,
                        "delta": delta,
                    },
                )
            except Exception as exc:  # noqa: BLE001 - per-line isolation
                _logger.exception(
                    "order_update_push: qty enqueue failed for line %s: %s",
                    line.id, exc,
                )

    def _enqueue_shopify_discount_pushes(self, pre_discounts):
        """After a Disc.% change: enqueue an Odoo → Shopify discount push."""
        if self.env.context.get("shopify_sync_origin") == "shopify":
            return
        if not _discount_sync_allows_odoo_to_shopify(self.env):
            return
        log = self.env["shopify.sync.log"]
        Job = self.env["shopify.sync.job"].sudo()
        for line in self:
            order = line.order_id
            if (
                not order
                or not order.shopify_order_id
                or order.state not in ("sale", "done")
                or line.display_type
                or is_charge_line(line)
                or not line.product_id
            ):
                continue
            new = clamp_percent(line.discount)
            old = clamp_percent(pre_discounts.get(line.id))
            if discounts_close(old, new):
                continue
            try:
                if line._discount_push_pending(Job, order):
                    continue
                job = Job.enqueue_and_process(
                    name="order_discount_push %s"
                    % (order.shopify_order_name or order.name),
                    job_type="order_discount_push",
                    payload_dict={
                        "shopify_order_id": order.shopify_order_id,
                        "so_id": order.id,
                        "so_name": order.name,
                        "so_line_id": line.id,
                        "product_id": line.product_id.id,
                        "discount": new,
                    },
                )
                log.log_event(
                    "info",
                    "Queued Shopify discount push for SO %s line %s "
                    "(%.2f%% -> %.2f%%, job %s, real-time processing "
                    "triggered)."
                    % (order.name, line.id, old, new, job.id),
                    source=DISCOUNT_SOURCE,
                    shopify_order_ref=order.shopify_order_name or order.name,
                    job=job,
                )
            except Exception as exc:  # noqa: BLE001 - per-line isolation
                _logger.exception(
                    "order_discount_push: enqueue failed for line %s: %s",
                    line.id, exc,
                )

    def _discount_push_pending(self, Job, order):
        """A queued order_discount_push already covers this sale line."""
        self.ensure_one()
        candidates = Job.search(
            [
                ("job_type", "=", "order_discount_push"),
                ("state", "in", ["pending", "processing"]),
                ("payload", "ilike", '"so_line_id": %s' % self.id),
            ]
        )
        for job in candidates:
            payload = job.payload_dict()
            if payload.get("so_line_id") == self.id:
                return True
        return False

    def _enqueue_shopify_order_update_push(self):
        # Echo guard: the Shopify-originated edit engine writes lines with
        # this context key; pushing those back would create a sync loop.
        if self.env.context.get("shopify_sync_origin") == "shopify":
            return
        enabled = self.env["ir.config_parameter"].sudo().get_param(
            PARAM_PREFIX + "order_update_push_enabled"
        )
        if not _is_truthy(enabled):
            return
        log = self.env["shopify.sync.log"]
        for line in self:
            order = line.order_id
            if (
                not order
                or not order.shopify_order_id
                or order.state not in ("sale", "done")
                or line.display_type  # product lines only (no section/note)
                or is_charge_line(line)
                or not line.product_id
            ):
                continue
            try:
                line._enqueue_one_order_update_push(order, log)
            except Exception as exc:  # noqa: BLE001 - per-line isolation
                try:
                    log.log_event(
                        "error",
                        "Failed to enqueue Shopify order update push for SO %s "
                        "line %s: %s" % (order.name, line.id, exc),
                        source=SOURCE,
                        shopify_order_ref=order.shopify_order_name or order.name,
                    )
                except Exception:  # noqa: BLE001
                    _logger.exception("order_update_push: error logging failed")

    def _enqueue_one_order_update_push(self, order, log):
        self.ensure_one()
        Job = self.env["shopify.sync.job"].sudo()
        if self._order_update_push_pending(Job, order):
            log.log_event(
                "info",
                "Shopify order update push for SO %s line %s skipped: a "
                "pending/processing order_update_push job already covers this "
                "order+product." % (order.name, self.id),
                source=SOURCE,
                shopify_order_ref=order.shopify_order_name or order.name,
            )
            return
        Job.enqueue(
            name="order_update_push %s" % (order.shopify_order_name or order.name),
            job_type="order_update_push",
            payload_dict={
                "shopify_order_id": order.shopify_order_id,
                "so_id": order.id,
                "so_name": order.name,
                "so_line_id": self.id,
                "product_id": self.product_id.id,
                "qty": self.product_uom_qty,
                "price_unit": self.price_unit,
            },
        )

    def _order_update_push_pending(self, Job, order):
        """A queued order_update_push job already covers this line/product.

        Prefilter via payload ilike (per contract), then confirm with an
        exact payload match so id prefix collisions ('"so_id": 5' vs 51)
        can never silently suppress a legitimate push.
        """
        self.ensure_one()
        candidates = Job.search(
            [
                ("job_type", "=", "order_update_push"),
                ("state", "in", ["pending", "processing"]),
                "|",
                ("payload", "ilike", '"so_line_id": %s' % self.id),
                "&",
                ("payload", "ilike", '"so_id": %s' % order.id),
                ("payload", "ilike", '"product_id": %s' % self.product_id.id),
            ]
        )
        for job in candidates:
            payload = job.payload_dict()
            if payload.get("so_line_id") == self.id:
                return True
            if (
                payload.get("so_id") == order.id
                and payload.get("product_id") == self.product_id.id
            ):
                return True
        return False


class ShopifyOrderUpdateEngine(models.AbstractModel):
    """Pushes Odoo-side sale order line additions to Shopify orders.

    Contract (implemented by agent task P) — see method docstrings.
    """

    _name = "shopify.order.update.engine"
    _description = "Shopify Order Update Push Engine"

    def process_order_address_push(self, job):
        """Push Odoo delivery address onto the Shopify order.

        Shipping uses GraphQL ``orderUpdate.shippingAddress``. Billing is
        not sent: Shopify's REST PUT ``billing_address`` returns HTTP 200
        without changing the order, and GraphQL ``orderUpdate`` has no
        billing field. Billing stays Shopify → Odoo on the sale order
        Invoice Address. Job payload may include push_shipping /
        push_billing flags.
        """
        log = self.env["shopify.sync.log"]
        api = self.env["shopify.api.client"]
        payload = job.payload_dict()
        so_name = payload.get("so_name") or "?"

        def _log(level, message):
            log.log_event(
                level, message, source=ADDRESS_SOURCE, job=job,
                shopify_order_ref=so_name,
            )

        if not _address_sync_allows_odoo_to_shopify(self.env):
            _log(
                "info",
                "Address push skipped: order address sync is off or "
                "direction is Shopify -> Odoo only (Settings -> Shopify Ops).",
            )
            return

        shopify_order_id = str(payload.get("shopify_order_id") or "").strip()
        if not shopify_order_id:
            raise RuntimeError(
                "Order address push job %s: payload has no shopify_order_id."
                % job.name
            )

        so = self.env["sale.order"].sudo().browse(payload.get("so_id") or 0)
        if not so.exists():
            _log(
                "warning",
                "Address push skipped: SO %s no longer exists."
                % payload.get("so_id"),
            )
            return

        push_shipping = payload.get("push_shipping")
        if push_shipping is None:
            push_shipping = bool(so.partner_shipping_id) and not _order_has_done_outgoing(so)
        push_billing = payload.get("push_billing")
        if push_billing is None:
            push_billing = bool(so.partner_invoice_id)

        if push_shipping and _order_has_done_outgoing(so):
            _log(
                "warning",
                "Shipping address push skipped for SO %s: already has a done "
                "outgoing delivery." % so.name,
            )
            push_shipping = False

        pushed = []
        if push_shipping:
            partner = so.partner_shipping_id
            if not partner:
                _log(
                    "warning",
                    "Shipping address push skipped for SO %s: no delivery "
                    "address set." % so.name,
                )
            else:
                shipping = self._partner_to_shopify_shipping_address(partner)
                if not self._mailing_address_usable(shipping):
                    _log(
                        "warning",
                        "Shipping address push skipped for SO %s: delivery "
                        "contact %s has no usable street/city/zip."
                        % (so.name, partner.display_name),
                    )
                else:
                    order_gid = "gid://shopify/Order/%s" % shopify_order_id
                    data = api.graphql(
                        _ORDER_UPDATE,
                        {"input": {"id": order_gid, "shippingAddress": shipping}},
                    )
                    result = (data or {}).get("orderUpdate") or {}
                    errors = result.get("userErrors") or []
                    if errors:
                        raise RuntimeError(
                            "orderUpdate shipping address failed for Shopify "
                            "order %s (SO %s): %s"
                            % (shopify_order_id, so_name, _format_user_errors(errors))
                        )
                    pushed.append("shipping")
                    _log(
                        "info",
                        "Pushed shipping address from SO %s (%s) to Shopify "
                        "order %s."
                        % (so.name, partner.display_name, shopify_order_id),
                    )

        if push_billing:
            _log(
                "warning",
                "Billing address was not sent to Shopify for SO %s: Shopify "
                "does not change billing on an existing order (REST PUT "
                "returns success with no update; GraphQL orderUpdate has no "
                "billing field). Billing sync is Shopify → Odoo only — look "
                "at the sale order Invoice Address, not the customer form."
                % so.name,
            )

        if not pushed and not push_billing:
            _log(
                "info",
                "Address push for SO %s: nothing to send to Shopify."
                % so.name,
            )

    @staticmethod
    def _mailing_address_usable(address):
        return bool(
            address.get("address1") or address.get("city") or address.get("zip")
        )

    def process_order_discount_push(self, job):
        """Push the Odoo sale-line Disc.% onto the matching Shopify line.

        Shopify will not remove discounts that were applied when the order
        was created (``This discount was applied to the order and can't be
        removed``). So we never call orderEditRemoveDiscount on those.

        - If Shopify needs a larger discount: add a line discount for the
          gap (percentage when the line currently has none, otherwise a
          fixed amount so percents do not stack).
        - If Shopify needs a smaller discount: try orderEditUpdateDiscount;
          if Shopify refuses, fail visibly so ops can change it in admin.
        """
        log = self.env["shopify.sync.log"]
        api = self.env["shopify.api.client"]
        payload = job.payload_dict()
        so_name = payload.get("so_name") or "?"

        def _log(level, message):
            log.log_event(
                level, message, source=DISCOUNT_SOURCE, job=job,
                shopify_order_ref=so_name,
            )

        if not _discount_sync_allows_odoo_to_shopify(self.env):
            _log(
                "info",
                "Discount push skipped: order discount sync is off or "
                "direction is Shopify -> Odoo only (Settings -> Shopify Ops).",
            )
            return

        shopify_order_id = str(payload.get("shopify_order_id") or "").strip()
        if not shopify_order_id:
            raise RuntimeError(
                "Order discount push job %s: payload has no shopify_order_id."
                % job.name
            )

        so_line = (
            self.env["sale.order.line"].sudo().browse(payload.get("so_line_id") or 0)
        )
        if not so_line.exists():
            _log(
                "warning",
                "Discount push skipped: sale line %s no longer exists."
                % payload.get("so_line_id"),
            )
            return
        if is_charge_line(so_line):
            _log(
                "info",
                "Discount push skipped: sale line %s is a Shopify charge line."
                % so_line.id,
            )
            return
        so = so_line.order_id
        product = so_line.product_id
        target = clamp_percent(so_line.discount)
        sku = (product.default_code or "").strip() if product else ""

        shopify_order = api.get_order(shopify_order_id)
        if not shopify_order:
            raise RuntimeError(
                "Discount push for SO %s: Shopify order %s not found."
                % (so_name, shopify_order_id)
            )
        shopify_item = self._shopify_line_by_sku(shopify_order, sku)
        if not shopify_item:
            raise RuntimeError(
                "Discount push for SO %s: no Shopify line with SKU '%s' "
                "on order %s." % (so_name, sku or "none", shopify_order_id)
            )
        current = line_discount_percent(shopify_item)
        if discounts_close(current, target):
            _log(
                "info",
                "Discount push for SO %s SKU %s skipped: Shopify already "
                "at %.2f%%." % (so_name, sku, current),
            )
            return

        variant_gid = self._resolve_variant_gid(api, product) if product else ""
        order_gid = "gid://shopify/Order/%s" % shopify_order_id
        begin = api.graphql(_ORDER_EDIT_BEGIN_WITH_DISCOUNTS, {"id": order_gid}).get(
            "orderEditBegin"
        ) or {}
        errors = begin.get("userErrors") or []
        if errors:
            raise RuntimeError(
                "Shopify order %s (id %s) cannot be edited to push a "
                "discount: %s"
                % (so_name, shopify_order_id, _format_user_errors(errors))
            )
        calc = begin.get("calculatedOrder") or {}
        calc_gid = calc.get("id")
        if not calc_gid:
            raise RuntimeError(
                "orderEditBegin for Shopify order %s returned no "
                "calculatedOrder id." % so_name
            )
        target_line = self._calculated_line_for_product(
            calc, variant_gid, sku
        )
        if not target_line:
            raise RuntimeError(
                "Variant %s (SKU %s) not found on Shopify order %s — "
                "cannot push discount."
                % (variant_gid or "unknown", sku or "none", so_name)
            )

        app_ids = []
        for alloc in target_line.get("calculatedDiscountAllocations") or []:
            app_id = (alloc.get("discountApplication") or {}).get("id")
            if app_id:
                app_ids.append(app_id)

        description = "Odoo SO %s" % (so.name or so_name)
        currency = (shopify_order.get("currency") or "").strip() or (
            self.env.company.currency_id.name or "AUD"
        )
        action = self._apply_discount_edit(
            api,
            calc_gid,
            target_line["id"],
            app_ids,
            shopify_item,
            current,
            target,
            currency,
            description,
            so_name,
        )
        if action == "unchanged":
            _log(
                "info",
                "Discount push for SO %s SKU %s: Shopify already matches "
                "after edit begin; nothing to commit." % (so_name, sku),
            )
            return

        commit = api.graphql(
            _ORDER_EDIT_COMMIT,
            {
                "id": calc_gid,
                "notifyCustomer": False,
                "staffNote": "Discount %s to %.2f%% via Odoo SO %s"
                % (action, target, so.name or so_name),
            },
        ).get("orderEditCommit") or {}
        errors = commit.get("userErrors") or []
        if errors:
            raise RuntimeError(
                "orderEditCommit failed for Shopify order %s discount "
                "push: %s" % (so_name, _format_user_errors(errors))
            )
        _log(
            "info",
            "Pushed Disc.%% from SO %s SKU %s to Shopify order %s: "
            "%.2f%% -> %.2f%% (%s, customer not notified)."
            % (so.name, sku, shopify_order_id, current, target, action),
        )

    @staticmethod
    def _shopify_line_by_sku(order, sku):
        if not sku:
            return None
        want = sku.strip().lower()
        for item in order.get("line_items") or []:
            if (item.get("sku") or "").strip().lower() == want:
                return item
        return None

    @staticmethod
    def _calculated_line_for_product(calc, variant_gid, sku):
        sku_l = (sku or "").strip().lower()
        for edge in (calc.get("lineItems") or {}).get("edges") or []:
            node = edge.get("node") or {}
            node_variant = (node.get("variant") or {}).get("id") or ""
            node_sku = (node.get("sku") or "").strip().lower()
            if (variant_gid and node_variant == variant_gid) or (
                sku_l and node_sku == sku_l
            ):
                return node
        return None

    def _apply_discount_edit(
        self,
        api,
        calc_gid,
        line_id,
        app_ids,
        shopify_item,
        current,
        target,
        currency,
        description,
        so_name,
    ):
        """Stage the discount change. Never removes original-order discounts."""
        qty = as_float(shopify_item.get("quantity"))
        price = as_float(shopify_item.get("price"))
        gross = price * qty
        current_amount = line_discount_amount(shopify_item)
        target_amount = round(gross * target / 100.0, 2) if gross else 0.0
        extra = round(target_amount - current_amount, 2)

        if extra > 0.009:
            if app_ids:
                try:
                    self._update_calculated_discount(
                        api, calc_gid, app_ids[0], target, description, so_name
                    )
                    return "updated"
                except RuntimeError:
                    # Original checkout/manual discounts often cannot be
                    # updated or removed. Add the gap as a new line discount.
                    pass
            if discounts_close(current, 0):
                discount = {
                    "percentValue": target,
                    "description": description,
                }
            else:
                discount = {
                    "fixedValue": {
                        "amount": "%.2f" % extra,
                        "currencyCode": currency,
                    },
                    "description": description,
                }
            self._add_calculated_line_discount(
                api, calc_gid, line_id, discount, so_name
            )
            return "added"

        if extra < -0.009:
            if not app_ids:
                raise RuntimeError(
                    "Shopify order %s already has more discount (%.2f%%) than "
                    "Odoo Disc.%% (%.2f%%) and there is no editable discount "
                    "application to update. Change the discount in Shopify "
                    "admin." % (so_name, current, target)
                )
            try:
                self._update_calculated_discount(
                    api, calc_gid, app_ids[0], target, description, so_name
                )
            except RuntimeError as exc:
                raise RuntimeError(
                    "Shopify will not let us reduce the original discount on "
                    "order %s (%.2f%% -> %.2f%%). Change it in Shopify admin. "
                    "%s" % (so_name, current, target, exc)
                ) from exc
            return "updated"

        return "unchanged"

    def _add_calculated_line_discount(
        self, api, calc_gid, line_id, discount, so_name
    ):
        result = api.graphql(
            _ORDER_EDIT_ADD_LINE_DISCOUNT,
            {
                "id": calc_gid,
                "lineItemId": line_id,
                "discount": discount,
            },
        ).get("orderEditAddLineItemDiscount") or {}
        errors = result.get("userErrors") or []
        if errors:
            raise RuntimeError(
                "orderEditAddLineItemDiscount failed for Shopify order %s: "
                "%s" % (so_name, _format_user_errors(errors))
            )

    def _update_calculated_discount(
        self, api, calc_gid, app_id, percent, description, so_name
    ):
        result = api.graphql(
            _ORDER_EDIT_UPDATE_DISCOUNT,
            {
                "id": calc_gid,
                "discountApplicationId": app_id,
                "discount": {
                    "percentValue": percent,
                    "description": description,
                },
            },
        ).get("orderEditUpdateDiscount") or {}
        errors = result.get("userErrors") or []
        if errors:
            raise RuntimeError(
                "orderEditUpdateDiscount failed for Shopify order %s: %s"
                % (so_name, _format_user_errors(errors))
            )

    def _partner_to_shopify_shipping_address(self, partner):
        """Map an Odoo delivery partner to Shopify MailingAddressInput fields."""
        first, last = self._split_partner_name(partner.name)
        address = {
            "firstName": first or (partner.name or "").strip() or "Customer",
            "lastName": last or "-",
        }
        if partner.street:
            address["address1"] = partner.street
        if partner.street2:
            address["address2"] = partner.street2
        if partner.city:
            address["city"] = partner.city
        if partner.zip:
            address["zip"] = partner.zip
        if partner.country_id and partner.country_id.code:
            address["countryCode"] = partner.country_id.code
        if partner.state_id and partner.state_id.code:
            address["provinceCode"] = partner.state_id.code
        if partner.phone:
            address["phone"] = partner.phone
        return address

    def _partner_to_shopify_rest_address(self, partner):
        """Map an Odoo partner to Shopify REST billing_address fields."""
        gql = ShopifyOrderUpdateEngine._partner_to_shopify_shipping_address(
            self, partner
        )
        key_map = (
            ("firstName", "first_name"),
            ("lastName", "last_name"),
            ("address1", "address1"),
            ("address2", "address2"),
            ("city", "city"),
            ("zip", "zip"),
            ("countryCode", "country_code"),
            ("provinceCode", "province_code"),
            ("phone", "phone"),
        )
        return {rest: gql[gql_key] for gql_key, rest in key_map if gql_key in gql}

    @staticmethod
    def _split_partner_name(name):
        parts = (name or "").strip().split(None, 1)
        first = parts[0] if parts else ""
        last = parts[1] if len(parts) > 1 else ""
        return first, last

    def process_order_update_push(self, job):
        """Push one queued line addition to Shopify.

        Job payload: {
            "shopify_order_id": str,
            "so_id": int,
            "so_name": str,
            "product_id": int,
            "qty": float,
            "price_unit": float,
        }

        Required behaviour:
        - Respect config `order_update_push_enabled`; when off, log info and
          return (no raise).
        - Resolve the Shopify variant ID: product.product.shopify_variant_id
          first; else GraphQL productVariants query by SKU (default_code);
          else raise RuntimeError (visible failure — product not linked).
        - Shopify order editing is GraphQL-only:
            1. orderEditBegin(id: gid://shopify/Order/<shopify_order_id>)
               -> calculatedOrder.id + userErrors
            2. orderEditAddVariant(calculatedOrderId, variantId:
               gid://shopify/ProductVariant/<vid>, quantity, price:
               {amount: <price_unit>, currencyCode: <shop currency>})
               -> userErrors
            3. orderEditCommit(calculatedOrderId, notifyCustomer: false,
               staffNote: "Line added via Odoo SO <so_name>") -> userErrors
          Any userErrors -> raise RuntimeError with the messages.
          Implementation note: the documented 2025-01 orderEditAddVariant
          schema has no `price` argument; we attempt it per contract and
          fall back to the documented shape (store catalog price) when the
          schema rejects the argument, logging a warning either way.
        - Shopify will fire orders/edited/updated back at us; the edit engine
          sees equal quantities afterwards and no-ops (idempotency covers the
          echo). The hook that enqueued this job must already be guarded by
          the context key shopify_sync_origin='shopify'.
        - Log steps (source='order_update_push', job=job,
          shopify_order_ref=so_name). Raise on failure; queue retries.
        """
        log = self.env["shopify.sync.log"]
        api = self.env["shopify.api.client"]
        payload = job.payload_dict()
        so_name = payload.get("so_name") or "?"

        def _log(level, message):
            log.log_event(
                level, message, source=SOURCE, job=job, shopify_order_ref=so_name
            )

        if not _is_truthy(api._param("order_update_push_enabled")):
            _log(
                "info",
                "Order update push skipped: order_update_push_enabled is off "
                "(Settings -> Shopify Ops).",
            )
            return

        shopify_order_id = str(payload.get("shopify_order_id") or "").strip()
        if not shopify_order_id:
            raise RuntimeError(
                "Order update push job %s: payload has no shopify_order_id."
                % job.name
            )

        so = self.env["sale.order"].sudo().browse(payload.get("so_id") or 0)
        product = (
            self.env["product.product"].sudo().browse(payload.get("product_id") or 0)
        )
        if not so.exists() or not product.exists():
            # The line/SO/product was deleted after enqueueing: nothing
            # meaningful left to push, so don't burn retries on it.
            _log(
                "warning",
                "Order update push skipped: SO %s or product %s no longer "
                "exists in Odoo."
                % (payload.get("so_id"), payload.get("product_id")),
            )
            return
        if is_charge_sku(product.default_code):
            _log(
                "info",
                "Order update push skipped: %s is a Shopify charge product."
                % (product.default_code,),
            )
            return

        # Quantity-increase pushes take the orderEditSetQuantity path.
        if payload.get("mode") == "qty":
            return self._process_qty_increase(
                job, api, payload, so, product, so_name, _log
            )

        qty = self._positive_int_qty(payload.get("qty"), so_name)
        variant_gid = self._resolve_variant_gid(api, product)
        order_gid = "gid://shopify/Order/%s" % shopify_order_id

        # 1. Begin the edit session.
        begin = api.graphql(_ORDER_EDIT_BEGIN, {"id": order_gid}).get(
            "orderEditBegin"
        ) or {}
        errors = begin.get("userErrors") or []
        if errors:
            # e.g. cancelled / fulfilled-locked / otherwise not editable —
            # raise so the failure is visible on the dashboard; ops handles
            # the order manually in Shopify.
            raise RuntimeError(
                "Shopify order %s (id %s) cannot be edited: %s"
                % (so_name, shopify_order_id, _format_user_errors(errors))
            )
        calc_gid = (begin.get("calculatedOrder") or {}).get("id")
        if not calc_gid:
            raise RuntimeError(
                "orderEditBegin for Shopify order %s (id %s) returned no "
                "calculatedOrder id." % (so_name, shopify_order_id)
            )
        _log(
            "info",
            "orderEditBegin ok for Shopify order %s (calculated order %s)."
            % (so_name, calc_gid),
        )

        # 2. Stage the line addition.
        outcome = self._add_variant(
            api,
            calc_gid,
            variant_gid,
            qty,
            payload.get("price_unit"),
            shopify_order_id,
            so_name,
            _log,
        )

        # 3. Commit (also on 'already_present': an interrupted earlier run may
        #    have staged the line without committing — committing a clean
        #    session finalizes it, and Shopify treats a no-change commit as a
        #    no-op).
        commit_vars = {
            "id": calc_gid,
            "notifyCustomer": False,
            "staffNote": "Line added via Odoo SO %s" % so_name,
        }
        commit = api.graphql(_ORDER_EDIT_COMMIT, commit_vars).get(
            "orderEditCommit"
        ) or {}
        errors = commit.get("userErrors") or []
        if errors:
            raise RuntimeError(
                "orderEditCommit failed for Shopify order %s (calculated "
                "order %s): %s" % (so_name, calc_gid, _format_user_errors(errors))
            )
        if outcome == "already_present":
            _log(
                "info",
                "Shopify order %s already had variant %s; edit committed, "
                "treated as idempotent success. Verify the quantity matches "
                "SO %s."
                % (so_name, variant_gid, so_name),
            )
        else:
            _log(
                "info",
                "Pushed line to Shopify order %s: variant %s x%s committed "
                "(staff note set, customer not notified)."
                % (so_name, variant_gid, qty),
            )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _process_qty_increase(self, job, api, payload, so, product, so_name, _log):
        """orderEditSetQuantity path: current Shopify qty + delta, commit."""
        delta = self._positive_int_qty(payload.get("delta"), so_name)
        variant_gid = self._resolve_variant_gid(api, product)
        order_gid = "gid://shopify/Order/%s" % payload["shopify_order_id"]

        begin = api.graphql(_ORDER_EDIT_BEGIN_WITH_LINES, {"id": order_gid}).get(
            "orderEditBegin"
        ) or {}
        errors = begin.get("userErrors") or []
        if errors:
            raise RuntimeError(
                "Shopify order %s (id %s) cannot be edited: %s"
                % (so_name, payload["shopify_order_id"], _format_user_errors(errors))
            )
        calc = begin.get("calculatedOrder") or {}
        calc_gid = calc.get("id")
        if not calc_gid:
            raise RuntimeError(
                "orderEditBegin for Shopify order %s returned no "
                "calculatedOrder id." % so_name
            )

        target = None
        current_qty = 0
        sku = (product.default_code or "").strip()
        for edge in (calc.get("lineItems") or {}).get("edges") or []:
            node = edge.get("node") or {}
            node_variant = (node.get("variant") or {}).get("id") or ""
            node_sku = (node.get("sku") or "").strip()
            if node_variant == variant_gid or (sku and node_sku == sku):
                target = node
                current_qty = int(node.get("quantity") or 0)
                break
        if not target:
            raise RuntimeError(
                "Variant %s (SKU %s) not found on Shopify order %s — cannot "
                "set quantity. If the line is new, add it on the SO instead."
                % (variant_gid, sku or "none", so_name)
            )

        new_qty = current_qty + delta
        result = api.graphql(
            _ORDER_EDIT_SET_QTY,
            {"id": calc_gid, "lineItemId": target["id"], "quantity": new_qty},
        ).get("orderEditSetQuantity") or {}
        errors = result.get("userErrors") or []
        if errors:
            raise RuntimeError(
                "orderEditSetQuantity failed for Shopify order %s: %s"
                % (so_name, _format_user_errors(errors))
            )

        commit = api.graphql(
            _ORDER_EDIT_COMMIT,
            {
                "id": calc_gid,
                "notifyCustomer": False,
                "staffNote": "Qty +%s via Odoo SO %s" % (delta, so_name),
            },
        ).get("orderEditCommit") or {}
        errors = commit.get("userErrors") or []
        if errors:
            raise RuntimeError(
                "orderEditCommit failed for Shopify order %s: %s"
                % (so_name, _format_user_errors(errors))
            )
        _log(
            "info",
            "Pushed qty increase to Shopify order %s: variant %s %s -> %s "
            "committed (customer not notified)."
            % (so_name, variant_gid, current_qty, new_qty),
        )

    def _add_variant(
        self, api, calc_gid, variant_gid, qty, price_unit, shopify_order_id,
        so_name, _log,
    ):
        """Run orderEditAddVariant; return 'added' or 'already_present'.

        Raises RuntimeError for any other userErrors. The contract asks for a
        price override (price: MoneyInput); the documented 2025-01 schema has
        no such argument, so on a schema rejection naming `price` we retry
        without it and the line takes the store catalog price.
        """
        variables = {"id": calc_gid, "variantId": variant_gid, "quantity": qty}
        price = self._money_input(api, price_unit, shopify_order_id)
        price_sent = False
        if price is not None:
            try:
                data = api.graphql(
                    _ORDER_EDIT_ADD_VARIANT, dict(variables, price=price)
                )
                price_sent = True
            except Exception as exc:  # noqa: BLE001 - filtered below
                if _PRICE_ARG_RE.search(str(exc)):
                    _log(
                        "warning",
                        "Shopify rejected the price argument on "
                        "orderEditAddVariant (%s); retrying without it — the "
                        "added line will use the store catalog price." % exc,
                    )
                    data = api.graphql(_ORDER_EDIT_ADD_VARIANT_NO_PRICE, variables)
                else:
                    raise
        else:
            data = api.graphql(_ORDER_EDIT_ADD_VARIANT_NO_PRICE, variables)

        add = (data or {}).get("orderEditAddVariant") or {}
        errors = add.get("userErrors") or []
        if errors:
            text = _format_user_errors(errors)
            if _ALREADY_PRESENT_RE.search(text):
                _log(
                    "warning",
                    "Variant %s seems to already be on Shopify order %s (%s); "
                    "attempting commit anyway before failing (idempotent "
                    "retry handling)." % (variant_gid, so_name, text),
                )
                return "already_present"
            raise RuntimeError(
                "orderEditAddVariant failed for Shopify order %s: %s"
                % (so_name, text)
            )
        _log(
            "info",
            "orderEditAddVariant staged on Shopify order %s: variant %s x%s%s."
            % (
                so_name,
                variant_gid,
                qty,
                " with price override %s" % price
                if price_sent
                else " at store catalog price (no price override sent)",
            ),
        )
        return "added"

    def _money_input(self, api, price_unit, shopify_order_id):
        """Build the MoneyInput for the price override, or None."""
        if price_unit is None:
            return None
        try:
            amount = "%.2f" % float(price_unit)
        except (TypeError, ValueError):
            return None
        currency = None
        try:
            currency = (api.get_order(shopify_order_id) or {}).get("currency")
        except Exception:  # noqa: BLE001 - fall back to company currency
            _logger.info(
                "order_update_push: currency lookup failed for Shopify order %s",
                shopify_order_id,
            )
        if not currency:
            currency = self.env.company.currency_id.name
        if not currency:
            return None
        return {"amount": amount, "currencyCode": currency}

    def _resolve_variant_gid(self, api, product):
        """Shopify variant GID for the product; RuntimeError naming the SKU."""
        vid = (product.shopify_variant_id or "").strip()
        if vid:
            return vid if vid.startswith("gid://") else (
                "gid://shopify/ProductVariant/%s" % vid
            )
        sku = (product.default_code or "").strip()
        if not sku:
            raise RuntimeError(
                "Product '%s' (id %s) is not linked to a Shopify variant and "
                "has no SKU (default_code); cannot push it to Shopify. Link "
                "the product or set a SKU."
                % (product.display_name, product.id)
            )
        data = api.graphql(_VARIANT_BY_SKU, {"skuQuery": "sku:%s" % sku})
        edges = (data.get("productVariants") or {}).get("edges") or []
        nodes = [edge.get("node") or {} for edge in edges]
        node = next(
            (
                n
                for n in nodes
                if (n.get("sku") or "").strip().lower() == sku.lower()
            ),
            None,
        ) or (nodes[0] if nodes else None)
        gid = (node or {}).get("id")
        if not gid:
            raise RuntimeError(
                "No Shopify variant found for SKU '%s' (product '%s', id %s); "
                "sync/link the product with Shopify before adding it to "
                "Shopify-linked orders."
                % (sku, product.display_name, product.id)
            )
        numeric = gid.rsplit("/", 1)[-1]
        if numeric:
            try:
                # Persist the link so later pushes skip the lookup.
                product.sudo().write({"shopify_variant_id": numeric})
            except Exception:  # noqa: BLE001 - linking is best-effort here
                _logger.warning(
                    "order_update_push: could not persist shopify_variant_id "
                    "on product %s",
                    product.id,
                )
        return gid

    @staticmethod
    def _positive_int_qty(qty, so_name):
        """Shopify order editing only accepts positive integer quantities."""
        try:
            value = float(qty)
        except (TypeError, ValueError):
            raise RuntimeError(
                "Order update push for SO %s: invalid quantity %r."
                % (so_name, qty)
            )
        rounded = int(round(value))
        if rounded < 1 or abs(value - rounded) > 1e-6:
            raise RuntimeError(
                "Order update push for SO %s: quantity %s is not a positive "
                "integer; Shopify order editing cannot add it — adjust the "
                "order in Shopify manually." % (so_name, value)
            )
        return rounded
