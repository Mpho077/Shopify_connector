from unittest.mock import patch

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "shopify_order_ops")
class TestOrderShippingAddressSync(TransactionCase):
    def setUp(self):
        super().setUp()
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("shopify_order_ops.address_propagation_enabled", "True")
        icp.set_param("shopify_order_ops.address_sync_direction", "two_way")
        icp.set_param("shopify_order_ops.order_pull_enabled", "False")
        icp.set_param("shopify_order_ops.order_update_push_enabled", "False")

        self.partner = self.env["res.partner"].create({"name": "Addr Customer"})
        self.shipping_old = self.env["res.partner"].create(
            {
                "name": "Jane Old",
                "parent_id": self.partner.id,
                "type": "delivery",
                "street": "1 Old Street",
                "city": "Cape Town",
                "zip": "8000",
                "country_id": self.env.ref("base.za").id,
            }
        )
        self.invoice_old = self.env["res.partner"].create(
            {
                "name": "Jane Bill",
                "parent_id": self.partner.id,
                "type": "invoice",
                "street": "10 Invoice Way",
                "city": "Cape Town",
                "zip": "8000",
                "country_id": self.env.ref("base.za").id,
            }
        )
        product = self.env["product.product"].create(
            {
                "name": "Address Sync Service",
                "type": "service",
                "list_price": 1.0,
            }
        )
        self.order = self.env["sale.order"].create(
            {
                "partner_id": self.partner.id,
                "partner_shipping_id": self.shipping_old.id,
                "partner_invoice_id": self.invoice_old.id,
                "shopify_order_id": "9001",
                "shopify_order_name": "#9001",
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": product.id,
                            "product_uom_qty": 1,
                            "price_unit": 1.0,
                        },
                    )
                ],
            }
        )
        self.order.action_confirm()

    def _jobs(self, so=None):
        so = so or self.order
        jobs = self.env["shopify.sync.job"].sudo().search(
            [("job_type", "=", "order_address_push")]
        )
        return jobs.filtered(
            lambda j: j.payload_dict().get("so_id") == so.id
        )

    def test_settings_fields_on_config(self):
        settings = self.env["res.config.settings"].create({})
        self.assertTrue(
            hasattr(settings, "shopify_address_propagation_enabled")
        )
        self.assertTrue(hasattr(settings, "shopify_address_sync_direction"))
        self.assertIn(
            "two_way",
            dict(settings._fields["shopify_address_sync_direction"].selection),
        )

    def test_odoo_to_shopify_enqueues_on_shipping_partner_change(self):
        new_ship = self.env["res.partner"].create(
            {
                "name": "Jane New",
                "parent_id": self.partner.id,
                "type": "delivery",
                "street": "2 New Road",
                "city": "Johannesburg",
                "zip": "2000",
                "country_id": self.env.ref("base.za").id,
            }
        )
        self.order.write({"partner_shipping_id": new_ship.id})
        jobs = self._jobs()
        self.assertEqual(len(jobs), 1)
        payload = jobs.payload_dict()
        self.assertEqual(payload.get("shopify_order_id"), "9001")
        self.assertEqual(payload.get("partner_shipping_id"), new_ship.id)
        self.assertTrue(payload.get("push_shipping"))
        self.assertTrue(payload.get("push_billing"))

    def test_odoo_to_shopify_enqueues_on_invoice_partner_change(self):
        new_inv = self.env["res.partner"].create(
            {
                "name": "Jane New Bill",
                "parent_id": self.partner.id,
                "type": "invoice",
                "street": "20 New Invoice",
                "city": "Johannesburg",
                "zip": "2000",
                "country_id": self.env.ref("base.za").id,
            }
        )
        self.order.write({"partner_invoice_id": new_inv.id})
        jobs = self._jobs()
        self.assertEqual(len(jobs), 1)
        payload = jobs.payload_dict()
        self.assertEqual(payload.get("shopify_order_id"), "9001")
        self.assertEqual(payload.get("partner_invoice_id"), new_inv.id)
        self.assertTrue(payload.get("push_billing"))

    def test_echo_guard_skips_enqueue(self):
        new_ship = self.env["res.partner"].create(
            {
                "name": "Echo Ship",
                "parent_id": self.partner.id,
                "type": "delivery",
                "street": "3 Echo Ave",
                "city": "Durban",
                "zip": "4001",
            }
        )
        self.order.with_context(shopify_sync_origin="shopify").write(
            {"partner_shipping_id": new_ship.id}
        )
        self.assertFalse(self._jobs())

    def test_shopify_to_odoo_direction_does_not_enqueue(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "shopify_order_ops.address_sync_direction", "shopify_to_odoo"
        )
        new_ship = self.env["res.partner"].create(
            {
                "name": "One Way",
                "parent_id": self.partner.id,
                "type": "delivery",
                "street": "4 One Way",
                "city": "Pretoria",
                "zip": "0001",
            }
        )
        self.order.write({"partner_shipping_id": new_ship.id})
        self.assertFalse(self._jobs())

    def test_shopify_to_odoo_direction_does_not_enqueue_billing(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "shopify_order_ops.address_sync_direction", "shopify_to_odoo"
        )
        new_inv = self.env["res.partner"].create(
            {
                "name": "One Way Bill",
                "parent_id": self.partner.id,
                "type": "invoice",
                "street": "4 One Way Bill",
                "city": "Pretoria",
                "zip": "0001",
            }
        )
        self.order.write({"partner_invoice_id": new_inv.id})
        self.assertFalse(self._jobs())

    def test_disabled_master_switch_skips_enqueue(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "shopify_order_ops.address_propagation_enabled", "False"
        )
        new_ship = self.env["res.partner"].create(
            {
                "name": "Off Ship",
                "parent_id": self.partner.id,
                "type": "delivery",
                "street": "5 Off Lane",
                "city": "Cape Town",
                "zip": "8001",
            }
        )
        self.order.write({"partner_shipping_id": new_ship.id})
        self.assertFalse(self._jobs())

    def test_in_place_partner_edit_enqueues(self):
        self.shipping_old.write({"street": "1 Old Street Unit 2"})
        jobs = self._jobs()
        self.assertEqual(len(jobs), 1)

    def test_in_place_invoice_partner_edit_enqueues(self):
        self.invoice_old.write({"street": "10 Invoice Way Unit 2"})
        jobs = self._jobs()
        self.assertEqual(len(jobs), 1)

    def test_shipping_change_updates_open_picking(self):
        """Changing the SO delivery address must update open outgoing
        deliveries and dismiss core's 'update the partner' warning."""
        widget = self.env["product.product"].create(
            {"name": "Addr Sync Widget", "type": "consu", "list_price": 5.0}
        )
        order = self.env["sale.order"].create(
            {
                "partner_id": self.partner.id,
                "partner_shipping_id": self.shipping_old.id,
                "shopify_order_id": "9002",
                "shopify_order_name": "#9002",
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": widget.id,
                            "product_uom_qty": 1,
                            "price_unit": 5.0,
                        },
                    )
                ],
            }
        )
        order.action_confirm()
        pickings = order.picking_ids.filtered(
            lambda p: p.picking_type_code == "outgoing"
        )
        self.assertTrue(pickings, "confirming the SO should create a delivery")
        self.assertEqual(pickings.mapped("partner_id"), self.shipping_old)

        new_ship = self.env["res.partner"].create(
            {
                "name": "Picking Ship",
                "parent_id": self.partner.id,
                "type": "delivery",
                "street": "9 Picking Blvd",
                "city": "Ottawa",
                "zip": "K2P 2L8",
            }
        )
        order.write({"partner_shipping_id": new_ship.id})
        self.assertEqual(
            pickings.mapped("partner_id"),
            new_ship,
            "open delivery should follow the new SO shipping address",
        )
        stale = (
            self.env["mail.activity"]
            .sudo()
            .search(
                [
                    ("res_model", "=", "stock.picking"),
                    ("res_id", "in", pickings.ids),
                ]
            )
            .filtered(
                lambda a: "delivery address has been changed"
                in (a.note or "").lower()
            )
        )
        self.assertFalse(
            stale, "address-changed warning activity should be dismissed"
        )

    def test_shopify_to_odoo_updates_delivery_address(self):
        engine = self.env["shopify.order.pull.engine"]
        shopify_order = {
            "name": "#9001",
            "shipping_address": {
                "name": "Jane New",
                "address1": "99 Shopify Street",
                "address2": "",
                "city": "Cape Town",
                "zip": "8001",
                "country_code": "ZA",
                "province_code": "",
            },
        }
        engine._sync_shipping_address(self.order, shopify_order, lambda *a: None)
        self.assertEqual(
            self.order.partner_shipping_id.street, "99 Shopify Street"
        )
        self.assertEqual(self.order.partner_shipping_id.zip, "8001")
        self.assertFalse(self._jobs())  # echo guard on the Shopify-origin write

    def test_odoo_to_shopify_direction_skips_pull_sync(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "shopify_order_ops.address_sync_direction", "odoo_to_shopify"
        )
        engine = self.env["shopify.order.pull.engine"]
        shopify_order = {
            "name": "#9001",
            "shipping_address": {
                "name": "Should Not Apply",
                "address1": "77 Ignore Me",
                "city": "Cape Town",
                "zip": "8001",
                "country_code": "ZA",
            },
            "billing_address": {
                "name": "Should Not Apply Bill",
                "address1": "88 Ignore Billing",
                "city": "Cape Town",
                "zip": "8001",
                "country_code": "ZA",
            },
        }
        engine._sync_shipping_address(self.order, shopify_order, lambda *a: None)
        self.assertEqual(self.order.partner_shipping_id.street, "1 Old Street")
        engine._sync_billing_address(self.order, shopify_order, lambda *a: None)
        self.assertEqual(self.order.partner_invoice_id.street, "10 Invoice Way")

    def test_shopify_to_odoo_updates_invoice_address(self):
        engine = self.env["shopify.order.pull.engine"]
        shopify_order = {
            "name": "#9001",
            "billing_address": {
                "name": "Jane New Bill",
                "address1": "88 Shopify Billing",
                "address2": "",
                "city": "Cape Town",
                "zip": "8003",
                "country_code": "ZA",
                "province_code": "",
            },
        }
        engine._sync_billing_address(self.order, shopify_order, lambda *a: None)
        self.assertEqual(
            self.order.partner_invoice_id.street, "88 Shopify Billing"
        )
        self.assertEqual(self.order.partner_invoice_id.zip, "8003")
        self.assertEqual(self.order.partner_invoice_id.type, "invoice")
        self.assertEqual(self.order.partner_shipping_id, self.shipping_old)
        self.assertFalse(self._jobs())

    def test_webhook_address_sync_job(self):
        shopify_order = {
            "id": 9001,
            "name": "#9001",
            "shipping_address": {
                "name": "Jane Import",
                "address1": "12 Import Road",
                "city": "Cape Town",
                "zip": "8002",
                "country_code": "ZA",
            },
            "billing_address": {
                "name": "Jane Import Bill",
                "address1": "15 Import Billing",
                "city": "Cape Town",
                "zip": "8004",
                "country_code": "ZA",
            },
        }
        job = (
            self.env["shopify.sync.job"]
            .sudo()
            .enqueue(
                name="order_address_sync 9001",
                job_type="order_address_sync",
                payload_dict={"order_id": 9001, "topic": "orders/updated"},
            )
        )
        with patch(
            "odoo.addons.shopify_order_ops.models.shopify_api.ShopifyApiClient.get_order",
            return_value=shopify_order,
        ):
            self.env["shopify.order.pull.engine"].process_order_address_sync(job)
        self.assertEqual(
            self.order.partner_shipping_id.street, "12 Import Road"
        )
        self.assertEqual(
            self.order.partner_invoice_id.street, "15 Import Billing"
        )

    def test_tag_payload_selects_tag_job_only(self):
        engine = self.env["shopify.order.pull.engine"]
        payload = {
            "id": 9001,
            "name": "#9001",
            "tags": "PICKUP_IN_STORE",
            "shipping_address": {
                "address1": "1 Old Street",
                "city": "Cape Town",
                "zip": "8000",
                "country_code": "ZA",
            },
            "billing_address": {
                "address1": "10 Invoice Way",
                "city": "Cape Town",
                "zip": "8000",
                "country_code": "ZA",
            },
        }
        types = engine.updated_order_job_types(self.order, payload)
        self.assertEqual(types, ["order_tag_sync"])
        self.assertNotIn("order_address_sync", types)
        self.assertNotIn("order_charge_sync", types)

    def test_matching_order_does_not_select_update_jobs(self):
        engine = self.env["shopify.order.pull.engine"]
        payload = {
            "id": 9001,
            "name": "#9001",
            "tags": "",
            "shipping_address": {
                "address1": "1 Old Street",
                "city": "Cape Town",
                "zip": "8000",
                "country_code": "ZA",
            },
            "billing_address": {
                "address1": "10 Invoice Way",
                "city": "Cape Town",
                "zip": "8000",
                "country_code": "ZA",
            },
        }
        self.assertEqual(engine.updated_order_job_types(self.order, payload), [])

    def test_cutoff_skips_enqueue_for_old_shopify_orders(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "shopify_order_ops.order_sync_after", "2026-08-19 18:00:00"
        )
        engine = self.env["shopify.order.pull.engine"]
        created = engine.enqueue_split_order_updates(
            9001,
            "orders/updated",
            order={
                "id": 9001,
                "name": "#9001",
                "created_at": "2026-01-01T10:00:00Z",
                "tags": "PICKUP_IN_STORE",
            },
        )
        self.assertFalse(created)

    def test_existing_so_updates_when_pull_off(self):
        """Legacy order_pull path still syncs address when pull is off."""
        shopify_order = {
            "id": 9001,
            "name": "#9001",
            "shipping_address": {
                "name": "Jane Import",
                "address1": "12 Import Road",
                "city": "Cape Town",
                "zip": "8002",
                "country_code": "ZA",
            },
            "billing_address": {
                "name": "Jane Import Bill",
                "address1": "15 Import Billing",
                "city": "Cape Town",
                "zip": "8004",
                "country_code": "ZA",
            },
        }
        job = (
            self.env["shopify.sync.job"]
            .sudo()
            .enqueue(
                name="order_pull 9001",
                job_type="order_pull",
                payload_dict={"order_id": 9001, "topic": "orders/updated"},
            )
        )
        with patch(
            "odoo.addons.shopify_order_ops.models.shopify_api.ShopifyApiClient.get_order",
            return_value=shopify_order,
        ):
            self.env["shopify.order.pull.engine"].process_order_pull(job)
        self.assertEqual(
            self.order.partner_shipping_id.street, "12 Import Road"
        )
        self.assertEqual(
            self.order.partner_invoice_id.street, "15 Import Billing"
        )

    def test_address_push_job_calls_order_update(self):
        captured = {}

        def fake_graphql(self, query, variables=None):
            captured["query"] = query
            captured["variables"] = variables
            return {
                "orderUpdate": {
                    "order": {"id": "gid://shopify/Order/9001"},
                    "userErrors": [],
                }
            }

        job = (
            self.env["shopify.sync.job"]
            .sudo()
            .enqueue(
                name="order_address_push #9001",
                job_type="order_address_push",
                payload_dict={
                    "shopify_order_id": "9001",
                    "so_id": self.order.id,
                    "so_name": self.order.name,
                    "partner_shipping_id": self.shipping_old.id,
                    "push_shipping": True,
                    "push_billing": False,
                },
            )
        )
        with patch(
            "odoo.addons.shopify_order_ops.models.shopify_api.ShopifyApiClient.graphql",
            fake_graphql,
        ), patch(
            "odoo.addons.shopify_order_ops.models.shopify_api.ShopifyApiClient.put",
        ) as fake_put:
            self.env["shopify.order.update.engine"].process_order_address_push(
                job
            )
        fake_put.assert_not_called()
        self.assertIn("orderUpdate", captured.get("query") or "")
        shipping = (captured.get("variables") or {}).get("input", {}).get(
            "shippingAddress"
        )
        self.assertEqual(shipping.get("address1"), "1 Old Street")
        self.assertEqual(shipping.get("city"), "Cape Town")
        self.assertEqual(shipping.get("countryCode"), "ZA")
        self.assertEqual(
            captured["variables"]["input"]["id"], "gid://shopify/Order/9001"
        )

    def test_partner_to_shopify_shipping_mapping(self):
        engine = self.env["shopify.order.update.engine"]
        mapped = engine._partner_to_shopify_shipping_address(self.shipping_old)
        self.assertEqual(mapped["firstName"], "Jane")
        self.assertEqual(mapped["lastName"], "Old")
        self.assertEqual(mapped["address1"], "1 Old Street")
        self.assertEqual(mapped["zip"], "8000")
        self.assertEqual(mapped["countryCode"], "ZA")

    def test_address_push_job_does_not_put_billing(self):
        """Shopify ignores billing updates on existing orders; do not PUT."""
        job = (
            self.env["shopify.sync.job"]
            .sudo()
            .enqueue(
                name="order_address_push billing #9001",
                job_type="order_address_push",
                payload_dict={
                    "shopify_order_id": "9001",
                    "so_id": self.order.id,
                    "so_name": self.order.name,
                    "partner_invoice_id": self.invoice_old.id,
                    "push_shipping": False,
                    "push_billing": True,
                },
            )
        )
        with patch(
            "odoo.addons.shopify_order_ops.models.shopify_api.ShopifyApiClient.put",
        ) as fake_put, patch(
            "odoo.addons.shopify_order_ops.models.shopify_api.ShopifyApiClient.graphql",
        ) as fake_graphql:
            self.env["shopify.order.update.engine"].process_order_address_push(
                job
            )
        fake_put.assert_not_called()
        fake_graphql.assert_not_called()
        logs = self.env["shopify.sync.log"].search([("job_id", "=", job.id)])
        self.assertTrue(
            any(
                rec.level == "warning" and "Billing address was not sent" in rec.message
                for rec in logs
            ),
            logs.mapped("message"),
        )

    def test_partner_to_shopify_rest_billing_mapping(self):
        engine = self.env["shopify.order.update.engine"]
        mapped = engine._partner_to_shopify_rest_address(self.invoice_old)
        self.assertEqual(mapped["first_name"], "Jane")
        self.assertEqual(mapped["last_name"], "Bill")
        self.assertEqual(mapped["address1"], "10 Invoice Way")
        self.assertEqual(mapped["zip"], "8000")
        self.assertEqual(mapped["country_code"], "ZA")
