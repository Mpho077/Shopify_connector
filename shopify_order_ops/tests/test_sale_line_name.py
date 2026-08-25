from odoo.tests import TransactionCase, tagged

SALES_COPY = "\n".join(
    ["<p>Ovia Zurich Extra Height bath — premium marketing copy.</p>"]
    + ["Technical specification dump line %s." % i for i in range(1, 40)]
)


@tagged("post_install", "-at_install", "shopify_order_ops")
class TestSaleLineName(TransactionCase):
    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param(
            "shopify_order_ops.skip_product_create", "False"
        )
        self.partner = self.env["res.partner"].create(
            {"name": "Quote Line Customer"}
        )
        self.product = self.env["product.product"].create(
            {
                "name": "Ovia Zurich Extra Height",
                "default_code": "OVIZURBTW",
                "type": "service",
                "list_price": 499.0,
                "description_sale": SALES_COPY,
            }
        )
        self.engine = self.env["shopify.order.pull.engine"]

    def _log(self, _level, _message):
        return None

    def _manual_order_vals(self, **extra):
        vals = {"partner_id": self.partner.id}
        vals.update(extra)
        return vals

    def test_product_hook_omits_description_sale(self):
        name = self.product.get_product_multiline_description_sale()
        self.assertEqual(name, self.product.display_name)
        self.assertNotIn("premium marketing copy", name)
        self.assertNotIn("Technical specification dump", name)
        self.assertEqual(self.product.description_sale, SALES_COPY)

    def test_manual_quote_line_stays_short(self):
        order = self.env["sale.order"].create(
            self._manual_order_vals(
                order_line=[
                    (
                        0,
                        0,
                        {
                            "product_id": self.product.id,
                            "product_uom_qty": 1,
                            "price_unit": 499.0,
                        },
                    )
                ]
            )
        )
        line = order.order_line
        self.assertEqual(len(line), 1)
        self.assertEqual(line.name, self.product.display_name)
        self.assertNotIn("premium marketing copy", line.name)
        self.assertNotIn("Technical specification dump", line.name)
        self.assertEqual(self.product.description_sale, SALES_COPY)

    def test_section_and_note_lines_keep_custom_name(self):
        order = self.env["sale.order"].create(self._manual_order_vals())
        section = self.env["sale.order.line"].create(
            {
                "order_id": order.id,
                "display_type": "line_section",
                "name": "Bathroom suite",
            }
        )
        note = self.env["sale.order.line"].create(
            {
                "order_id": order.id,
                "display_type": "line_note",
                "name": "Customer wants extra height.",
            }
        )
        self.assertEqual(section.name, "Bathroom suite")
        self.assertEqual(note.name, "Customer wants extra height.")

    def test_shopify_pull_keeps_short_line_title(self):
        so = self.engine._create_sale_order(
            {
                "name": "#9401",
                "line_items": [
                    {
                        "sku": "OVIZURBTW",
                        "title": "Ovia Zurich Extra Height",
                        "quantity": 1,
                        "price": "499.00",
                    }
                ],
            },
            "9401",
            self.partner,
            self._log,
        )
        line = so.order_line.filtered(lambda l: l.product_id == self.product)
        self.assertEqual(len(line), 1)
        self.assertEqual(line.name, "Ovia Zurich Extra Height")
        self.assertNotIn("premium marketing copy", line.name)
        self.assertEqual(self.product.description_sale, SALES_COPY)
