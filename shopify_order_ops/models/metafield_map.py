from odoo import api, fields, models


class ShopifyMetafieldMap(models.Model):
    """Configuration: which Shopify metafield maps to which Odoo field."""

    _name = "shopify.metafield.map"
    _description = "Shopify Metafield Mapping"
    _order = "applies_to, shopify_namespace, shopify_key"

    name = fields.Char(compute="_compute_name", store=True)
    active = fields.Boolean(default=True)
    applies_to = fields.Selection(
        [("product", "Product"), ("customer", "Customer"), ("order", "Sale Order")],
        required=True,
        default="product",
    )
    direction = fields.Selection(
        [
            ("shopify_to_odoo", "Shopify -> Odoo"),
            ("odoo_to_shopify", "Odoo -> Shopify"),
        ],
        required=True,
        default="shopify_to_odoo",
    )
    shopify_namespace = fields.Char(required=True, default="custom")
    shopify_key = fields.Char(required=True)
    shopify_type = fields.Char(
        required=True,
        default="single_line_text_field",
        help="Shopify metafield type, used when writing values to Shopify",
    )
    odoo_model = fields.Selection(
        [
            ("product.product", "Product (product.product)"),
            ("res.partner", "Customer (res.partner)"),
            ("sale.order", "Sale Order (sale.order)"),
        ],
        required=True,
        default="product.product",
    )
    odoo_field = fields.Char(
        required=True,
        help="Technical field name on the Odoo model, e.g. x_finish",
    )

    @api.depends("applies_to", "shopify_namespace", "shopify_key", "odoo_field")
    def _compute_name(self):
        for rec in self:
            rec.name = (
                f"{rec.applies_to or '?'}: "
                f"{rec.shopify_namespace or ''}.{rec.shopify_key or ''}"
                f" -> {rec.odoo_field or ''}"
            )
