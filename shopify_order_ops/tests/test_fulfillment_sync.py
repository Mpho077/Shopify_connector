from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "shopify_order_ops")
class TestFulfillmentSyncDirection(TransactionCase):
    def setUp(self):
        super().setUp()
        self.icp = self.env["ir.config_parameter"].sudo()
        self.sync = self.env["shopify.fulfillment.sync"]

    def _set_fulfillment(self, direction, enabled=True):
        self.icp.set_param(
            "shopify_order_ops.fulfillment_sync_enabled", "True" if enabled else "False"
        )
        self.icp.set_param("shopify_order_ops.fulfillment_sync_direction", direction)

    def test_settings_field_on_config(self):
        settings = self.env["res.config.settings"].create({})
        self.assertTrue(hasattr(settings, "shopify_fulfillment_sync_direction"))

    def test_odoo_to_shopify_direction(self):
        self._set_fulfillment("odoo_to_shopify")
        self.assertTrue(self.sync._odoo_to_shopify_enabled())
        self.assertFalse(self.sync._shopify_to_odoo_enabled())

    def test_shopify_to_odoo_direction(self):
        self._set_fulfillment("shopify_to_odoo")
        self.assertFalse(self.sync._odoo_to_shopify_enabled())
        self.assertTrue(self.sync._shopify_to_odoo_enabled())

    def test_two_way_direction(self):
        self._set_fulfillment("two_way")
        self.assertTrue(self.sync._odoo_to_shopify_enabled())
        self.assertTrue(self.sync._shopify_to_odoo_enabled())

    def test_disabled_overrides_direction(self):
        self._set_fulfillment("two_way", enabled=False)
        self.assertFalse(self.sync._odoo_to_shopify_enabled())
        self.assertFalse(self.sync._shopify_to_odoo_enabled())

    def test_order_needs_fulfillment_pull_respects_direction(self):
        product = self.env["product.product"].create(
            {"name": "Fulfillment Test", "type": "product", "list_price": 10.0}
        )
        order = self.env["sale.order"].create(
            {
                "partner_id": self.env.ref("base.res_partner_1").id,
                "shopify_order_id": "8001",
                "shopify_order_name": "#8001",
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": product.id,
                            "product_uom_qty": 1,
                            "price_unit": 10.0,
                        },
                    )
                ],
            }
        )
        order.action_confirm()
        shopify_order = {
            "fulfillment_status": "fulfilled",
            "fulfillments": [{"id": 1, "tracking_number": "TRACK123"}],
        }
        self._set_fulfillment("odoo_to_shopify")
        self.assertFalse(
            self.sync.order_needs_fulfillment_pull(order, shopify_order)
        )
        self._set_fulfillment("shopify_to_odoo")
        self.assertTrue(
            self.sync.order_needs_fulfillment_pull(order, shopify_order)
        )
