from datetime import datetime, timedelta

from odoo.tests.common import Form
from odoo.tests import TransactionCase, tagged

from odoo.addons.shopify_order_ops.models.discount_catalogue import (
    extract_discount_gid,
    normalize_percent,
    parse_discount_node,
)


@tagged("post_install", "-at_install", "shopify_order_ops")
class TestDiscountCatalogue(TransactionCase):
    def setUp(self):
        super().setUp()
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("shopify_order_ops.discount_catalogue_sync_enabled", "True")
        icp.set_param("shopify_order_ops.discount_apply_manual_orders", "True")
        self.partner = self.env["res.partner"].create({"name": "Catalogue Customer"})
        self.product = self.env["product.product"].create(
            {
                "name": "Ovia Zurich Extra Height",
                "default_code": "OVIZURBTW",
                "type": "service",
                "list_price": 499.0,
                "shopify_variant_id": "900001",
                "shopify_product_id": "900000",
            }
        )
        self.delivery_fee = self.env["product.product"].create(
            {
                "name": "Delivery Fee",
                "default_code": "DEL-FEE",
                "type": "service",
                "list_price": 99.0,
            }
        )
        self.instore_only = self.env["product.product"].create(
            {
                "name": "Instore Only Accessory",
                "default_code": "INSTORE-1",
                "type": "consu",
                "list_price": 50.0,
            }
        )

    def _afterpay(self, **extra):
        now = datetime.now()
        vals = {
            "title": "Afterpay Day Sale",
            "shopify_discount_id": "gid://shopify/DiscountAutomaticNode/1",
            "method": "automatic",
            "applies_to": "order",
            "value_type": "percentage",
            "value_percent": 15.0,
            "status": "active",
            "active": True,
            "min_requirement": "none",
            "customer_eligibility": "all",
            "date_start": now - timedelta(days=1),
            "date_end": now + timedelta(days=7),
        }
        vals.update(extra)
        return self.env["shopify.discount"].create(vals)

    def _quote(self, **extra):
        vals = {
            "partner_id": self.partner.id,
            "order_line": [
                (
                    0,
                    0,
                    {
                        "product_id": self.product.id,
                        "product_uom_qty": 1,
                        "price_unit": 499.0,
                    },
                )
            ],
        }
        vals.update(extra)
        return self.env["sale.order"].create(vals)

    def test_normalize_percent_fraction_and_whole(self):
        self.assertEqual(normalize_percent(0.15), 15.0)
        self.assertEqual(normalize_percent(15), 15.0)

    def test_parse_afterpay_automatic_node(self):
        node = {
            "id": "gid://shopify/DiscountAutomaticNode/99",
            "automaticDiscount": {
                "__typename": "DiscountAutomaticBasic",
                "title": "Afterpay Day Sale",
                "status": "ACTIVE",
                "startsAt": "2026-08-11T00:00:00+10:00",
                "endsAt": "2026-08-24T00:00:00+10:00",
                "asyncUsageCount": 627,
                "appliesOnOneTimePurchase": True,
                "appliesOnSubscription": False,
                "discountClass": "ORDER",
                "combinesWith": {
                    "orderDiscounts": False,
                    "productDiscounts": False,
                    "shippingDiscounts": False,
                },
                "customerGets": {
                    "appliesOnOneTimePurchase": True,
                    "appliesOnSubscription": False,
                    "value": {"percentage": 0.15},
                    "items": {"__typename": "AllDiscountItems"},
                },
                "customerSelection": {"__typename": "DiscountCustomerAll"},
            },
        }
        vals = parse_discount_node(node)
        self.assertEqual(vals["title"], "Afterpay Day Sale")
        self.assertEqual(vals["method"], "automatic")
        self.assertEqual(vals["applies_to"], "order")
        self.assertEqual(vals["value_type"], "percentage")
        self.assertEqual(vals["value_percent"], 15.0)
        self.assertEqual(vals["min_requirement"], "none")
        self.assertFalse(vals["combines_order"])
        self.assertTrue(vals["active"])
        self.assertEqual(vals["purchase_type"], "one_time")

    def test_extract_discount_gid(self):
        self.assertEqual(
            extract_discount_gid(
                {"admin_graphql_api_id": "gid://shopify/DiscountAutomaticNode/1"}
            ),
            "gid://shopify/DiscountAutomaticNode/1",
        )

    def test_settings_fields(self):
        settings = self.env["res.config.settings"].create({})
        self.assertTrue(hasattr(settings, "shopify_discount_catalogue_sync_enabled"))
        self.assertTrue(hasattr(settings, "shopify_discount_apply_manual_orders"))

    def test_automatic_percent_applies_on_manual_quote(self):
        self._afterpay()
        order = self._quote()
        line = order.order_line.filtered(lambda l: l.product_id == self.product)
        self.assertAlmostEqual(line.discount, 15.0, places=2)
        self.assertEqual(order.shopify_discount_codes, "Afterpay Day Sale")
        self.assertFalse(order.order_line.filtered("shopify_discount_line"))

    def test_does_not_apply_on_shopify_order(self):
        self._afterpay()
        order = self._quote(shopify_order_id="9001", shopify_order_name="#9001")
        line = order.order_line.filtered(lambda l: l.product_id == self.product)
        self.assertAlmostEqual(line.discount, 0.0, places=2)

    def test_typed_code_applies(self):
        self.env["shopify.discount"].create(
            {
                "title": "Save fifteen",
                "shopify_discount_id": "gid://shopify/DiscountCodeNode/2",
                "method": "code",
                "applies_to": "order",
                "value_type": "percentage",
                "value_percent": 15.0,
                "status": "active",
                "active": True,
                "codes": "SAVE15",
                "min_requirement": "none",
            }
        )
        order = self._quote(shopify_coupon_code="save15")
        line = order.order_line.filtered(lambda l: l.product_id == self.product)
        self.assertAlmostEqual(line.discount, 15.0, places=2)
        self.assertEqual(order.shopify_discount_codes, "SAVE15")

    def test_staff_disc_percent_not_overwritten(self):
        self._afterpay()
        order = self._quote()
        line = order.order_line.filtered(lambda l: l.product_id == self.product)
        line.write({"discount": 10.0})
        self.assertAlmostEqual(line.discount, 10.0, places=2)

    def test_automatic_percent_shows_before_save(self):
        self._afterpay()
        form = Form(self.env["sale.order"])
        form.partner_id = self.partner
        with form.order_line.new() as line:
            line.product_id = self.product
            self.assertAlmostEqual(line.discount, 15.0, places=2)

    def test_delivery_fee_does_not_get_percent(self):
        self._afterpay()
        order = self._quote(
            order_line=[
                (
                    0,
                    0,
                    {
                        "product_id": self.product.id,
                        "product_uom_qty": 1,
                        "price_unit": 499.0,
                    },
                ),
                (
                    0,
                    0,
                    {
                        "product_id": self.delivery_fee.id,
                        "product_uom_qty": 1,
                        "price_unit": 99.0,
                        "name": "Delivery Fee",
                    },
                ),
            ]
        )
        shop = order.order_line.filtered(lambda l: l.product_id == self.product)
        fee = order.order_line.filtered(lambda l: l.product_id == self.delivery_fee)
        self.assertAlmostEqual(shop.discount, 15.0, places=2)
        self.assertAlmostEqual(fee.discount, 0.0, places=2)

    def test_clears_percent_previously_applied_to_delivery(self):
        self._afterpay()
        order = self._quote(
            order_line=[
                (
                    0,
                    0,
                    {
                        "product_id": self.product.id,
                        "product_uom_qty": 1,
                        "price_unit": 499.0,
                    },
                ),
                (
                    0,
                    0,
                    {
                        "product_id": self.delivery_fee.id,
                        "product_uom_qty": 1,
                        "price_unit": 99.0,
                        "name": "Delivery Fee",
                        "discount": 15.0,
                    },
                ),
            ]
        )
        fee = order.order_line.filtered(lambda l: l.product_id == self.delivery_fee)
        self.assertAlmostEqual(fee.discount, 0.0, places=2)

    def test_instore_only_product_does_not_get_percent(self):
        self._afterpay()
        order = self._quote(
            order_line=[
                (
                    0,
                    0,
                    {
                        "product_id": self.product.id,
                        "product_uom_qty": 1,
                        "price_unit": 499.0,
                    },
                ),
                (
                    0,
                    0,
                    {
                        "product_id": self.instore_only.id,
                        "product_uom_qty": 1,
                        "price_unit": 50.0,
                    },
                ),
            ]
        )
        shop = order.order_line.filtered(lambda l: l.product_id == self.product)
        local = order.order_line.filtered(lambda l: l.product_id == self.instore_only)
        self.assertAlmostEqual(shop.discount, 15.0, places=2)
        self.assertAlmostEqual(local.discount, 0.0, places=2)
