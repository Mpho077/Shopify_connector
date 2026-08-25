from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "shopify_order_ops")
class TestOrderEditFromUpdatedWebhook(TransactionCase):
    def setUp(self):
        super().setUp()
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("shopify_order_ops.include_order_tags", "False")
        icp.set_param("shopify_order_ops.address_propagation_enabled", "False")
        icp.set_param("shopify_order_ops.discount_sync_enabled", "False")
        icp.set_param("shopify_order_ops.shipping_charge_sync_enabled", "False")
        icp.set_param("shopify_order_ops.order_edit_enabled", "True")
        self.partner = self.env["res.partner"].create({"name": "Edit Split Customer"})
        self.bath_1700 = self.env["product.product"].create(
            {
                "name": "1700 Bath",
                "default_code": "BATH1700",
                "type": "consu",
                "list_price": 1700.0,
            }
        )
        self.bath_1500 = self.env["product.product"].create(
            {
                "name": "1500 Bath",
                "default_code": "BATH1500",
                "type": "consu",
                "list_price": 1500.0,
            }
        )
        self.order = self.env["sale.order"].create(
            {
                "partner_id": self.partner.id,
                "shopify_order_id": "13899",
                "shopify_order_name": "#13899",
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": self.bath_1700.id,
                            "product_uom_qty": 1,
                            "price_unit": 1700.0,
                        },
                    )
                ],
            }
        )
        self.engine = self.env["shopify.order.pull.engine"]

    def test_swap_enqueues_order_edit_not_only_qty(self):
        payload = {
            "id": 13899,
            "name": "#13899",
            "line_items": [
                {
                    "sku": "BATH1700",
                    "quantity": 1,
                    "current_quantity": 0,
                    "price": "1700.00",
                    "title": "1700 Bath",
                },
                {
                    "sku": "BATH1500",
                    "quantity": 1,
                    "current_quantity": 1,
                    "price": "1500.00",
                    "title": "1500 Bath",
                },
            ],
        }
        types = self.engine.updated_order_job_types(self.order, payload)
        self.assertIn("order_edit", types)
        self.assertNotIn("order_qty_sync", types)

    def test_remove_only_still_uses_qty_sync(self):
        payload = {
            "id": 13899,
            "name": "#13899",
            "line_items": [
                {
                    "sku": "BATH1700",
                    "quantity": 1,
                    "current_quantity": 0,
                    "price": "1700.00",
                    "title": "1700 Bath",
                },
            ],
        }
        types = self.engine.updated_order_job_types(self.order, payload)
        # Removals use order_edit (same path as additions) so swaps cannot
        # enqueue qty-only and miss the new line.
        self.assertEqual(types, ["order_edit"])

    def test_manual_sync_lines_runs_order_edit_without_shopify_edit(self):
        empty = self.env["sale.order"].create({"partner_id": self.partner.id})
        with self.assertRaises(UserError):
            empty.action_shopify_sync_lines_from_shopify()

        with patch.object(
            type(self.env["shopify.order.edit.engine"]),
            "process_order_edit",
            lambda engine, job: None,
        ):
            result = self.order.action_shopify_sync_lines_from_shopify()

        job = self.env["shopify.sync.job"].search(
            [("job_type", "=", "order_edit")], limit=1, order="id desc"
        )
        self.assertTrue(job)
        self.assertEqual(job.state, "done")
        payload = job.payload_dict()
        self.assertEqual(str(payload.get("order_id")), "13899")
        self.assertEqual(payload.get("topic"), "manual")
        self.assertEqual(result["tag"], "display_notification")

    def test_synco_composite_id_is_used_without_our_shopify_order_id(self):
        from odoo.addons.shopify_order_ops.models.order_pull_engine import (
            parse_shopify_rest_order_id,
        )

        composite = (
            "example-shop.myshopify.com:7234616819861"
        )
        self.assertEqual(parse_shopify_rest_order_id(composite), "7234616819861")
        self.order.shopify_order_id = composite
        with patch.object(
            type(self.env["shopify.order.edit.engine"]),
            "process_order_edit",
            lambda engine, job: None,
        ):
            self.order.action_shopify_sync_lines_from_shopify()
        job = self.env["shopify.sync.job"].search(
            [("job_type", "=", "order_edit")], limit=1, order="id desc"
        )
        self.assertEqual(str(job.payload_dict().get("order_id")), "7234616819861")

    def test_find_existing_order_by_customer_reference(self):
        synco = self.env["sale.order"].create(
            {
                "partner_id": self.partner.id,
                "client_order_ref": "#13900",
            }
        )
        found = self.engine._find_existing_order("7234616819862", "#13900")
        self.assertEqual(found, synco)

    def test_process_now_retries_failed_job(self):
        job = self.env["shopify.sync.job"].create(
            {
                "name": "order_edit 13899",
                "job_type": "order_edit",
                "state": "failed",
                "attempts": 5,
                "payload": '{"order_id": "13899", "topic": "manual", "raw": {}}',
            }
        )
        with patch.object(
            type(self.env["shopify.order.edit.engine"]),
            "process_order_edit",
            lambda engine, job: None,
        ):
            job.action_process_now()
        self.assertEqual(job.state, "done")
        self.assertEqual(job.attempts, 1)

    def test_order_edit_skips_catalog_match_for_sku_already_on_so(self):
        """Duplicate catalog SKUs must not block adding a new Shopify line."""
        pem_on_order = self.env["product.product"].create(
            {
                "name": "PEM 900 on order",
                "default_code": "PEM900A",
                "type": "consu",
                "list_price": 100.0,
            }
        )
        self.env["product.product"].create(
            {
                "name": "PEM 900 duplicate",
                "default_code": "PEM900A",
                "type": "consu",
                "list_price": 100.0,
            }
        )
        self.order.write(
            {
                "client_order_ref": "#13899",
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": pem_on_order.id,
                            "product_uom_qty": 1,
                            "price_unit": 100.0,
                        },
                    )
                ],
            }
        )
        shopify_order = {
            "id": 7234616819861,
            "name": "#13899",
            "financial_status": "paid",
            "currency": self.order.currency_id.name,
            "line_items": [
                {
                    "sku": "PEM900A",
                    "quantity": 1,
                    "current_quantity": 1,
                    "price": "100.00",
                    "title": "PEM 900",
                },
                {
                    "sku": "BATH1500",
                    "quantity": 1,
                    "current_quantity": 1,
                    "price": "1500.00",
                    "title": "1500 Bath",
                },
            ],
        }
        job = self.env["shopify.sync.job"].create(
            {
                "name": "order_edit 13899",
                "job_type": "order_edit",
                "payload": (
                    '{"order_id": "7234616819861", "topic": "manual", "raw": {}}'
                ),
            }
        )
        with patch(
            "odoo.addons.shopify_order_ops.models.shopify_api.ShopifyApiClient.get_order",
            return_value=shopify_order,
        ):
            self.env["shopify.order.edit.engine"].process_order_edit(job)
        skus = self.order.order_line.mapped("product_id.default_code")
        self.assertIn("BATH1500", skus)
        self.assertEqual(skus.count("PEM900A"), 1)
