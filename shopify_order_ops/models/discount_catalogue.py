"""Shopify discount catalogue (codes + automatic sales) → Odoo.

One-way Shopify → Odoo. Records mirror Admin discount settings so staff
can see Afterpay-style sales (15% off order, dates, combinations) and
those automatic discounts can apply on manual sale orders.
"""

from datetime import datetime, timezone

from odoo import Command, api, fields, models
from odoo.exceptions import UserError

from .shopify_discount import (
    DISCOUNT_PRODUCT_XMLID,
    amounts_close,
    discounts_close,
    is_charge_line,
    is_charge_sku,
)

SOURCE = "discount_catalogue"
PARAM_CATALOGUE = "discount_catalogue_sync_enabled"
PARAM_MANUAL = "discount_apply_manual_orders"

_CODE_TYPENAMES = (
    "DiscountCodeBasic",
    "DiscountCodeBxgy",
    "DiscountCodeFreeShipping",
    "DiscountCodeApp",
)
_BXGY_TYPES = ("DiscountCodeBxgy", "DiscountAutomaticBxgy")
_SHIP_TYPES = ("DiscountCodeFreeShipping", "DiscountAutomaticFreeShipping")
_APP_TYPES = ("DiscountCodeApp", "DiscountAutomaticApp")

# Purchase-type flags live on DiscountCustomerGets, not DiscountCodeBasic.
_CUSTOMER_GETS = """
    customerGets {
      appliesOnOneTimePurchase
      appliesOnSubscription
      value {
        ... on DiscountPercentage { percentage }
        ... on DiscountAmount { amount { amount currencyCode } }
      }
      items { __typename }
    }
"""

DISCOUNT_FRAGMENT = """
fragment DiscountFields on DiscountCode {
  __typename
  ... on DiscountCodeBasic {
    title
    status
    startsAt
    endsAt
    asyncUsageCount
    usageLimit
    discountClass
    combinesWith { orderDiscounts productDiscounts shippingDiscounts }
    codes(first: 10) { nodes { code } }
    minimumRequirement {
      ... on DiscountMinimumQuantity { greaterThanOrEqualToQuantity }
      ... on DiscountMinimumSubtotal { greaterThanOrEqualToSubtotal { amount } }
    }
    %s
  }
  ... on DiscountCodeBxgy {
    title
    status
    startsAt
    endsAt
    asyncUsageCount
    combinesWith { orderDiscounts productDiscounts shippingDiscounts }
  }
  ... on DiscountCodeFreeShipping {
    title
    status
    startsAt
    endsAt
    asyncUsageCount
    combinesWith { orderDiscounts productDiscounts shippingDiscounts }
  }
  ... on DiscountCodeApp {
    title
    status
    startsAt
    endsAt
    asyncUsageCount
  }
}
""" % _CUSTOMER_GETS

# Automatic discounts do not implement DiscountCodeDiscount; query inline.
AUTOMATIC_INNER = """
  __typename
  ... on DiscountAutomaticBasic {
    title
    status
    startsAt
    endsAt
    asyncUsageCount
    combinesWith { orderDiscounts productDiscounts shippingDiscounts }
    minimumRequirement {
      ... on DiscountMinimumQuantity { greaterThanOrEqualToQuantity }
      ... on DiscountMinimumSubtotal { greaterThanOrEqualToSubtotal { amount } }
    }
    %s
  }""" % _CUSTOMER_GETS + """
  ... on DiscountAutomaticBxgy {
    title
    status
    startsAt
    endsAt
    asyncUsageCount
    combinesWith { orderDiscounts productDiscounts shippingDiscounts }
  }
  ... on DiscountAutomaticFreeShipping {
    title
    status
    startsAt
    endsAt
    asyncUsageCount
    combinesWith { orderDiscounts productDiscounts shippingDiscounts }
  }
  ... on DiscountAutomaticApp {
    title
    status
    startsAt
    endsAt
    asyncUsageCount
  }
"""

CODE_LIST_QUERY = (
    """
query CodeDiscounts($cursor: String) {
  codeDiscountNodes(first: 25, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        codeDiscount {
          ...DiscountFields
        }
      }
    }
  }
}
"""
    + DISCOUNT_FRAGMENT
)

AUTOMATIC_LIST_QUERY = """
query AutomaticDiscounts($cursor: String) {
  automaticDiscountNodes(first: 25, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        automaticDiscount {
          %s
        }
      }
    }
  }
}
""" % AUTOMATIC_INNER

NODE_QUERY = """
query DiscountNode($id: ID!) {
  node(id: $id) {
    __typename
    ... on DiscountCodeNode {
      id
      codeDiscount {
        %s
      }
    }
    ... on DiscountAutomaticNode {
      id
      automaticDiscount {
        %s
      }
    }
  }
}
""" % (
    """
        __typename
        ... on DiscountCodeBasic {
          title status startsAt endsAt asyncUsageCount usageLimit
          discountClass
          combinesWith { orderDiscounts productDiscounts shippingDiscounts }
          minimumRequirement {
            ... on DiscountMinimumQuantity { greaterThanOrEqualToQuantity }
            ... on DiscountMinimumSubtotal { greaterThanOrEqualToSubtotal { amount } }
          }
          customerGets {
            appliesOnOneTimePurchase
            appliesOnSubscription
            value {
              ... on DiscountPercentage { percentage }
              ... on DiscountAmount { amount { amount currencyCode } }
            }
            items { __typename }
          }
        }
        ... on DiscountCodeBxgy {
          title status startsAt endsAt asyncUsageCount
          combinesWith { orderDiscounts productDiscounts shippingDiscounts }
        }
        ... on DiscountCodeFreeShipping {
          title status startsAt endsAt asyncUsageCount
          combinesWith { orderDiscounts productDiscounts shippingDiscounts }
        }
        ... on DiscountCodeApp {
          title status startsAt endsAt asyncUsageCount
        }
    """,
    AUTOMATIC_INNER,
)

_CODES_CONNECTION = """
          codes(first: 100, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            nodes { code }
          }
"""

CODES_PAGE_QUERY = """
query DiscountRedeemCodes($id: ID!, $cursor: String) {
  node(id: $id) {
    ... on DiscountCodeNode {
      codeDiscount {
        ... on DiscountCodeBasic {
          %s
        }
        ... on DiscountCodeBxgy {
          %s
        }
        ... on DiscountCodeFreeShipping {
          %s
        }
        ... on DiscountCodeApp {
          %s
        }
      }
    }
  }
}
""" % (
    _CODES_CONNECTION,
    _CODES_CONNECTION,
    _CODES_CONNECTION,
    _CODES_CONNECTION,
)


def parse_shopify_datetime(value):
    text = (value or "").strip() if isinstance(value, str) else ""
    if not text:
        return False
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def normalize_percent(raw):
    """Shopify GraphQL may send 0.15 or 15 for 15%."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if 0 < value <= 1:
        return round(value * 100.0, 4)
    return round(value, 4)


def _codes_from_inner(inner):
    nodes = ((inner.get("codes") or {}).get("nodes")) or []
    codes = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        code = (node.get("code") or "").strip()
        if code and code not in codes:
            codes.append(code)
    return codes


def _min_requirement(inner):
    req = inner.get("minimumRequirement") or {}
    qty = req.get("greaterThanOrEqualToQuantity")
    money = req.get("greaterThanOrEqualToSubtotal") or {}
    amount = money.get("amount")
    if qty not in (None, "", False):
        try:
            return "quantity", 0.0, float(qty)
        except (TypeError, ValueError):
            return "quantity", 0.0, 0.0
    if amount not in (None, "", False):
        try:
            return "amount", float(amount), 0.0
        except (TypeError, ValueError):
            return "amount", 0.0, 0.0
    return "none", 0.0, 0.0


def _value_from_inner(inner):
    gets = inner.get("customerGets") or {}
    value = gets.get("value") or {}
    if value.get("percentage") not in (None, "", False):
        return "percentage", normalize_percent(value.get("percentage")), 0.0
    amount = (value.get("amount") or {}).get("amount")
    if amount not in (None, "", False):
        try:
            return "fixed_amount", 0.0, float(amount)
        except (TypeError, ValueError):
            return "fixed_amount", 0.0, 0.0
    return False, 0.0, 0.0


def _applies_to(inner):
    typename = inner.get("__typename") or ""
    if typename in _BXGY_TYPES:
        return "bxgy"
    if typename in _SHIP_TYPES:
        return "shipping"
    if typename in _APP_TYPES:
        return "app"
    dclass = (inner.get("discountClass") or "").upper()
    if dclass == "SHIPPING":
        return "shipping"
    if dclass == "ORDER":
        return "order"
    items = ((inner.get("customerGets") or {}).get("items")) or {}
    item_type = items.get("__typename") or ""
    if item_type in ("DiscountProducts", "DiscountCollections"):
        return "product"
    if dclass == "PRODUCT":
        return "product"
    return "order"


def _purchase_type(inner):
    gets = inner.get("customerGets") or {}
    one_time = gets.get("appliesOnOneTimePurchase")
    if one_time is None:
        one_time = inner.get("appliesOnOneTimePurchase")
    sub = gets.get("appliesOnSubscription")
    if sub is None:
        sub = inner.get("appliesOnSubscription")
    if one_time is False and sub:
        return "subscription"
    if sub and one_time is not False:
        return "both"
    return "one_time"


def parse_discount_node(node):
    """GraphQL DiscountCodeNode / DiscountAutomaticNode → vals dict or None."""
    if not isinstance(node, dict):
        return None
    gid = node.get("id") or ""
    inner = node.get("codeDiscount")
    method = "code"
    if inner is None:
        inner = node.get("automaticDiscount")
        method = "automatic"
    if not isinstance(inner, dict):
        inner = {}
    typename = inner.get("__typename") or ""
    if method == "code" and typename and typename not in _CODE_TYPENAMES:
        if "Automatic" in typename:
            method = "automatic"
    title = (inner.get("title") or "").strip()
    if not gid:
        return None
    codes = _codes_from_inner(inner)
    min_kind, min_amount, min_qty = _min_requirement(inner)
    value_type, percent, amount = _value_from_inner(inner)
    combines = inner.get("combinesWith") or {}
    selection = inner.get("customerSelection") or {}
    sel_type = (selection.get("__typename") or "").lower()
    customer_all = "all" in sel_type or not sel_type
    status = (inner.get("status") or "").upper() or "ACTIVE"
    vals = {
        "shopify_discount_id": gid,
        "title": title or gid.rsplit("/", 1)[-1],
        "method": method,
        "applies_to": _applies_to(inner),
        "value_type": value_type or False,
        "value_percent": percent,
        "value_amount": amount,
        "purchase_type": _purchase_type(inner),
        "customer_eligibility": "all" if customer_all else "specific",
        "min_requirement": min_kind,
        "min_amount": min_amount,
        "min_qty": min_qty,
        "combines_product": bool(combines.get("productDiscounts")),
        "combines_order": bool(combines.get("orderDiscounts")),
        "combines_shipping": bool(combines.get("shippingDiscounts")),
        "date_start": parse_shopify_datetime(inner.get("startsAt")),
        "date_end": parse_shopify_datetime(inner.get("endsAt")),
        "status": status.lower() if status else "active",
        "usage_count": int(inner.get("asyncUsageCount") or 0),
        "usage_limit": int(inner.get("usageLimit") or 0),
        "codes": ", ".join(codes),
        "active": status == "ACTIVE",
    }
    if vals["status"] not in (
        "active",
        "expired",
        "scheduled",
        "disabled",
    ):
        vals["status"] = "active" if vals["active"] else "disabled"
    return vals


def extract_discount_gid(payload):
    if not isinstance(payload, dict):
        return None
    gid = payload.get("admin_graphql_api_id") or payload.get("id")
    if isinstance(gid, str) and gid.startswith("gid://"):
        return gid
    if gid not in (None, "", False) and not isinstance(gid, dict):
        # REST numeric id — prefer GraphQL id when present.
        return str(gid)
    return None


class ShopifyDiscount(models.Model):
    """One Shopify automatic sale or discount-code program."""

    _name = "shopify.discount"
    _description = "Shopify Discount"
    _order = "method, title"
    _rec_name = "title"

    title = fields.Char(required=True)
    shopify_discount_id = fields.Char(index=True, copy=False)
    active = fields.Boolean(default=True)
    method = fields.Selection(
        [("automatic", "Automatic"), ("code", "Discount code")],
        required=True,
        default="automatic",
    )
    applies_to = fields.Selection(
        [
            ("order", "Amount off order"),
            ("product", "Amount off products"),
            ("bxgy", "Buy X get Y"),
            ("shipping", "Free shipping"),
            ("app", "App discount"),
        ],
        default="order",
    )
    value_type = fields.Selection(
        [
            ("percentage", "Percentage"),
            ("fixed_amount", "Fixed amount"),
        ],
    )
    value_percent = fields.Float(string="Percent off")
    value_amount = fields.Float(string="Amount off")
    purchase_type = fields.Selection(
        [
            ("one_time", "One-time purchase"),
            ("subscription", "Subscription"),
            ("both", "One-time and subscription"),
        ],
        default="one_time",
    )
    customer_eligibility = fields.Selection(
        [("all", "All customers"), ("specific", "Specific customers")],
        default="all",
    )
    min_requirement = fields.Selection(
        [
            ("none", "No minimum requirements"),
            ("amount", "Minimum purchase amount"),
            ("quantity", "Minimum quantity of items"),
        ],
        default="none",
        string="Minimum requirements",
    )
    min_amount = fields.Float(string="Minimum purchase amount")
    min_qty = fields.Float(string="Minimum quantity")
    combines_product = fields.Boolean(string="Combines with product discounts")
    combines_order = fields.Boolean(string="Combines with order discounts")
    combines_shipping = fields.Boolean(string="Combines with shipping discounts")
    date_start = fields.Datetime(string="Start")
    date_end = fields.Datetime(string="End")
    status = fields.Selection(
        [
            ("active", "Active"),
            ("scheduled", "Scheduled"),
            ("expired", "Expired"),
            ("disabled", "Disabled"),
        ],
        default="active",
    )
    usage_count = fields.Integer(string="Times used")
    usage_limit = fields.Integer(string="Usage limit")
    codes = fields.Text(
        string="Discount codes",
        help="Comma-separated codes customers type at checkout.",
    )
    summary = fields.Char(compute="_compute_summary", store=True)

    _sql_constraints = [
        (
            "shopify_discount_id_uniq",
            "unique(shopify_discount_id)",
            "This Shopify discount is already synced.",
        )
    ]

    @api.depends(
        "method",
        "applies_to",
        "value_type",
        "value_percent",
        "value_amount",
        "min_requirement",
        "date_start",
        "date_end",
        "status",
    )
    def _compute_summary(self):
        for rec in self:
            parts = []
            if rec.method == "automatic":
                parts.append("Automatic")
            else:
                parts.append("Code")
            if rec.value_type == "percentage" and rec.value_percent:
                parts.append("%s%% off" % rec.value_percent)
            elif rec.value_type == "fixed_amount" and rec.value_amount:
                parts.append("%s off" % rec.value_amount)
            if rec.applies_to == "order":
                parts.append("entire order")
            elif rec.applies_to == "product":
                parts.append("products")
            elif rec.applies_to == "shipping":
                parts.append("shipping")
            rec.summary = " · ".join(parts)

    def _codes_list(self):
        self.ensure_one()
        return [
            part.strip()
            for part in (self.codes or "").split(",")
            if part.strip()
        ]

    def is_in_date_window(self, when=None):
        self.ensure_one()
        when = when or fields.Datetime.now()
        if self.date_start and when < self.date_start:
            return False
        if self.date_end and when >= self.date_end:
            return False
        return True

    def matches_code(self, raw):
        want = (raw or "").strip().casefold()
        if not want:
            return False
        return any(code.casefold() == want for code in self._codes_list())

    def eligible_for_order(self, order, coupon_code=None):
        """True when this catalogue discount can apply on a manual SO."""
        self.ensure_one()
        if not self.active or self.status != "active":
            return False
        if self.applies_to in ("bxgy", "shipping", "app"):
            return False
        if not self.is_in_date_window():
            return False
        if self.method == "code":
            if not self.matches_code(coupon_code):
                return False
        elif self.method != "automatic":
            return False
        elif self.customer_eligibility == "specific":
            return False
        if self.min_requirement == "amount":
            if (order.amount_untaxed or 0.0) + 0.001 < (self.min_amount or 0.0):
                return False
        if self.min_requirement == "quantity":
            qty = sum(
                line.product_uom_qty
                for line in _manual_product_lines(order)
            )
            if qty + 0.001 < (self.min_qty or 0.0):
                return False
        return True

    def apply_to_manual_order(self, order):
        """Apply the best matching catalogue discount on a manual sale order."""
        if not order or order.shopify_order_id or order.shopify_order_name:
            return
        if order.state in ("cancel", "done"):
            return
        Coupon = self.browse()
        coupon = (order.shopify_coupon_code or "").strip()
        if coupon:
            Coupon = self.search(
                [
                    ("method", "=", "code"),
                    ("active", "=", True),
                    ("status", "=", "active"),
                ]
            ).filtered(lambda d: d.eligible_for_order(order, coupon))
        Automatic = self.search(
            [
                ("method", "=", "automatic"),
                ("active", "=", True),
                ("status", "=", "active"),
            ]
        ).filtered(lambda d: d.eligible_for_order(order))
        chosen = self._pick_one(Coupon or Automatic)
        ctx = dict(self.env.context, shopify_applying_catalogue_discount=True)
        if not chosen:
            self._clear_catalogue_application(order.with_context(ctx))
            return
        chosen._apply_on_order(order.with_context(ctx))

    def _pick_one(self, discounts):
        if not discounts:
            return self.browse()
        exclusive = discounts.filtered(
            lambda d: not (d.combines_order or d.combines_product)
        )
        pool = exclusive or discounts
        best = pool[:1]
        best_score = (-1.0, -1.0, 0)
        for disc in pool:
            score = (
                disc.value_percent if disc.value_type == "percentage" else 0.0,
                disc.value_amount or 0.0,
                disc.id,
            )
            if score > best_score:
                best_score = score
                best = disc
        return best

    def _apply_on_order(self, order):
        self.ensure_one()
        lines = _manual_product_lines(order)
        label = self.title
        if self.method == "code" and self._codes_list():
            label = self._codes_list()[0]
        if (order.shopify_discount_codes or "").strip() != (label or ""):
            order.shopify_discount_codes = label or False
        if self.value_type == "percentage" and self.value_percent:
            want = round(self.value_percent, 2)
            for line in lines:
                if discounts_close(line.discount, want):
                    continue
                if line.discount and not discounts_close(line.discount, want):
                    # Staff typed a different Disc.% — leave it.
                    continue
                line.discount = want
            # Strip catalogue % from delivery / shipping fee lines if a
            # previous version applied it there by mistake.
            for line in order.order_line - lines:
                if line.display_type or not line.product_id:
                    continue
                if _line_looks_like_delivery(line) and discounts_close(
                    line.discount, want
                ):
                    line.discount = 0.0
            self._remove_fixed_discount_line(order)
            return
        if self.value_type == "fixed_amount" and self.value_amount:
            self._upsert_fixed_discount_line(order, -abs(self.value_amount))
            return
        self._remove_fixed_discount_line(order)

    def _clear_catalogue_application(self, order):
        """Drop auto Disc.% / charge line we applied, not staff-typed discounts.

        Only clears when shopify_discount_codes still names a catalogue
        title/code (so a walk-in 10% typed by staff is kept).
        """
        current = (order.shopify_discount_codes or "").strip()
        if not current:
            return
        known = self.search([]).filtered(
            lambda d: (d.title or "").strip() == current
            or d.matches_code(current)
        )
        if not known:
            return
        percent = known[:1].value_percent if known[:1].value_type == "percentage" else 0.0
        for line in _manual_product_lines(order):
            if percent and discounts_close(line.discount, percent):
                line.discount = 0.0
        self._remove_fixed_discount_line(order)
        order.shopify_discount_codes = False

    def _discount_product(self):
        tmpl = self.env.ref(DISCOUNT_PRODUCT_XMLID, raise_if_not_found=False)
        return tmpl.product_variant_id if tmpl else self.env["product.product"]

    def _upsert_fixed_discount_line(self, order, amount):
        name = "Discount (%s)" % (self.title or "Shopify")
        product = self._discount_product()
        lines = order.order_line.filtered("shopify_discount_line")
        vals = {
            "name": name,
            "product_uom_qty": 1.0,
            "price_unit": amount,
            "discount": 0.0,
            "shopify_discount_line": True,
        }
        if product:
            vals["product_id"] = product.id
        if lines:
            keeper = lines.sorted("id")[-1]
            extra = lines - keeper
            update = {}
            if keeper.name != name:
                update["name"] = name
            if not amounts_close(keeper.price_unit, amount):
                update["price_unit"] = amount
            for key, value in update.items():
                keeper[key] = value
            if extra:
                extra.unlink()
            return
        if abs(amount) <= 0.02:
            return
        if isinstance(order.id, int):
            order.write({"order_line": [Command.create(vals)]})
        else:
            order.order_line += self.env["sale.order.line"].new(vals)

    def _remove_fixed_discount_line(self, order):
        extra = order.order_line.filtered("shopify_discount_line")
        if extra:
            extra.unlink()


def _line_looks_like_delivery(line):
    """True for shipping / delivery-fee lines that must not get shopfront Disc.%."""
    if not line:
        return False
    if getattr(line, "shopify_shipping_line", False):
        return True
    if getattr(line, "is_delivery", False):
        return True
    product = line.product_id
    sku = (product.default_code or "").strip().casefold() if product else ""
    if sku in ("delivery", "shipping", "freight", "postage", "courier"):
        return True
    if is_charge_sku(sku):
        return True
    blobs = [
        (line.name or ""),
        (product.name if product else "") or "",
        (product.display_name if product else "") or "",
        sku,
    ]
    text = " ".join(blobs).casefold()
    for token in (
        "delivery fee",
        "delivery charge",
        "shipping fee",
        "shipping charge",
        "freight",
        "postage",
        "courier",
        "ewe ",
        "[delivery]",
    ):
        if token in text:
            return True
    # Bare "delivery" / "shipping" as the whole line or SKU label.
    compact = "".join(ch for ch in text if ch.isalnum())
    if compact in ("delivery", "shipping", "deliveryfee", "shippingfee"):
        return True
    if text.strip() in ("delivery", "shipping") or text.startswith("shipping"):
        return True
    return False


def _is_shopfront_product(product):
    """True when the product is linked to the Shopify shopfront catalogue."""
    if not product:
        return False
    if getattr(product, "shopify_variant_id", False):
        return True
    if getattr(product, "shopify_product_id", False):
        return True
    return False


def _manual_product_lines(order):
    """Merchandise lines eligible for Shopify catalogue Disc.% on manual SOs.

    Shopfront (Shopify-linked) products only. Delivery fees, shipping lines,
    and dedicated discount/shipping charge products never get the sale %.
    """

    def keep(line):
        if line.display_type or not line.product_id:
            return False
        if is_charge_line(line) or _line_looks_like_delivery(line):
            return False
        if not _is_shopfront_product(line.product_id):
            return False
        return True

    return order.order_line.filtered(keep)


class ShopifyDiscountCatalogueSync(models.AbstractModel):
    _name = "shopify.discount.catalogue.sync"
    _description = "Syncs Shopify discount programmes into Odoo"

    def _enabled(self):
        raw = self.env["shopify.api.client"]._param(PARAM_CATALOGUE)
        if raw is None or str(raw).strip() == "":
            return True
        return str(raw).strip().lower() in ("1", "true", "yes", "on")

    def _manual_apply_enabled(self):
        raw = self.env["shopify.api.client"]._param(PARAM_MANUAL)
        if raw is None or str(raw).strip() == "":
            return True
        return str(raw).strip().lower() in ("1", "true", "yes", "on")

    def cron_sync_discounts(self, limit=1000):
        """Pull automatic + code discounts from Shopify (Shopify → Odoo)."""
        log = self.env["shopify.sync.log"]
        if not self._enabled():
            log.log_event(
                "info",
                "Discount catalogue sync skipped: disabled.",
                source=SOURCE,
            )
            return
        api = self.env["shopify.api.client"]
        try:
            limit = max(1, int(limit or 1000))
        except (TypeError, ValueError):
            limit = 1000
        created = updated = failed = 0
        nodes = self._fetch_all_nodes(api, limit)
        Discount = self.env["shopify.discount"].sudo()
        for node in nodes:
            try:
                vals = parse_discount_node(node)
                if not vals or not vals.get("shopify_discount_id"):
                    continue
                existing = Discount.search(
                    [("shopify_discount_id", "=", vals["shopify_discount_id"])],
                    limit=1,
                )
                if existing:
                    existing.write(vals)
                    updated += 1
                else:
                    Discount.create(vals)
                    created += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                log.log_event(
                    "error",
                    "Discount catalogue upsert failed for %s: %s"
                    % ((node or {}).get("id"), exc),
                    source=SOURCE,
                )
        log.log_event(
            "info",
            "Discount catalogue Shopify→Odoo finished: %d created, %d "
            "updated, %d failed."
            % (created, updated, failed),
            source=SOURCE,
        )

    def _fetch_all_nodes(self, api, limit):
        log = self.env["shopify.sync.log"]
        auto_nodes = self._paginate_safe(
            api,
            AUTOMATIC_LIST_QUERY,
            "automaticDiscountNodes",
            min(limit, 200),
            "automatic discounts",
        )
        remaining = max(0, limit - len(auto_nodes))
        code_nodes = []
        if remaining:
            code_nodes = self._paginate_safe(
                api, CODE_LIST_QUERY, "codeDiscountNodes", remaining, "code discounts"
            )
        log.log_event(
            "info",
            "Discount catalogue fetched %d automatic + %d code node(s)."
            % (len(auto_nodes), len(code_nodes)),
            source=SOURCE,
        )
        return (auto_nodes + code_nodes)[:limit]

    def _paginate_safe(self, api, query, root, limit, label):
        log = self.env["shopify.sync.log"]
        try:
            return self._paginate(api, query, root, limit)
        except UserError as exc:
            message = str(exc)
            if "403" in message or "ACCESS" in message.upper():
                log.log_event(
                    "error",
                    "Discount catalogue %s need the read_discounts "
                    "Admin API scope: %s" % (label, message),
                    source=SOURCE,
                )
                return []
            log.log_event(
                "error",
                "Discount catalogue could not list %s: %s" % (label, message),
                source=SOURCE,
            )
            return []

    def _paginate(self, api, query, root, limit):
        nodes = []
        cursor = None
        last_cursor = object()
        pages = 0
        while len(nodes) < limit and pages < 40:
            pages += 1
            data = api.graphql(
                query, {"cursor": cursor}, allow_partial=True
            )
            conn = data.get(root) or {}
            batch = 0
            for edge in conn.get("edges") or []:
                node = (edge or {}).get("node")
                if node:
                    nodes.append(node)
                    batch += 1
                if len(nodes) >= limit:
                    break
            info = conn.get("pageInfo") or {}
            if not info.get("hasNextPage") or not batch:
                break
            cursor = info.get("endCursor")
            if not cursor or cursor == last_cursor:
                break
            last_cursor = cursor
        return nodes

    def _hydrate_codes(self, api, node):
        inner = node.get("codeDiscount") if isinstance(node, dict) else None
        gid = (node or {}).get("id")
        if not isinstance(inner, dict) or not gid:
            return
        try:
            codes = self._fetch_code_strings(api, gid)
        except UserError as exc:
            self.env["shopify.sync.log"].log_event(
                "warning",
                "Could not load redeem codes for %s: %s" % (gid, exc),
                source=SOURCE,
            )
            return
        if codes:
            inner["codes"] = {"nodes": [{"code": code} for code in codes]}

    def _fetch_code_strings(self, api, gid):
        codes = []
        cursor = None
        seen = set()
        for _ in range(2):
            data = api.graphql(
                CODES_PAGE_QUERY,
                {"id": gid, "cursor": cursor},
                allow_partial=True,
            )
            inner = ((data.get("node") or {}).get("codeDiscount")) or {}
            conn = inner.get("codes") or {}
            for row in conn.get("nodes") or []:
                code = ((row or {}).get("code") or "").strip()
                if code and code not in seen:
                    seen.add(code)
                    codes.append(code)
            info = conn.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                break
            cursor = info.get("endCursor")
            if not cursor:
                break
        return codes

    def process_discount_webhook(self, job):
        log = self.env["shopify.sync.log"]
        if not self._enabled():
            log.log_event(
                "info",
                "Discount webhook ignored: catalogue sync is off.",
                source=SOURCE,
                job=job,
            )
            return
        payload = job.payload_dict()
        topic = payload.get("topic") or ""
        raw = payload.get("raw") or {}
        gid = extract_discount_gid(raw) or payload.get("discount_id")
        Discount = self.env["shopify.discount"].sudo()
        if "delete" in topic:
            if gid:
                rec = Discount.search(
                    [("shopify_discount_id", "=", str(gid))], limit=1
                )
                if rec:
                    rec.write({"active": False, "status": "disabled"})
            log.log_event(
                "info",
                "Discount webhook (%s): archived %s." % (topic, gid),
                source=SOURCE,
                job=job,
            )
            return
        if not gid or not str(gid).startswith("gid://"):
            log.log_event(
                "warning",
                "Discount webhook carried no GraphQL id; run catalogue sync.",
                source=SOURCE,
                job=job,
            )
            return
        api = self.env["shopify.api.client"]
        try:
            data = api.graphql(NODE_QUERY, {"id": gid})
        except UserError as exc:
            log.log_event(
                "error",
                "Discount webhook fetch failed for %s: %s" % (gid, exc),
                source=SOURCE,
                job=job,
            )
            return
        node = data.get("node") or {}
        if not node.get("id"):
            rec = Discount.search([("shopify_discount_id", "=", gid)], limit=1)
            if rec:
                rec.write({"active": False, "status": "disabled"})
            return
        self._hydrate_codes(api, node)
        vals = parse_discount_node(node)
        if not vals:
            return
        existing = Discount.search(
            [("shopify_discount_id", "=", vals["shopify_discount_id"])], limit=1
        )
        if existing:
            existing.write(vals)
        else:
            Discount.create(vals)
        log.log_event(
            "info",
            "Discount webhook (%s) upserted '%s'."
            % (topic, vals.get("title")),
            source=SOURCE,
            job=job,
        )
