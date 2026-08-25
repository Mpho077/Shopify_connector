"""Shopify order tag helpers. No Odoo imports."""


def shopify_tag_names(order):
    """Comma-separated REST `tags` or a list -> ordered unique names."""
    if isinstance(order, dict):
        raw = order.get("tags")
    else:
        raw = order
    if raw is None or raw is False:
        names = []
    elif isinstance(raw, (list, tuple)):
        names = [str(item).strip() for item in raw if str(item).strip()]
    else:
        names = [part.strip() for part in str(raw).split(",") if part.strip()]
    seen = set()
    unique = []
    for name in names:
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(name)
    return unique


def tags_csv(names):
    return ", ".join(names)


def _tag_compact(name):
    return "".join(ch for ch in str(name).casefold() if ch.isalnum())


def pickup_in_store_tag_present(names):
    """True when tags include Shopify's PICKUP_IN_STORE (any punctuation)."""
    for name in names or []:
        if _tag_compact(name) == "pickupinstore":
            return True
    return False
