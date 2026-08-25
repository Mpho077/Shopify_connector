"""Helpers for Shopify refund → Odoo credit-note quantities."""

from .shopify_discount import as_float, is_charge_sku, line_discount_amount


def norm_sku(sku):
    return (sku or "").strip().lower()


def refund_line_item_id(entry):
    """Shopify order line id on a refund_line_items row."""
    if not isinstance(entry, dict):
        return ""
    line_item = entry.get("line_item")
    lid = entry.get("line_item_id")
    if isinstance(line_item, dict):
        lid = lid or line_item.get("id")
    elif line_item not in (None, False, ""):
        lid = lid or line_item
    if lid in (None, False, ""):
        return ""
    return str(lid).strip()


def _order_lines_by_id(order):
    by_id = {}
    for item in (order or {}).get("line_items") or []:
        if not isinstance(item, dict) or item.get("id") in (None, False, ""):
            continue
        by_id[str(item.get("id"))] = item
    return by_id


def refund_line_sku(entry, order=None):
    """SKU on one refund_line_items row (nested line_item may omit sku)."""
    if not isinstance(entry, dict):
        return ""
    line_item = entry.get("line_item") or {}
    if not isinstance(line_item, dict):
        line_item = {}
    variant = line_item.get("variant") or {}
    if not isinstance(variant, dict):
        variant = {}
    sku = (
        line_item.get("sku")
        or entry.get("sku")
        or variant.get("sku")
        or ""
    )
    sku = str(sku).strip()
    if sku:
        return sku
    parent = _order_lines_by_id(order).get(refund_line_item_id(entry)) or {}
    return str(parent.get("sku") or "").strip()


def refund_unit_price(entry, order=None):
    """Unit price for one refund_line_items row (shop currency)."""
    if not isinstance(entry, dict):
        return 0.0
    qty = as_float(entry.get("quantity")) or 1.0
    if entry.get("subtotal") not in (None, ""):
        return as_float(entry.get("subtotal")) / qty
    line_item = entry.get("line_item") if isinstance(entry.get("line_item"), dict) else {}
    parent = _order_lines_by_id(order).get(refund_line_item_id(entry)) or {}
    for src in (line_item, parent, entry):
        if src.get("price") not in (None, ""):
            return as_float(src.get("price"))
    return 0.0


def refund_prices_by_sku(refund, order=None):
    """{sku: [unit prices]} for refunded product lines."""
    parent_by_id = _order_lines_by_id(order)
    prices = {}
    for entry in (refund or {}).get("refund_line_items") or []:
        sku = refund_line_sku(entry, order)
        if not sku:
            parent = parent_by_id.get(refund_line_item_id(entry)) or {}
            sku = str(parent.get("sku") or "").strip()
        if not sku or is_charge_sku(sku):
            continue
        price = refund_unit_price(entry, order)
        if price:
            prices.setdefault(sku, []).append(price)
    return prices


def refund_qty_by_sku(refund, order=None):
    """{sku: refunded qty} from refund_line_items. Skips charge SKUs.

    When the refund row has no SKU (order-edit removals often omit it), look
    up the order line via line_item_id.
    """
    parent_by_id = _order_lines_by_id(order)
    qty_by_sku = {}
    for entry in (refund or {}).get("refund_line_items") or []:
        sku = refund_line_sku(entry, order)
        if not sku:
            parent = parent_by_id.get(refund_line_item_id(entry)) or {}
            sku = str(parent.get("sku") or "").strip()
        if not sku or is_charge_sku(sku):
            continue
        qty = as_float(entry.get("quantity"))
        if qty > 0:
            qty_by_sku[sku] = qty_by_sku.get(sku, 0.0) + qty
    return qty_by_sku


def refund_entry_discount(entry, order=None):
    """Merchandise discount allocated to one refunded qty."""
    if not isinstance(entry, dict):
        return 0.0
    sku = refund_line_sku(entry, order)
    if sku and is_charge_sku(sku):
        return 0.0
    direct = line_discount_amount(entry)
    if direct > 0.0001:
        return direct
    line_item = entry.get("line_item") if isinstance(entry.get("line_item"), dict) else {}
    parent = _order_lines_by_id(order).get(refund_line_item_id(entry)) or {}
    item = line_item or parent
    full = line_discount_amount(item)
    if full <= 0.0001:
        return 0.0
    orig_qty = as_float(item.get("quantity")) or 1.0
    refund_qty = as_float(entry.get("quantity"))
    if orig_qty <= 0 or refund_qty <= 0:
        return 0.0
    return full * min(refund_qty, orig_qty) / orig_qty


def refunded_discount_amount(refund, order=None):
    """Sum of Shopify discounts allocated to refunded product lines."""
    total = 0.0
    for entry in (refund or {}).get("refund_line_items") or []:
        total += refund_entry_discount(entry, order)
    return total


def discount_share_for_credit(
    refund,
    order,
    credited_gross,
    invoice_product_gross,
    invoice_discount_abs,
):
    """Discount to put on the credit note so the customer is not over-refunded.

    Prefers Shopify's per-line allocations. If those are missing (cart-wide
    codes stored only as the Odoo SHOPIFY-DISCOUNT line), uses the credited
    products' share of that invoice discount line.
    """
    invoice_discount_abs = abs(as_float(invoice_discount_abs))
    shopify = refunded_discount_amount(refund, order)
    if shopify > 0.0001:
        if invoice_discount_abs > 0.0001:
            return min(shopify, invoice_discount_abs)
        return shopify
    credited_gross = abs(as_float(credited_gross))
    invoice_product_gross = abs(as_float(invoice_product_gross))
    if invoice_discount_abs <= 0.0001 or invoice_product_gross <= 0.0001:
        return 0.0
    share = invoice_discount_abs * credited_gross / invoice_product_gross
    return min(share, invoice_discount_abs)


def refund_transaction_amount(refund):
    total = 0.0
    for tx in (refund or {}).get("transactions") or []:
        if not isinstance(tx, dict):
            continue
        kind = (tx.get("kind") or "").lower()
        status = (tx.get("status") or "success").lower()
        if kind != "refund" or status not in ("success", "pending"):
            continue
        total += as_float(tx.get("amount"))
    return total


def is_full_product_refund(invoiced_by_sku, refund_qty, remaining_ordered_qty=0.0):
    """True only when every invoiced product SKU is fully refunded.

    If the sale order still has ordered product qty, this is not a full
    invoice reversal — even when Shopify's refund transaction equals the
    original payment (order-edit refunds often look like that).
    Duplicate invoice lines for one SKU are summed before comparing.
    """
    remaining_ordered_qty = as_float(remaining_ordered_qty)
    if remaining_ordered_qty > 0.0001:
        return False
    if not invoiced_by_sku:
        return False
    if not refund_qty:
        return False
    refund_norm = {norm_sku(sku): as_float(qty) for sku, qty in refund_qty.items()}
    for sku, qty in invoiced_by_sku.items():
        if is_charge_sku(sku):
            continue
        if refund_norm.get(norm_sku(sku), 0.0) + 0.0001 < as_float(qty):
            return False
    return True


def _price_match_rank(line, prices_norm, alias_map):
    price = abs(as_float(line.get("price")))
    if price <= 0:
        return 0
    keys = [norm_sku(line.get("sku")), norm_sku(line.get("barcode"))]
    for key in keys:
        bucket = alias_map.get(key)
        for candidate in prices_norm.get(bucket) or []:
            if abs(candidate - price) < 0.02:
                return 1
    return 0


def allocate_refund_to_lines(
    lines, refund_qty, aliases=None, product_ids=None, prices=None
):
    """Consume refund_qty across credit-note lines.

    Duplicate SKUs (same product, different prices) prefer:
    1. lines still invoiced after the sale qty was reduced (``extra``)
    2. unit price matching the Shopify refund line
    3. newest line id (same order as sale-order qty sync)

    ``lines`` is a list of dicts: {id, sku, quantity} plus optional barcode,
    product_id, extra, price. Returns {line_id: keep_qty}; omitted ids should
    be dropped.
    """
    remaining = {}
    for sku, qty in (refund_qty or {}).items():
        if not sku or is_charge_sku(sku):
            continue
        key = norm_sku(sku)
        remaining[key] = remaining.get(key, 0.0) + as_float(qty)
    alias_map = {key: key for key in remaining}
    for alt, canonical in (aliases or {}).items():
        n_alt, n_can = norm_sku(alt), norm_sku(canonical)
        if n_alt and n_can in remaining:
            alias_map[n_alt] = n_can
    product_to_key = {}
    for pid, sku in (product_ids or {}).items():
        key = norm_sku(sku)
        if pid and key in remaining:
            product_to_key[pid] = key
    prices_norm = {}
    for sku, plist in (prices or {}).items():
        prices_norm[norm_sku(sku)] = [as_float(p) for p in (plist or [])]

    ordered = sorted(
        lines or [],
        key=lambda line: (
            -as_float(line.get("extra")),
            -_price_match_rank(line, prices_norm, alias_map),
            -(line.get("id") or 0),
        ),
    )

    keep = {}
    for line in ordered:
        sku = (line.get("sku") or "").strip()
        if is_charge_sku(sku) or is_charge_sku(line.get("barcode")):
            continue
        quantity = abs(as_float(line.get("quantity")))
        line_id = line.get("id")
        if quantity <= 0:
            continue
        bucket = None
        pid = line.get("product_id")
        if pid in product_to_key:
            bucket = product_to_key[pid]
        if not bucket:
            for raw in (sku, line.get("barcode")):
                mapped = alias_map.get(norm_sku(raw))
                if mapped and remaining.get(mapped, 0.0) > 0.0001:
                    bucket = mapped
                    break
        if not bucket:
            continue
        available = remaining.get(bucket, 0.0)
        if available <= 0.0001:
            continue
        take = min(quantity, available)
        remaining[bucket] = available - take
        keep[line_id] = take
    return keep
