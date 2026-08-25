"""Standalone checks for Shopify order tag parsing.

Run: python tests/order_tag_checks.py
Odoo will not auto-discover this file (no test_ prefix).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "shopify_order_tags",
    ROOT / "models" / "shopify_order_tags.py",
)
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)


def check(name, cond, detail=""):
    if not cond:
        raise SystemExit("FAIL %s %s" % (name, detail))
    print("OK", name)


def main():
    check(
        "csv tags",
        mod.shopify_tag_names({"tags": "PICKUP_IN_STORE, STOQ-preorder"})
        == ["PICKUP_IN_STORE", "STOQ-preorder"],
    )
    check(
        "list tags",
        mod.shopify_tag_names({"tags": ["PICKUP_IN_STORE", "STOQ-preorder"]})
        == ["PICKUP_IN_STORE", "STOQ-preorder"],
    )
    check("empty", mod.shopify_tag_names({"tags": ""}) == [])
    check("missing", mod.shopify_tag_names({}) == [])
    check(
        "dedupe case",
        mod.shopify_tag_names({"tags": "PICKUP_IN_STORE, pickup_in_store"})
        == ["PICKUP_IN_STORE"],
    )
    check(
        "csv join",
        mod.tags_csv(["PICKUP_IN_STORE", "STOQ-preorder"])
        == "PICKUP_IN_STORE, STOQ-preorder",
    )
    check(
        "pickup tag exact",
        mod.pickup_in_store_tag_present(["PICKUP_IN_STORE", "STOQ-preorder"]),
    )
    check(
        "pickup tag case",
        mod.pickup_in_store_tag_present(["pickup_in_store"]),
    )
    check(
        "not pickup",
        not mod.pickup_in_store_tag_present(["STOQ-preorder"]),
    )
    check(
        "empty not pickup",
        not mod.pickup_in_store_tag_present([]),
    )
    print("\nAll standalone order tag checks passed.")


if __name__ == "__main__":
    main()
