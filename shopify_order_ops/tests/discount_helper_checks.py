"""Standalone checks for Shopify → Odoo discount helpers.

Run: python tests/discount_helper_checks.py
Odoo will not auto-discover this file (no test_ prefix).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "shopify_discount",
    ROOT / "models" / "shopify_discount.py",
)
shopify_discount = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(shopify_discount)

aggregate_by_sku = shopify_discount.aggregate_by_sku
discount_application_labels = shopify_discount.discount_application_labels
discount_sync_enabled = shopify_discount.discount_sync_enabled
discounts_close = shopify_discount.discounts_close
line_discount_amount = shopify_discount.line_discount_amount
line_discount_percent = shopify_discount.line_discount_percent
line_item_quantity = shopify_discount.line_item_quantity
shipping_discount_amount = shopify_discount.shipping_discount_amount
unallocated_discount_amount = shopify_discount.unallocated_discount_amount
merchandise_discount_amount = shopify_discount.merchandise_discount_amount
discount_line_name = shopify_discount.discount_line_name
amounts_close = shopify_discount.amounts_close
order_shipping_amount = shopify_discount.order_shipping_amount
shipping_line_name = shopify_discount.shipping_line_name
shipping_line_net_amount = shopify_discount.shipping_line_net_amount
is_charge_sku = shopify_discount.is_charge_sku


def check(name, cond, detail=""):
    if not cond:
        raise SystemExit("FAIL %s %s" % (name, detail))
    print("OK", name)


def main():
    line = {
        "sku": "LOCK117",
        "title": "Lock",
        "quantity": 2,
        "price": "100.00",
        "total_discount": "40.00",
    }
    check("percent from total_discount", line_discount_percent(line) == 20.0)

    alloc_only = {
        "sku": "LOCK117",
        "quantity": 1,
        "price": "50.00",
        "discount_allocations": [{"amount": "12.50"}],
    }
    check("percent from allocations", line_discount_percent(alloc_only) == 25.0)
    check("amount from allocations", line_discount_amount(alloc_only) == 12.5)

    nested = {
        "quantity": 1,
        "price": "80",
        "discount_allocations": [
            {"amount_set": {"shop_money": {"amount": "8.00"}}},
        ],
    }
    check("amount from amount_set", line_discount_amount(nested) == 8.0)

    free = {"quantity": 1, "price": "15.00", "total_discount": "15.00"}
    check("100 percent", line_discount_percent(free) == 100.0)

    none = {"quantity": 1, "price": "15.00"}
    check("no discount", line_discount_percent(none) == 0.0)

    check("close enough", discounts_close(20.0, 20.004))
    check("not close", not discounts_close(20.0, 20.02))

    mapped = aggregate_by_sku(
        [
            {
                "sku": "A",
                "title": "One",
                "quantity": 1,
                "price": "10",
                "total_discount": "1",
            },
            {
                "sku": "A",
                "title": "One",
                "quantity": 1,
                "price": "10",
                "total_discount": "3",
            },
            {"sku": "B", "quantity": 1, "price": "5"},
        ]
    )
    check("aggregate qty", mapped["A"]["qty"] == 2)
    check("weighted discount", mapped["A"]["discount"] == 20.0)
    check("other sku untouched", mapped["B"]["discount"] == 0.0)
    check(
        "current_quantity wins",
        line_item_quantity({"quantity": 3, "current_quantity": 0}) == 0.0,
    )
    check(
        "quantity fallback",
        line_item_quantity({"quantity": 3}) == 3.0,
    )
    remaining = aggregate_by_sku(
        [
            {
                "sku": "A",
                "quantity": 2,
                "current_quantity": 0,
                "price": "10",
            }
        ]
    )
    check("aggregate remaining qty", remaining["A"]["qty"] == 0.0)

    order = {
        "current_total_discounts": "15.00",
        "line_items": [line],  # 40 allocated — leftover should be 0 (15 < 40? wait)
    }
    # line total_discount 40 > order 15 → leftover 0
    check("no leftover when lines cover it", unallocated_discount_amount(order) == 0.0)

    leftover_order = {
        "total_discounts": "10.00",
        "line_items": [{"quantity": 1, "price": "50", "total_discount": "2.00"}],
        "shipping_lines": [{"price": "8.00", "discounted_price": "8.00"}],
    }
    check("leftover order discount", unallocated_discount_amount(leftover_order) == 8.0)

    merch = {
        "current_total_discounts": "25.00",
        "line_items": [
            {"quantity": 1, "price": "50", "total_discount": "10.00"},
            {"quantity": 1, "price": "50", "total_discount": "10.00"},
        ],
        "shipping_lines": [{"price": "12.00", "discounted_price": "7.00"}],
        "discount_applications": [{"code": "SAVE10"}],
    }
    check("merchandise excludes shipping", merchandise_discount_amount(merch) == 20.0)
    check(
        "discount line name uses code",
        discount_line_name(merch) == "Discount (SAVE10)",
    )
    check("amounts close", amounts_close(-20.0, -20.004))
    check("amounts not close", not amounts_close(-20.0, -20.05))

    ship = {"price": "12.00", "discounted_price": "4.00"}
    check("shipping discount", shipping_discount_amount(ship) == 8.0)
    check("shipping net amount", shipping_line_net_amount(ship) == 4.0)
    check(
        "order shipping from SHIP line",
        order_shipping_amount(
            {
                "shipping_lines": [
                    {
                        "title": "SHIP",
                        "price": "20.00",
                        "discounted_price": "20.00",
                    }
                ]
            }
        )
        == 20.0,
    )
    check(
        "shipping line name uses title",
        shipping_line_name({"shipping_lines": [{"title": "SHIP"}]})
        == "Shipping (SHIP)",
    )
    check(
        "shipping line name prefers code inside weight title",
        shipping_line_name(
            {
                "shipping_lines": [
                    {
                        "title": "SHIP (0.0 lb: Items 0.0 lb, Package 0.0 lb)",
                        "code": "SHIP",
                    }
                ]
            }
        )
        == "Shipping (SHIP)",
    )
    check("charge sku discount", is_charge_sku("SHOPIFY-DISCOUNT") is True)
    check("charge sku shipping", is_charge_sku("SHOPIFY-SHIPPING") is True)
    check("charge sku other", is_charge_sku("100A_LOCK117C_SCP") is False)

    labels = discount_application_labels(
        {
            "discount_applications": [
                {"code": "SAVE20"},
                {"title": "Automatic"},
                {"code": "SAVE20"},
            ]
        }
    )
    check("labels unique", labels == ["SAVE20", "Automatic"])

    pct_order = {
        "line_items": [
            {
                "sku": "VANITY",
                "quantity": 1,
                "price": "499.00",
                "total_discount": "74.85",
            }
        ],
        "discount_codes": [{"code": "SAVE15"}],
        "discount_applications": [
            {
                "code": "SAVE15",
                "value": "15.0",
                "value_type": "percentage",
                "target_type": "line_item",
            }
        ],
    }
    check(
        "percent coupon uses line Disc.%",
        shopify_discount.uses_line_percent_discount(pct_order) is True,
    )
    check(
        "percent coupon code csv",
        shopify_discount.applied_discount_codes_csv(pct_order) == "SAVE15",
    )
    check("499 15 percent", line_discount_percent(pct_order["line_items"][0]) == 15.0)

    fixed_order = {
        "line_items": [
            {"sku": "A", "quantity": 1, "price": "200", "total_discount": "40"}
        ],
        "discount_applications": [
            {
                "code": "TAKE40",
                "value": "40.00",
                "value_type": "fixed_amount",
                "target_type": "line_item",
            }
        ],
    }
    check(
        "fixed amount uses dedicated line",
        shopify_discount.uses_line_percent_discount(fixed_order) is False,
    )
    check(
        "legacy payload without value_type stays dedicated",
        shopify_discount.uses_line_percent_discount(
            {
                "line_items": [
                    {"sku": "A", "quantity": 2, "price": "100", "total_discount": "40"}
                ],
                "discount_applications": [{"code": "SAVE20"}],
            }
        )
        is False,
    )

    check("enabled unset", discount_sync_enabled(None) is True)
    check("enabled true", discount_sync_enabled("True") is True)
    check("disabled", discount_sync_enabled("False") is False)
    check("disabled 0", discount_sync_enabled("0") is False)
    check(
        "direction default shopify_to_odoo",
        shopify_discount.discount_sync_direction(None) == "shopify_to_odoo",
    )
    check(
        "stored two_way treated as shopify_to_odoo",
        shopify_discount.discount_sync_direction("two_way") == "shopify_to_odoo",
    )
    check(
        "two_way does not push to shopify",
        shopify_discount.discount_sync_allows_odoo_to_shopify("True", "two_way")
        is False,
    )
    check(
        "allows shopify to odoo by default",
        shopify_discount.discount_sync_allows_shopify_to_odoo("True", None)
        is True,
    )
    check(
        "blocks odoo to shopify when shopify_to_odoo",
        shopify_discount.discount_sync_allows_odoo_to_shopify(
            "True", "shopify_to_odoo"
        )
        is False,
    )
    check(
        "blocks shopify to odoo when odoo_to_shopify",
        shopify_discount.discount_sync_allows_shopify_to_odoo(
            "True", "odoo_to_shopify"
        )
        is False,
    )
    check("clamp", shopify_discount.clamp_percent(150) == 100.0)
    print("All discount helper checks passed.")


if __name__ == "__main__":
    main()
