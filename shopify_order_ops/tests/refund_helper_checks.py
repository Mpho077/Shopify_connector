"""Standalone checks for Shopify refund → credit-note qty helpers.

Run: python tests/refund_helper_checks.py
Odoo will not auto-discover this file (no test_ prefix).
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_package_module(fullname, relative):
    spec = importlib.util.spec_from_file_location(fullname, ROOT / relative)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[fullname] = mod
    spec.loader.exec_module(mod)
    return mod


pkg = types.ModuleType("shopify_order_ops")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("shopify_order_ops", pkg)
models_pkg = types.ModuleType("shopify_order_ops.models")
models_pkg.__path__ = [str(ROOT / "models")]
sys.modules.setdefault("shopify_order_ops.models", models_pkg)

_load_package_module(
    "shopify_order_ops.models.shopify_discount",
    "models/shopify_discount.py",
)
shopify_refund = _load_package_module(
    "shopify_order_ops.models.shopify_refund",
    "models/shopify_refund.py",
)


def check(name, cond, detail=""):
    if not cond:
        raise SystemExit("FAIL %s %s" % (name, detail))
    print("OK", name)


def main():
    entry = {
        "quantity": 1,
        "line_item": {"sku": "100A_LOCK_SCP", "title": "Lock"},
    }
    check(
        "sku from nested line_item",
        shopify_refund.refund_line_sku(entry) == "100A_LOCK_SCP",
    )
    check(
        "sku from variant fallback",
        shopify_refund.refund_line_sku(
            {"quantity": 1, "line_item": {"variant": {"sku": "LOCK2"}}}
        )
        == "LOCK2",
    )

    refund = {
        "refund_line_items": [
            {"quantity": 1, "line_item": {"sku": "100A_LOCK_SCP"}},
            {"quantity": 1, "line_item": {"sku": "SHOPIFY-DISCOUNT"}},
        ]
    }
    qty = shopify_refund.refund_qty_by_sku(refund)
    check("refund qty skips charge sku", qty == {"100A_LOCK_SCP": 1.0})

    invoiced = {
        "100A_LOCK117C_SCP": 2.0,
        "100A_LOCK_SCP": 1.0,
    }
    check(
        "one sku canceled is not a full refund",
        shopify_refund.is_full_product_refund(
            invoiced, {"100A_LOCK_SCP": 1.0}, remaining_ordered_qty=2.0
        )
        is False,
    )
    check(
        "empty refund_qty with remaining items is not full",
        shopify_refund.is_full_product_refund(invoiced, {}, remaining_ordered_qty=2.0)
        is False,
    )
    check(
        "empty refund_qty is never amount-based full",
        shopify_refund.is_full_product_refund(invoiced, {}, remaining_ordered_qty=0.0)
        is False,
    )
    check(
        "all skus refunded and nothing left on SO is full",
        shopify_refund.is_full_product_refund(
            invoiced,
            {"100A_LOCK117C_SCP": 2.0, "100A_LOCK_SCP": 1.0},
            remaining_ordered_qty=0.0,
        )
        is True,
    )

    keep = shopify_refund.allocate_refund_to_lines(
        [
            {"id": 1, "sku": "100A_LOCK117C_SCP", "quantity": 1},
            {"id": 2, "sku": "100A_LOCK117C_SCP", "quantity": 1},
            {"id": 3, "sku": "100A_LOCK_SCP", "quantity": 1},
            {"id": 4, "sku": "SHOPIFY-DISCOUNT", "quantity": 1},
            {"id": 5, "sku": "SHOPIFY-SHIPPING", "quantity": 1},
        ],
        {"100A_LOCK_SCP": 1.0},
    )
    check("partial keep only canceled sku", keep == {3: 1.0})

    keep_dup = shopify_refund.allocate_refund_to_lines(
        [
            {"id": 1, "sku": "A", "quantity": 1},
            {"id": 2, "sku": "A", "quantity": 1},
        ],
        {"A": 1.0},
    )
    check("duplicate sku consumes newest first", keep_dup == {2: 1.0})

    keep_extra = shopify_refund.allocate_refund_to_lines(
        [
            {
                "id": 1,
                "sku": "100A_LOCK117C_SCP",
                "quantity": 1,
                "price": 23.09,
                "extra": 0,
            },
            {
                "id": 2,
                "sku": "100A_LOCK117C_SCP",
                "quantity": 1,
                "price": 9.50,
                "extra": 1,
            },
        ],
        {"100A_LOCK117C_SCP": 1.0},
    )
    check("duplicate sku prefers invoiced-beyond-ordered", keep_extra == {2: 1.0})

    keep_price = shopify_refund.allocate_refund_to_lines(
        [
            {
                "id": 1,
                "sku": "100A_LOCK117C_SCP",
                "quantity": 1,
                "price": 23.09,
                "extra": 0,
            },
            {
                "id": 2,
                "sku": "100A_LOCK117C_SCP",
                "quantity": 1,
                "price": 9.50,
                "extra": 0,
            },
        ],
        {"100A_LOCK117C_SCP": 1.0},
        prices={"100A_LOCK117C_SCP": [9.50]},
    )
    check("duplicate sku prefers refund unit price", keep_price == {2: 1.0})

    refund_no_sku = {
        "refund_line_items": [
            {"quantity": 1, "line_item_id": 99, "line_item": {}},
        ]
    }
    order = {
        "line_items": [{"id": 99, "sku": "100A_LOCK_SCP", "quantity": 1}],
    }
    check(
        "sku from order line_item_id",
        shopify_refund.refund_qty_by_sku(refund_no_sku, order)
        == {"100A_LOCK_SCP": 1.0},
    )
    check(
        "sku from integer line_item id",
        shopify_refund.refund_qty_by_sku(
            {"refund_line_items": [{"quantity": 1, "line_item": 99}]},
            order,
        )
        == {"100A_LOCK_SCP": 1.0},
    )

    keep_case = shopify_refund.allocate_refund_to_lines(
        [{"id": 1, "sku": "100a_lock_scp", "quantity": 1}],
        {"100A_LOCK_SCP": 1.0},
    )
    check("sku match is case-insensitive", keep_case == {1: 1.0})

    keep_neg = shopify_refund.allocate_refund_to_lines(
        [{"id": 1, "sku": "100A_LOCK_SCP", "quantity": -1}],
        {"100A_LOCK_SCP": 1.0},
    )
    check("negative credit-note qty still matches", keep_neg == {1: 1.0})

    keep_barcode = shopify_refund.allocate_refund_to_lines(
        [{"id": 1, "sku": "", "barcode": "BAR-1", "quantity": 1}],
        {"100A_LOCK_SCP": 1.0},
        aliases={"BAR-1": "100A_LOCK_SCP"},
    )
    check("barcode alias matches", keep_barcode == {1: 1.0})

    keep_pid = shopify_refund.allocate_refund_to_lines(
        [{"id": 1, "sku": "", "product_id": 42, "quantity": 1}],
        {"100A_LOCK_SCP": 1.0},
        product_ids={42: "100A_LOCK_SCP"},
    )
    check("product id matches", keep_pid == {1: 1.0})

    disc_entry = {
        "quantity": 1,
        "line_item": {
            "sku": "100A_LOCK117C_SCP",
            "quantity": 1,
            "price": "9.50",
            "total_discount": "0.95",
        },
    }
    check(
        "refund line discount from total_discount",
        abs(shopify_refund.refund_entry_discount(disc_entry) - 0.95) < 0.001,
    )
    check(
        "refund line discount prorated by qty",
        abs(
            shopify_refund.refund_entry_discount(
                {
                    "quantity": 1,
                    "line_item": {
                        "sku": "LOCK",
                        "quantity": 2,
                        "total_discount": "10.00",
                    },
                }
            )
            - 5.0
        )
        < 0.001,
    )
    check(
        "shopify allocation wins over proportional",
        abs(
            shopify_refund.discount_share_for_credit(
                {"refund_line_items": [disc_entry]},
                None,
                credited_gross=9.50,
                invoice_product_gross=32.59,
                invoice_discount_abs=7.09,
            )
            - 0.95
        )
        < 0.001,
    )
    share = shopify_refund.discount_share_for_credit(
        {"refund_line_items": [{"quantity": 1, "line_item": {"sku": "LOCK"}}]},
        None,
        credited_gross=9.50,
        invoice_product_gross=32.59,
        invoice_discount_abs=7.09,
    )
    check(
        "cart discount split across credited products",
        abs(share - (7.09 * 9.50 / 32.59)) < 0.001,
        share,
    )

    print("All refund helper checks passed.")


if __name__ == "__main__":
    main()
