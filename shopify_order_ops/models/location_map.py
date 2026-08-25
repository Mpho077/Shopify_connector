from odoo import fields, models


class ShopifyLocationMap(models.Model):
    """Maps a Shopify location to an Odoo stock location.

    Inventory pushes read these mappings to know which Odoo stock feeds which
    Shopify location. One mapping per Shopify location.
    """

    _name = "shopify.location.map"
    _description = "Shopify Location Mapping"
    _order = "shopify_location_name"

    name = fields.Char(compute="_compute_name", store=True)
    active = fields.Boolean(default=True)
    shopify_location_id = fields.Char(required=True, index=True)
    shopify_location_name = fields.Char(required=True)
    odoo_location_id = fields.Many2one(
        "stock.location",
        required=True,
        domain="[('usage', '=', 'internal')]",
        string="Odoo Location",
    )

    _sql_constraints = [
        (
            "shopify_location_uniq",
            "unique(shopify_location_id)",
            "A Shopify location can only be mapped once.",
        )
    ]

    def _compute_name(self):
        for rec in self:
            rec.name = "%s -> %s" % (
                rec.shopify_location_name or rec.shopify_location_id or "?",
                rec.odoo_location_id.display_name or "?",
            )
