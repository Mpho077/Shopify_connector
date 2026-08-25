"""Shopify order discounts and shipping → dedicated Odoo sale lines.

Merchandise discounts (REST ``total_discount`` / allocations, or the
order total minus shipping discounts) become one negative sale line so
a cart-wide code applies to the whole order, not one SKU. Net shipping
the customer paid becomes one positive service line.
"""

PARAM_KEY = "discount_sync_enabled"
PARAM_FULL = "shopify_order_ops.discount_sync_enabled"
DIRECTION_KEY = "discount_sync_direction"
# One direction at a time. Stored "two_way" from 19.0.1.0.7 is ignored.
DIRECTIONS = ("shopify_to_odoo", "odoo_to_shopify")
# Service product used on the dedicated Odoo discount sale line. Never pushed
# to Shopify; Odoo requires a product on confirmed-order / invoice lines.
DISCOUNT_PRODUCT_SKU = "SHOPIFY-DISCOUNT"
DISCOUNT_PRODUCT_XMLID = "shopify_order_ops.product_template_shopify_discount"
SHIPPING_PRODUCT_SKU = "SHOPIFY-SHIPPING"
SHIPPING_PRODUCT_XMLID = "shopify_order_ops.product_template_shopify_shipping"
CHARGE_PRODUCT_SKUS = (DISCOUNT_PRODUCT_SKU, SHIPPING_PRODUCT_SKU)


def is_charge_sku(sku):
    return (sku or "").strip() in CHARGE_PRODUCT_SKUS


def is_charge_line(line):
    """True for dedicated Shopify discount/shipping sale or invoice lines."""
    if not line:
        return False
    if getattr(line, "shopify_discount_line", False):
        return True
    if getattr(line, "shopify_shipping_line", False):
        return True
    product = getattr(line, "product_id", False)
    sku = product.default_code if product else None
    if is_charge_sku(sku):
        return True
    sale_lines = getattr(line, "sale_line_ids", False)
    if not sale_lines:
        return False
    return any(
        getattr(sl, "shopify_discount_line", False)
        or getattr(sl, "shopify_shipping_line", False)
        or is_charge_sku(sl.product_id.default_code if sl.product_id else None)
        for sl in sale_lines
    )


# Compare stored vs computed percentages; Odoo Discount precision is 2 d.p.
PERCENT_TOLERANCE = 0.01
# Compare money amounts (shop currency).
AMOUNT_TOLERANCE = 0.02


def as_float(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def line_discount_amount(item):
    """Shop-currency discount amount on one Shopify line_item.

    Prefers ``total_discount`` (REST). Falls back to summing
    ``discount_allocations`` (and ``amount_set.shop_money.amount`` when
    ``amount`` is missing).
    """
    if not isinstance(item, dict):
        return 0.0
    raw = item.get("total_discount")
    if raw not in (None, ""):
        amount = as_float(raw)
        if amount > 0:
            return amount
    total = 0.0
    for alloc in item.get("discount_allocations") or []:
        if not isinstance(alloc, dict):
            continue
        amount = as_float(alloc.get("amount"))
        if amount <= 0:
            shop = (alloc.get("amount_set") or {}).get("shop_money") or {}
            amount = as_float(shop.get("amount"))
        total += max(0.0, amount)
    return total


def shipping_discount_amount(ship):
    """Discount on one Shopify shipping_line."""
    if not isinstance(ship, dict):
        return 0.0
    amount = line_discount_amount(ship)
    if amount > 0:
        return amount
    discounted = ship.get("discounted_price")
    if discounted in (None, ""):
        return 0.0
    return max(0.0, as_float(ship.get("price")) - as_float(discounted))


def percent_from_gross(gross, discount_amount, ndigits=4):
    """Odoo ``discount`` (0-100) from a gross line amount and a discount."""
    if gross <= 0 or discount_amount <= 0:
        return 0.0
    if discount_amount >= gross:
        return 100.0
    return round(100.0 * discount_amount / gross, ndigits)


def line_item_quantity(item):
    """Remaining Shopify line qty after refunds and order edits.

    ``quantity`` is what was purchased. After a refund or a removed line,
    ``current_quantity`` is what is still on the order. Fall back to
    ``quantity`` when ``current_quantity`` is absent (older payloads).
    """
    if not isinstance(item, dict):
        return 0.0
    if item.get("current_quantity") not in (None, ""):
        return max(0.0, as_float(item.get("current_quantity")))
    return max(0.0, as_float(item.get("quantity")))


def line_discount_percent(item, ndigits=4):
    """Odoo sale-line discount % for one Shopify line_item."""
    if not isinstance(item, dict):
        return 0.0
    qty = as_float(item.get("quantity"))
    price = as_float(item.get("price"))
    return percent_from_gross(price * qty, line_discount_amount(item), ndigits)


def discounts_close(left, right):
    return abs(as_float(left) - as_float(right)) < PERCENT_TOLERANCE


def amounts_close(left, right):
    return abs(as_float(left) - as_float(right)) < AMOUNT_TOLERANCE


def _shopify_line_name(item):
    title = (item.get("title") or "").strip()
    variant = (item.get("variant_title") or "").strip()
    if variant and variant.casefold() != "default title":
        parts = [part for part in (title, variant) if part]
        if len(parts) > 1:
            return "%s / %s" % (parts[0], parts[1])
        return parts[0] if parts else ""
    return title


def aggregate_by_sku(line_items):
    """Map SKU → {qty, price, discount, name, disc_amount, gross}.

    Duplicate SKUs on one Shopify order are combined; the discount % is
    the quantity-weighted share of allocated discount over gross.
    """
    by_sku = {}
    for item in line_items or []:
        if not isinstance(item, dict):
            continue
        sku = (item.get("sku") or "").strip()
        if not sku:
            continue
        qty = line_item_quantity(item)
        price = as_float(item.get("price"))
        amount = line_discount_amount(item)
        if sku in by_sku:
            data = by_sku[sku]
            data["qty"] += qty
            data["gross"] += price * qty
            data["disc_amount"] += amount
            if price:
                data["price"] = price
            if item.get("title"):
                data["title"] = item.get("title")
                data["name"] = item.get("title")
                data["line_name"] = _shopify_line_name(item)
        else:
            by_sku[sku] = {
                "qty": qty,
                "price": price,
                "title": item.get("title") or sku,
                "name": item.get("title") or sku,
                "line_name": _shopify_line_name(item),
                "gross": price * qty,
                "disc_amount": amount,
            }
    for data in by_sku.values():
        data["discount"] = percent_from_gross(data["gross"], data["disc_amount"])
    return by_sku


def unallocated_discount_amount(order):
    """Order-level discount not explained by line or shipping allocations.

    Returns 0 when the leftover is within 2 cents (rounding).
    """
    if not isinstance(order, dict):
        return 0.0
    order_total = as_float(order.get("current_total_discounts"))
    if order_total <= 0:
        order_total = as_float(order.get("total_discounts"))
    allocated = 0.0
    for item in order.get("line_items") or []:
        allocated += line_discount_amount(item)
    for ship in order.get("shipping_lines") or []:
        allocated += shipping_discount_amount(ship)
    leftover = round(order_total - allocated, 2)
    return leftover if leftover > 0.02 else 0.0


def shipping_discount_total(order):
    total = 0.0
    for ship in (order or {}).get("shipping_lines") or []:
        total += shipping_discount_amount(ship)
    return total


def shipping_line_net_amount(ship):
    """Amount the customer paid for one Shopify shipping_line."""
    if not isinstance(ship, dict):
        return 0.0
    discounted = ship.get("discounted_price")
    if discounted not in (None, ""):
        return max(0.0, as_float(discounted))
    return max(0.0, as_float(ship.get("price")) - shipping_discount_amount(ship))


def order_shipping_amount(order):
    """Net shipping the customer paid (after shipping-line discounts)."""
    if not isinstance(order, dict):
        return 0.0
    total = 0.0
    for ship in order.get("shipping_lines") or []:
        total += shipping_line_net_amount(ship)
    return round(total, 2)


def shipping_line_name(order):
    titles = []
    for ship in (order or {}).get("shipping_lines") or []:
        if not isinstance(ship, dict):
            continue
        title = (ship.get("title") or "").strip()
        code = (ship.get("code") or "").strip()
        if code and (not title or code == title or code in title):
            label = code
        else:
            label = title or code
        if label and label not in titles:
            titles.append(label)
    if len(titles) == 1:
        return "Shipping (%s)" % titles[0]
    if titles:
        return "Shipping (%s)" % ", ".join(titles)
    return "Shipping"


def merchandise_discount_amount(order):
    """Shopify merchandise discount for the Odoo discount sale line.

    Prefer ``current_total_discounts`` / ``total_discounts`` minus shipping
    discounts. Fall back to summed line allocations when the order total is
    missing.
    """
    if not isinstance(order, dict):
        return 0.0
    order_total = as_float(order.get("current_total_discounts"))
    if order_total <= 0:
        order_total = as_float(order.get("total_discounts"))
    amount = round(order_total - shipping_discount_total(order), 2)
    if amount > 0:
        return amount
    lines = 0.0
    for item in order.get("line_items") or []:
        lines += line_discount_amount(item)
    return max(0.0, round(lines, 2))


def discount_line_name(order):
    labels = discount_application_labels(order)
    if labels:
        return "Discount (%s)" % ", ".join(labels)
    return "Discount"


def discount_application_labels(order):
    """Human-readable codes/titles from discount_applications."""
    labels = []
    for app in (order or {}).get("discount_applications") or []:
        if not isinstance(app, dict):
            continue
        label = (
            app.get("code")
            or app.get("title")
            or app.get("description")
            or ""
        ).strip()
        if label and label not in labels:
            labels.append(label)
    return labels


def applied_discount_codes(order):
    """Coupon codes / automatic titles applied on a Shopify order."""
    codes = []
    for row in (order or {}).get("discount_codes") or []:
        if isinstance(row, dict):
            code = (row.get("code") or "").strip()
        else:
            code = str(row or "").strip()
        if code and code not in codes:
            codes.append(code)
    for label in discount_application_labels(order):
        if label and label not in codes:
            codes.append(label)
    return codes


def applied_discount_codes_csv(order):
    return ", ".join(applied_discount_codes(order))


def _application_value_type(app):
    return (
        (app or {}).get("value_type") or (app or {}).get("valueType") or ""
    ).strip().lower()


def _application_target_type(app):
    return (
        (app or {}).get("target_type") or (app or {}).get("targetType") or "line_item"
    ).strip().lower()


def merchandise_discount_applications(order):
    """discount_applications that hit merchandise, not shipping."""
    apps = []
    for app in (order or {}).get("discount_applications") or []:
        if not isinstance(app, dict):
            continue
        target = _application_target_type(app)
        if target in ("shipping_line", "shipping"):
            continue
        apps.append(app)
    return apps


def uses_line_percent_discount(order):
    """True when merchandise discounts should land as Odoo Disc.%.

    Shopify percentage coupons (``value_type=percentage``) belong on the
    product line, e.g. 15% off $499 → Disc.% 15, not a SHOPIFY-DISCOUNT
    line. Cart-wide fixed-amount codes stay on the dedicated line.
    Missing ``value_type`` keeps the dedicated-line model (legacy tests /
    manual $ off).
    """
    if "line_items" not in (order or {}):
        return False
    apps = merchandise_discount_applications(order)
    if not apps:
        return False
    types = {_application_value_type(app) for app in apps}
    if "fixed_amount" in types:
        return False
    return bool(types & {"percentage", "percent"})


def discount_sync_enabled(param_value):
    """Default-on: unset means enabled; only explicit false/0 disables."""
    if param_value is None:
        return True
    return str(param_value).strip().lower() not in ("false", "0")


def discount_sync_direction(param_value):
    """Return configured direction; default Shopify → Odoo when unset."""
    raw = (param_value or "").strip()
    if raw in DIRECTIONS:
        return raw
    return "shopify_to_odoo"


def discount_sync_allows_shopify_to_odoo(enabled_param, direction_param):
    if not discount_sync_enabled(enabled_param):
        return False
    return discount_sync_direction(direction_param) == "shopify_to_odoo"


def discount_sync_allows_odoo_to_shopify(enabled_param, direction_param):
    if not discount_sync_enabled(enabled_param):
        return False
    return discount_sync_direction(direction_param) == "odoo_to_shopify"


def clamp_percent(value):
    return max(0.0, min(100.0, as_float(value)))
