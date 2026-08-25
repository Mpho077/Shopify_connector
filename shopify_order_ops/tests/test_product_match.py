from unittest.mock import patch

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "shopify_order_ops")
class TestProductMatch(TransactionCase):
    def setUp(self):
        super().setUp()
        self.api = self.env["shopify.api.client"]
        self.good = self.env["product.product"].create(
            {
                "name": "[PEM900A] Premium Basin",
                "default_code": "PEM900A",
                "type": "consu",
                "list_price": 900.0,
                "shopify_variant_id": "88001",
            }
        )
        self.env["product.product"].create(
            {
                "name": "PEM900A Old Duplicate",
                "default_code": "PEM900A",
                "type": "consu",
                "list_price": 900.0,
                "shopify_variant_id": "88002",
            }
        )

    def test_variant_id_match_is_primary(self):
        product = self.api.match_product_for_shopify_line(
            {
                "sku": "PEM900A",
                "variant_id": 88001,
                "title": "Wrong title on purpose",
            }
        )
        self.assertEqual(product, self.good)

    def test_duplicate_sku_uses_variant_id_among_matches(self):
        product = self.api.match_product_by_sku(
            "PEM900A",
            variant_id=88001,
            name="Totally Different Product",
        )
        self.assertEqual(product, self.good)

    def test_duplicate_sku_falls_back_to_line_name(self):
        self.good.shopify_variant_id = False
        product = self.api.match_product_by_sku(
            "PEM900A",
            name="Premium Basin",
        )
        self.assertEqual(product, self.good)

    def test_shopify_line_item_name_includes_variant(self):
        name = self.api.shopify_line_item_name(
            {
                "title": "1500 Bath",
                "variant_title": "White",
            }
        )
        self.assertEqual(name, "1500 Bath / White")

    def test_order_pull_imports_missing_variant_from_shopify(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "shopify_order_ops.product_sync_enabled", "True"
        )
        self.env["ir.config_parameter"].sudo().set_param(
            "shopify_order_ops.product_create_mode", "create_update"
        )
        line_item = {
            "sku": "OVMFPR",
            "variant_id": 47734441181333,
            "product_id": 900100,
            "title": "Ovia MF Product",
            "price": "499.00",
        }
        shop_product = {
            "id": 900100,
            "title": "Ovia MF Product",
            "status": "active",
            "variants": [
                {
                    "id": 47734441181333,
                    "sku": "OVMFPR",
                    "price": "499.00",
                    "barcode": None,
                }
            ],
        }
        variant = shop_product["variants"][0]
        sync = self.env["shopify.product.sync"]

        def fake_get_variant(_self, variant_id):
            if str(variant_id) == "47734441181333":
                return dict(variant, product_id=900100)
            return {}

        def fake_get_product(_self, product_id):
            if str(product_id) == "900100":
                return shop_product
            return {}

        with patch.object(
            type(self.env["shopify.api.client"]),
            "get_variant",
            fake_get_variant,
        ), patch.object(
            type(self.env["shopify.api.client"]),
            "get_product",
            fake_get_product,
        ):
            product = sync.match_or_import_for_order_line(
                "OVMFPR", line_item=line_item
            )
        self.assertTrue(product)
        self.assertEqual(product.default_code, "OVMFPR")
        self.assertEqual(product.shopify_variant_id, "47734441181333")
