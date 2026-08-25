from odoo import api, fields, models
from odoo.exceptions import UserError


class SaleOrder(models.Model):
    _inherit = "sale.order"

    shopify_order_id = fields.Char(
        string="Shopify Order ID",
        index=True,
        copy=False,
        help="Numeric Shopify Admin order id. Use this in automations "
        "(webhooks, Studio, IWS).",
    )
    shopify_order_name = fields.Char(
        string="Shopify Order Name",
        index=True,
        copy=False,
        help="Shopify order name, e.g. #14335.",
    )
    shopify_discount_codes = fields.Char(
        string="Shopify Discount Codes",
        copy=False,
        help="Coupon codes / automatic discount titles applied on the "
        "Shopify order. Use this in Studio / IWS automations.",
    )
    shopify_coupon_code = fields.Char(
        string="Shopify coupon code",
        copy=False,
        help="On a manual Odoo order, type a Shopify discount code to "
        "apply it. Automatic Shopify sales (e.g. Afterpay Day 15%) "
        "apply on their own while they are active.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        orders = super().create(vals_list)
        orders._apply_shopify_catalogue_discounts()
        return orders

    def write(self, vals):
        res = super().write(vals)
        if not self.env.context.get("shopify_applying_catalogue_discount"):
            self._apply_shopify_catalogue_discounts()
        return res

    def _apply_shopify_catalogue_discounts(self):
        """Shopify → Odoo catalogue: auto sales + typed codes on manual SOs."""
        if self.env.context.get("shopify_sync_origin") == "shopify":
            return
        if self.env.context.get("shopify_applying_catalogue_discount"):
            return
        if not self.env["shopify.discount.catalogue.sync"]._manual_apply_enabled():
            return
        Discount = self.env["shopify.discount"].sudo()
        for order in self:
            if order.shopify_order_id or order.shopify_order_name:
                continue
            Discount.apply_to_manual_order(order)

    @api.onchange("shopify_coupon_code")
    def _onchange_shopify_coupon_code(self):
        self._apply_shopify_catalogue_discounts()

    def action_shopify_sync_lines_from_shopify(self):
        """Pull current Shopify line items onto this order. Does not edit Shopify."""
        self.ensure_one()
        rest_id = self.env["shopify.order.pull.engine"].rest_order_id_from_sale_order(
            self
        )
        if not rest_id:
            raise UserError(
                "This order has no numeric Shopify order id. On orders "
                "pulled by the old connector, copy the number after the "
                "colon in Shopify Id into Shopify Order ID, then try again."
            )
        job = self.env["shopify.sync.job"].sudo().enqueue(
            "order_edit %s (manual)" % rest_id,
            "order_edit",
            {
                "order_id": rest_id,
                "topic": "manual",
                "raw": {},
            },
        )
        job._process_one()
        if job.state == "failed":
            raise UserError(
                "Could not pull Shopify lines onto this order:\n%s"
                % (job.error or "See Shopify Ops → Sync Logs.")
            )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Shopify line sync",
                "message": (
                    "Pulled the current Shopify lines onto this order. "
                    "Shopify was not changed."
                ),
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }


class ProductProduct(models.Model):
    _inherit = "product.product"

    def get_product_multiline_description_sale(self):
        """Quote/SO line names use display_name only, not description_sale.

        Standard Odoo appends description_sale (often Shopify body_html via
        another connector) onto sale.order.line.name, which dumps marketing copy onto
        quotations. Leave description_sale on the product for Odoo→Shopify
        body_html push.
        """
        return self.display_name


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    shopify_discount_line = fields.Boolean(
        string="Shopify Discount Line",
        copy=False,
        help="Negative line that carries the Shopify order discount.",
    )
    shopify_shipping_line = fields.Boolean(
        string="Shopify Shipping Line",
        copy=False,
        help="Line that carries the Shopify shipping charge.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        if not self.env.context.get("shopify_applying_catalogue_discount"):
            lines.mapped("order_id")._apply_shopify_catalogue_discounts()
        return lines

    def write(self, vals):
        res = super().write(vals)
        if self.env.context.get("shopify_applying_catalogue_discount"):
            return res
        if set(vals) & {
            "product_id",
            "product_uom_qty",
            "price_unit",
            "discount",
        }:
            self.mapped("order_id")._apply_shopify_catalogue_discounts()
        return res

    @api.onchange("product_id", "product_uom_qty", "price_unit")
    def _onchange_shopify_catalogue_discount(self):
        """Show catalogue Disc.% on the line before the quotation is saved."""
        if self.env.context.get("shopify_applying_catalogue_discount"):
            return
        for line in self:
            order = line.order_id
            if order:
                order._apply_shopify_catalogue_discounts()

    def _get_sale_order_line_multiline_description_sale(self):
        """Product display name plus variant extras; never description_sale.

        Section/note lines have no product_id and are skipped by _compute_name;
        still return the existing name if this hook is reached for them.
        """
        self.ensure_one()
        if self.display_type or not self.product_id:
            return self.name or ""
        description = super()._get_sale_order_line_multiline_description_sale()
        extra = self.product_id.description_sale
        if extra:
            suffix = "\n" + extra
            if description.endswith(suffix):
                description = description[: -len(suffix)]
        return description


class AccountMove(models.Model):
    _inherit = "account.move"

    shopify_order_id = fields.Char(string="Shopify Order ID", index=True, copy=False)
