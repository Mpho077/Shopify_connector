from unittest.mock import patch

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "shopify_order_ops")
class TestOrderDiscountSync(TransactionCase):
    def setUp(self):
        super().setUp()
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("shopify_order_ops.discount_sync_enabled", "True")
        icp.set_param("shopify_order_ops.discount_sync_direction", "shopify_to_odoo")
        icp.set_param("shopify_order_ops.order_pull_enabled", "False")
        icp.set_param("shopify_order_ops.skip_product_create", "True")

        self.partner = self.env["res.partner"].create({"name": "Discount Customer"})
        self.product = self.env["product.product"].create(
            {
                "name": "Discount Widget",
                "default_code": "DISC-SKU",
                "type": "service",
                "list_price": 100.0,
            }
        )
        self.order = self.env["sale.order"].create(
            {
                "partner_id": self.partner.id,
                "shopify_order_id": "9101",
                "shopify_order_name": "#9101",
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": self.product.id,
                            "product_uom_qty": 2,
                            "price_unit": 100.0,
                        },
                    )
                ],
            }
        )
        self.order.action_confirm()
        self.engine = self.env["shopify.order.pull.engine"]

    def _log(self, _level, _message):
        return None

    def test_settings_field_on_config(self):
        settings = self.env["res.config.settings"].create({})
        self.assertTrue(hasattr(settings, "shopify_discount_sync_enabled"))
        self.assertTrue(hasattr(settings, "shopify_discount_sync_direction"))
        self.assertTrue(hasattr(settings, "shopify_shipping_charge_sync_enabled"))
        self.assertTrue(hasattr(settings, "shopify_order_sync_after"))
        self.assertIn(
            "shopify_to_odoo",
            dict(settings._fields["shopify_discount_sync_direction"].selection),
        )

    def test_legacy_two_way_direction_can_save(self):
        settings = self.env["res.config.settings"].create(
            {"shopify_discount_sync_direction": "two_way"}
        )
        self.assertEqual(
            settings.shopify_discount_sync_direction, "shopify_to_odoo"
        )

    def test_sync_adds_dedicated_discount_line(self):
        shopify_order = {
            "name": "#9101",
            "line_items": [
                {
                    "sku": "DISC-SKU",
                    "title": "Discount Widget",
                    "quantity": 2,
                    "price": "100.00",
                    "total_discount": "40.00",
                }
            ],
            "discount_applications": [{"code": "SAVE20"}],
        }
        self.engine._sync_line_discounts(
            self.order, shopify_order, self._log, on_create=True
        )
        product_line = self.order.order_line.filtered(
            lambda l: not l.shopify_discount_line and not l.shopify_shipping_line
        )
        disc_line = self.order.order_line.filtered("shopify_discount_line")
        self.assertEqual(product_line.discount, 0.0)
        self.assertEqual(len(disc_line), 1)
        self.assertEqual(disc_line.price_unit, -40.0)
        self.assertEqual(disc_line.name, "Discount (SAVE20)")
        self.assertTrue(disc_line.product_id)
        self.assertEqual(disc_line.product_id.default_code, "SHOPIFY-DISCOUNT")
        self.assertFalse(self._discount_jobs(product_line))
        self.assertEqual(self.order.shopify_discount_codes, "SAVE20")

    def test_sync_is_idempotent(self):
        shopify_order = {
            "name": "#9101",
            "line_items": [
                {
                    "sku": "DISC-SKU",
                    "quantity": 2,
                    "price": "100.00",
                    "total_discount": "40.00",
                }
            ],
        }
        self.engine._sync_line_discounts(
            self.order, shopify_order, self._log, on_create=True
        )
        self.engine._sync_line_discounts(
            self.order, shopify_order, self._log, on_create=True
        )
        disc_line = self.order.order_line.filtered("shopify_discount_line")
        self.assertEqual(len(disc_line), 1)
        self.assertEqual(disc_line.price_unit, -40.0)
        product_line = self.order.order_line.filtered(
            lambda l: not l.shopify_discount_line and not l.shopify_shipping_line
        )
        self.assertEqual(product_line.discount, 0.0)

    def test_disabled_leaves_discount_untouched(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "shopify_order_ops.discount_sync_enabled", "False"
        )
        shopify_order = {
            "name": "#9101",
            "line_items": [
                {
                    "sku": "DISC-SKU",
                    "quantity": 2,
                    "price": "100.00",
                    "total_discount": "40.00",
                }
            ],
        }
        self.engine._sync_line_discounts(
            self.order, shopify_order, self._log, on_create=True
        )
        self.assertEqual(self.order.order_line.discount, 0.0)

    def test_odoo_to_shopify_direction_skips_pull_sync(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "shopify_order_ops.discount_sync_direction", "odoo_to_shopify"
        )
        shopify_order = {
            "name": "#9101",
            "line_items": [
                {
                    "sku": "DISC-SKU",
                    "quantity": 2,
                    "price": "100.00",
                    "total_discount": "40.00",
                }
            ],
        }
        self.engine._sync_line_discounts(
            self.order, shopify_order, self._log, on_create=True
        )
        self.assertEqual(self.order.order_line.discount, 0.0)

    def test_create_sale_order_sets_discount(self):
        shopify_order = {
            "name": "#9102",
            "line_items": [
                {
                    "sku": "ANY",
                    "title": "Discounted line",
                    "quantity": 1,
                    "price": "80.00",
                    "discount_allocations": [{"amount": "8.00"}],
                }
            ],
        }
        so = self.engine._create_sale_order(
            shopify_order, "9102", self.partner, self._log
        )
        self.engine._sync_line_discounts(
            so, shopify_order, self._log, on_create=True
        )
        product_line = so.order_line.filtered(
            lambda l: not l.shopify_discount_line and not l.shopify_shipping_line
        )
        disc_line = so.order_line.filtered("shopify_discount_line")
        self.assertEqual(len(product_line), 1)
        self.assertEqual(product_line.price_unit, 80.0)
        self.assertEqual(product_line.discount, 0.0)
        self.assertEqual(len(disc_line), 1)
        self.assertEqual(disc_line.price_unit, -8.0)
        self.assertTrue(disc_line.product_id)

    def test_order_level_discount_covers_all_products(self):
        other = self.env["product.product"].create(
            {
                "name": "Second Widget",
                "default_code": "DISC-SKU-2",
                "type": "service",
                "list_price": 50.0,
            }
        )
        self.order.write(
            {
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": other.id,
                            "product_uom_qty": 1,
                            "price_unit": 50.0,
                        },
                    )
                ]
            }
        )
        shopify_order = {
            "name": "#9101",
            "current_total_discounts": "15.00",
            "line_items": [
                {
                    "sku": "DISC-SKU",
                    "quantity": 2,
                    "price": "100.00",
                    "total_discount": "10.00",
                },
                {
                    "sku": "DISC-SKU-2",
                    "quantity": 1,
                    "price": "50.00",
                    "total_discount": "5.00",
                },
            ],
            "discount_applications": [{"code": "SAVE10"}],
        }
        self.engine._sync_line_discounts(
            self.order, shopify_order, self._log, on_create=True
        )
        product_lines = self.order.order_line.filtered(
            lambda l: not l.shopify_discount_line and not l.shopify_shipping_line
        )
        disc_line = self.order.order_line.filtered("shopify_discount_line")
        self.assertEqual(len(product_lines), 2)
        self.assertTrue(all(not line.discount for line in product_lines))
        self.assertEqual(len(disc_line), 1)
        self.assertEqual(disc_line.price_unit, -15.0)
        self.assertEqual(disc_line.name, "Discount (SAVE10)")

    def test_sync_adds_shipping_line(self):
        shopify_order = {
            "name": "#1030",
            "line_items": [
                {
                    "sku": "DISC-SKU",
                    "quantity": 2,
                    "price": "100.00",
                }
            ],
            "shipping_lines": [
                {
                    "title": "SHIP",
                    "code": "SHIP",
                    "price": "20.00",
                    "discounted_price": "20.00",
                }
            ],
        }
        self.engine._sync_line_discounts(
            self.order, shopify_order, self._log, on_create=True
        )
        ship_line = self.order.order_line.filtered("shopify_shipping_line")
        self.assertEqual(len(ship_line), 1)
        self.assertEqual(ship_line.price_unit, 20.0)
        self.assertEqual(ship_line.name, "Shipping (SHIP)")
        self.assertTrue(ship_line.product_id)
        self.assertEqual(ship_line.product_id.default_code, "SHOPIFY-SHIPPING")
        self.assertFalse(self._discount_jobs(ship_line))

    def test_sync_discount_and_shipping_together(self):
        """Custom -$20 discount plus $20 SHIP leaves product lines at list."""
        shopify_order = {
            "name": "#1030",
            "current_total_discounts": "20.00",
            "discount_applications": [{"title": "Custom discount"}],
            "line_items": [
                {
                    "sku": "DISC-SKU",
                    "quantity": 2,
                    "price": "100.00",
                    "total_discount": "20.00",
                }
            ],
            "shipping_lines": [
                {
                    "title": "SHIP",
                    "price": "20.00",
                    "discounted_price": "20.00",
                }
            ],
        }
        self.engine._sync_line_discounts(
            self.order, shopify_order, self._log, on_create=True
        )
        product_line = self.order.order_line.filtered(
            lambda l: not l.shopify_discount_line and not l.shopify_shipping_line
        )
        disc_line = self.order.order_line.filtered("shopify_discount_line")
        ship_line = self.order.order_line.filtered("shopify_shipping_line")
        self.assertEqual(product_line.price_unit, 100.0)
        self.assertEqual(product_line.discount, 0.0)
        self.assertEqual(disc_line.price_unit, -20.0)
        self.assertEqual(disc_line.name, "Discount (Custom discount)")
        self.assertEqual(ship_line.price_unit, 20.0)
        self.assertEqual(ship_line.name, "Shipping (SHIP)")
        self.assertAlmostEqual(
            sum(line.price_subtotal for line in self.order.order_line),
            200.0,
            places=2,
        )

    def test_shipping_sync_is_idempotent(self):
        shopify_order = {
            "name": "#1030",
            "shipping_lines": [
                {"title": "SHIP", "price": "20.00", "discounted_price": "20.00"}
            ],
            "line_items": [
                {"sku": "DISC-SKU", "quantity": 2, "price": "100.00"}
            ],
        }
        self.engine._sync_line_discounts(
            self.order, shopify_order, self._log, on_create=True
        )
        self.engine._sync_line_discounts(
            self.order, shopify_order, self._log, on_create=True
        )
        ship_line = self.order.order_line.filtered("shopify_shipping_line")
        self.assertEqual(len(ship_line), 1)
        self.assertEqual(ship_line.price_unit, 20.0)

    def test_disabled_shipping_leaves_no_shipping_line(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "shopify_order_ops.shipping_charge_sync_enabled", "False"
        )
        shopify_order = {
            "name": "#1030",
            "shipping_lines": [
                {"title": "SHIP", "price": "20.00", "discounted_price": "20.00"}
            ],
            "line_items": [
                {"sku": "DISC-SKU", "quantity": 2, "price": "100.00"}
            ],
        }
        self.engine._sync_line_discounts(
            self.order, shopify_order, self._log, on_create=True
        )
        self.assertFalse(self.order.order_line.filtered("shopify_shipping_line"))

    def test_existing_order_does_not_gain_shipping_line(self):
        shopify_order = {
            "name": "#9101",
            "created_at": "2024-01-01T10:00:00Z",
            "shipping_lines": [
                {"title": "SHIP", "price": "20.00", "discounted_price": "20.00"}
            ],
            "line_items": [
                {"sku": "DISC-SKU", "quantity": 2, "price": "100.00"}
            ],
        }
        self.engine._sync_line_discounts(self.order, shopify_order, self._log)
        self.assertFalse(self.order.order_line.filtered("shopify_shipping_line"))
        self.assertFalse(self.order.order_line.filtered("shopify_discount_line"))

    def test_cutoff_allows_shipping_on_recent_existing_order(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "shopify_order_ops.order_sync_after", "2026-08-01 00:00:00"
        )
        shopify_order = {
            "name": "#9101",
            "created_at": "2026-08-19T17:00:00Z",
            "shipping_lines": [
                {"title": "SHIP", "price": "15.00", "discounted_price": "15.00"}
            ],
            "line_items": [
                {"sku": "DISC-SKU", "quantity": 2, "price": "100.00"}
            ],
        }
        self.engine._sync_line_discounts(self.order, shopify_order, self._log)
        ship_line = self.order.order_line.filtered("shopify_shipping_line")
        self.assertEqual(len(ship_line), 1)
        self.assertEqual(ship_line.price_unit, 15.0)

    def test_cutoff_blocks_shipping_on_older_create(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "shopify_order_ops.order_sync_after", "2026-08-19 18:00:00"
        )
        shopify_order = {
            "name": "#9101",
            "created_at": "2026-08-19T10:00:00Z",
            "shipping_lines": [
                {"title": "SHIP", "price": "15.00", "discounted_price": "15.00"}
            ],
            "line_items": [
                {"sku": "DISC-SKU", "quantity": 2, "price": "100.00"}
            ],
        }
        self.engine._sync_line_discounts(
            self.order, shopify_order, self._log, on_create=True
        )
        self.assertFalse(self.order.order_line.filtered("shopify_shipping_line"))

    def test_skip_order_before_cutoff(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "shopify_order_ops.order_sync_after", "2026-08-19 17:00:00"
        )
        self.assertTrue(
            self.engine.skip_order_before_cutoff(
                {"name": "#1", "created_at": "2026-08-01T10:00:00Z"}
            )
        )
        self.assertFalse(
            self.engine.skip_order_before_cutoff(
                {"name": "#2", "created_at": "2026-08-19T18:00:00Z"}
            )
        )

    def test_percentage_coupon_sets_line_disc_percent(self):
        """15% Shopify coupon on $499 lands as Disc.% 15, not SHOPIFY-DISCOUNT."""
        self.order.order_line.write({"price_unit": 499.0, "product_uom_qty": 1})
        shopify_order = {
            "name": "#9101",
            "line_items": [
                {
                    "sku": "DISC-SKU",
                    "title": "Vanity",
                    "quantity": 1,
                    "price": "499.00",
                    "total_discount": "74.85",
                }
            ],
            "discount_codes": [{"code": "SAVE15", "amount": "74.85", "type": "percentage"}],
            "discount_applications": [
                {
                    "code": "SAVE15",
                    "value": "15.0",
                    "value_type": "percentage",
                    "target_type": "line_item",
                }
            ],
        }
        self.engine._sync_line_discounts(
            self.order, shopify_order, self._log, on_create=True
        )
        product_line = self.order.order_line.filtered(
            lambda l: not l.shopify_discount_line and not l.shopify_shipping_line
        )
        self.assertEqual(len(product_line), 1)
        self.assertAlmostEqual(product_line.discount, 15.0, places=2)
        self.assertAlmostEqual(product_line.price_unit, 499.0, places=2)
        self.assertAlmostEqual(product_line.price_subtotal, 424.15, places=2)
        self.assertFalse(self.order.order_line.filtered("shopify_discount_line"))
        self.assertEqual(self.order.shopify_discount_codes, "SAVE15")

    def test_existing_synco_percent_is_kept(self):
        """Existing Disc.% 15 is not stripped when Shopify still has 15%."""
        self.order.order_line.write(
            {"price_unit": 499.0, "product_uom_qty": 1, "discount": 15.0}
        )
        shopify_order = {
            "name": "#9101",
            "created_at": "2024-01-01T10:00:00Z",
            "line_items": [
                {
                    "sku": "DISC-SKU",
                    "quantity": 1,
                    "price": "499.00",
                    "total_discount": "74.85",
                }
            ],
            "discount_applications": [
                {
                    "code": "SAVE15",
                    "value": "15.0",
                    "value_type": "percentage",
                    "target_type": "line_item",
                }
            ],
        }
        self.engine._sync_line_discounts(self.order, shopify_order, self._log)
        product_line = self.order.order_line.filtered(
            lambda l: not l.shopify_discount_line and not l.shopify_shipping_line
        )
        self.assertAlmostEqual(product_line.discount, 15.0, places=2)
        self.assertFalse(self.order.order_line.filtered("shopify_discount_line"))
        self.assertEqual(self.order.shopify_discount_codes, "SAVE15")

    def test_existing_synco_percent_not_cleared_when_fixed_backfill_skipped(self):
        """Do not wipe existing Disc.% when a dedicated line would not be added."""
        self.order.order_line.write({"discount": 15.0})
        shopify_order = {
            "name": "#9101",
            "created_at": "2024-01-01T10:00:00Z",
            "line_items": [
                {
                    "sku": "DISC-SKU",
                    "quantity": 2,
                    "price": "100.00",
                    "total_discount": "40.00",
                }
            ],
            "discount_applications": [
                {
                    "code": "SAVE40",
                    "value": "40.00",
                    "value_type": "fixed_amount",
                    "target_type": "line_item",
                }
            ],
        }
        self.engine._sync_line_discounts(self.order, shopify_order, self._log)
        product_line = self.order.order_line.filtered(
            lambda l: not l.shopify_discount_line and not l.shopify_shipping_line
        )
        self.assertAlmostEqual(product_line.discount, 15.0, places=2)
        self.assertFalse(self.order.order_line.filtered("shopify_discount_line"))

    def test_fixed_amount_code_still_uses_dedicated_line(self):
        shopify_order = {
            "name": "#9101",
            "line_items": [
                {
                    "sku": "DISC-SKU",
                    "quantity": 2,
                    "price": "100.00",
                    "total_discount": "40.00",
                }
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
        self.engine._sync_line_discounts(
            self.order, shopify_order, self._log, on_create=True
        )
        product_line = self.order.order_line.filtered(
            lambda l: not l.shopify_discount_line and not l.shopify_shipping_line
        )
        disc_line = self.order.order_line.filtered("shopify_discount_line")
        self.assertEqual(product_line.discount, 0.0)
        self.assertEqual(len(disc_line), 1)
        self.assertEqual(disc_line.price_unit, -40.0)
        self.assertEqual(self.order.shopify_discount_codes, "TAKE40")

    def test_order_edit_line_map_includes_discount(self):
        edit = self.env["shopify.order.edit.engine"]
        mapped = edit._shopify_line_map(
            {
                "name": "#9101",
                "line_items": [
                    {
                        "sku": "DISC-SKU",
                        "title": "Discount Widget",
                        "quantity": 2,
                        "price": "100.00",
                        "total_discount": "40.00",
                    }
                ],
            },
            self._log,
        )
        self.assertEqual(mapped["DISC-SKU"]["discount"], 20.0)
        self.assertEqual(mapped["DISC-SKU"]["qty"], 2)

    def test_shopify_line_map_uses_current_quantity(self):
        edit = self.env["shopify.order.edit.engine"]
        mapped = edit._shopify_line_map(
            {
                "name": "#9101",
                "line_items": [
                    {
                        "sku": "DISC-SKU",
                        "quantity": 2,
                        "current_quantity": 0,
                        "price": "100.00",
                    }
                ],
            },
            self._log,
        )
        self.assertEqual(mapped["DISC-SKU"]["qty"], 0.0)

    def test_quantity_decrease_matches_current_quantity(self):
        shopify_order = {
            "name": "#9101",
            "line_items": [
                {
                    "sku": "DISC-SKU",
                    "quantity": 2,
                    "current_quantity": 0,
                    "price": "100.00",
                }
            ],
        }
        changed = self.env["shopify.order.edit.engine"]._sync_quantities_from_shopify(
            self.order, shopify_order, self._log
        )
        self.assertTrue(changed)
        product_line = self.order.order_line.filtered(
            lambda l: not l.shopify_discount_line and not l.shopify_shipping_line
        )
        self.assertEqual(product_line.product_uom_qty, 0.0)

    def test_quantity_decrease_is_idempotent(self):
        shopify_order = {
            "name": "#9101",
            "line_items": [
                {
                    "sku": "DISC-SKU",
                    "quantity": 2,
                    "current_quantity": 1,
                    "price": "100.00",
                }
            ],
        }
        edit = self.env["shopify.order.edit.engine"]
        edit._sync_quantities_from_shopify(self.order, shopify_order, self._log)
        edit._sync_quantities_from_shopify(self.order, shopify_order, self._log)
        product_line = self.order.order_line.filtered(
            lambda l: not l.shopify_discount_line and not l.shopify_shipping_line
        )
        self.assertEqual(product_line.product_uom_qty, 1.0)

    def _discount_jobs(self, line=None):
        line = line or self.order.order_line[0]
        jobs = self.env["shopify.sync.job"].sudo().search(
            [("job_type", "=", "order_discount_push")]
        )
        return jobs.filtered(
            lambda j: j.payload_dict().get("so_line_id") == line.id
        )

    def test_odoo_disc_percent_enqueues_push(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "shopify_order_ops.discount_sync_direction", "odoo_to_shopify"
        )
        self.order.order_line.write({"discount": 10.0})
        jobs = self._discount_jobs()
        self.assertEqual(len(jobs), 1)
        payload = jobs.payload_dict()
        self.assertEqual(payload.get("shopify_order_id"), "9101")
        self.assertEqual(payload.get("discount"), 10.0)

    def test_echo_guard_skips_discount_push(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "shopify_order_ops.discount_sync_direction", "odoo_to_shopify"
        )
        self.order.order_line.with_context(
            shopify_sync_origin="shopify"
        ).write({"discount": 15.0})
        self.assertFalse(self._discount_jobs())

    def test_shopify_to_odoo_direction_does_not_enqueue_push(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "shopify_order_ops.discount_sync_direction", "shopify_to_odoo"
        )
        self.order.order_line.write({"discount": 10.0})
        self.assertFalse(self._discount_jobs())

    def test_discount_push_job_adds_line_discount(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "shopify_order_ops.discount_sync_direction", "odoo_to_shopify"
        )
        captured = []

        def fake_graphql(self, query, variables=None):
            captured.append((query, variables))
            if "orderEditBegin" in (query or ""):
                return {
                    "orderEditBegin": {
                        "calculatedOrder": {
                            "id": "gid://shopify/CalculatedOrder/1",
                            "lineItems": {
                                "edges": [
                                    {
                                        "node": {
                                            "id": "gid://shopify/CalculatedLineItem/1",
                                            "quantity": 2,
                                            "sku": "DISC-SKU",
                                            "variant": {
                                                "id": "gid://shopify/ProductVariant/1"
                                            },
                                            "calculatedDiscountAllocations": [],
                                        }
                                    }
                                ]
                            },
                        },
                        "userErrors": [],
                    }
                }
            if "orderEditAddLineItemDiscount" in (query or ""):
                return {
                    "orderEditAddLineItemDiscount": {
                        "calculatedOrder": {
                            "id": "gid://shopify/CalculatedOrder/1"
                        },
                        "userErrors": [],
                    }
                }
            if "orderEditCommit" in (query or ""):
                return {"orderEditCommit": {"userErrors": []}}
            return {}

        shopify_order = {
            "id": 9101,
            "name": "#9101",
            "line_items": [
                {
                    "sku": "DISC-SKU",
                    "quantity": 2,
                    "price": "100.00",
                    "total_discount": "0.00",
                }
            ],
        }
        self.product.shopify_variant_id = "gid://shopify/ProductVariant/1"
        self.order.order_line.with_context(
            shopify_sync_origin="shopify"
        ).write({"discount": 10.0})
        job = (
            self.env["shopify.sync.job"]
            .sudo()
            .enqueue(
                name="order_discount_push 9101",
                job_type="order_discount_push",
                payload_dict={
                    "shopify_order_id": "9101",
                    "so_id": self.order.id,
                    "so_name": self.order.name,
                    "so_line_id": self.order.order_line.id,
                    "product_id": self.product.id,
                    "discount": 10.0,
                },
            )
        )
        with patch(
            "odoo.addons.shopify_order_ops.models.shopify_api.ShopifyApiClient.get_order",
            return_value=shopify_order,
        ), patch(
            "odoo.addons.shopify_order_ops.models.shopify_api.ShopifyApiClient.graphql",
            fake_graphql,
        ):
            self.env["shopify.order.update.engine"].process_order_discount_push(job)
        add_calls = [
            vars_ for query, vars_ in captured
            if "orderEditAddLineItemDiscount" in (query or "")
        ]
        self.assertTrue(add_calls)
        self.assertEqual(add_calls[0]["discount"]["percentValue"], 10.0)
        commit_calls = [
            query for query, _vars in captured if "orderEditCommit" in (query or "")
        ]
        self.assertTrue(commit_calls)

    def test_discount_push_adds_when_original_cannot_be_removed(self):
        """Checkout/manual discounts on the original order cannot be
        removed; the push must add a new line discount instead."""
        self.env["ir.config_parameter"].sudo().set_param(
            "shopify_order_ops.discount_sync_direction", "odoo_to_shopify"
        )
        captured = []

        def fake_graphql(self, query, variables=None):
            captured.append((query, variables))
            if "orderEditBegin" in (query or ""):
                return {
                    "orderEditBegin": {
                        "calculatedOrder": {
                            "id": "gid://shopify/CalculatedOrder/1",
                            "lineItems": {
                                "edges": [
                                    {
                                        "node": {
                                            "id": "gid://shopify/CalculatedLineItem/1",
                                            "quantity": 1,
                                            "sku": "DISC-SKU",
                                            "variant": {
                                                "id": "gid://shopify/ProductVariant/1"
                                            },
                                            "calculatedDiscountAllocations": [
                                                {
                                                    "discountApplication": {
                                                        "id": (
                                                            "gid://shopify/"
                                                            "CalculatedManualDiscount"
                                                            "Application/1"
                                                        )
                                                    }
                                                }
                                            ],
                                        }
                                    }
                                ]
                            },
                        },
                        "userErrors": [],
                    }
                }
            if "orderEditUpdateDiscount" in (query or ""):
                return {
                    "orderEditUpdateDiscount": {
                        "userErrors": [
                            {
                                "field": ["discountApplicationId"],
                                "message": (
                                    "This discount was applied to the order "
                                    "and can't be removed."
                                ),
                            }
                        ]
                    }
                }
            if "orderEditAddLineItemDiscount" in (query or ""):
                return {
                    "orderEditAddLineItemDiscount": {
                        "calculatedOrder": {
                            "id": "gid://shopify/CalculatedOrder/1"
                        },
                        "userErrors": [],
                    }
                }
            if "orderEditCommit" in (query or ""):
                return {"orderEditCommit": {"userErrors": []}}
            return {}

        shopify_order = {
            "id": 9101,
            "name": "#9101",
            "currency": "AUD",
            "line_items": [
                {
                    "sku": "DISC-SKU",
                    "quantity": 1,
                    "price": "24.31",
                    "total_discount": "0.00",
                }
            ],
        }
        self.product.shopify_variant_id = "gid://shopify/ProductVariant/1"
        self.order.order_line.with_context(
            shopify_sync_origin="shopify"
        ).write({"discount": 2.0})
        job = (
            self.env["shopify.sync.job"]
            .sudo()
            .enqueue(
                name="order_discount_push 9101 original",
                job_type="order_discount_push",
                payload_dict={
                    "shopify_order_id": "9101",
                    "so_id": self.order.id,
                    "so_name": self.order.name,
                    "so_line_id": self.order.order_line.id,
                    "product_id": self.product.id,
                    "discount": 2.0,
                },
            )
        )
        with patch(
            "odoo.addons.shopify_order_ops.models.shopify_api.ShopifyApiClient.get_order",
            return_value=shopify_order,
        ), patch(
            "odoo.addons.shopify_order_ops.models.shopify_api.ShopifyApiClient.graphql",
            fake_graphql,
        ):
            self.env["shopify.order.update.engine"].process_order_discount_push(job)
        queries = [query or "" for query, _vars in captured]
        self.assertFalse(
            any("orderEditRemoveDiscount" in q for q in queries),
            "must not try to remove an original-order discount",
        )
        add_calls = [
            vars_ for query, vars_ in captured
            if "orderEditAddLineItemDiscount" in (query or "")
        ]
        self.assertTrue(add_calls)
        self.assertEqual(add_calls[0]["discount"]["percentValue"], 2.0)
