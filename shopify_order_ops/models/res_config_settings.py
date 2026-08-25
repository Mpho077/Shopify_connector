import logging

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

PREFIX = "shopify_order_ops."
_DISCOUNT_DIRECTIONS = ("shopify_to_odoo", "odoo_to_shopify")


def _coerce_discount_direction(value):
    """Map legacy stored values (two_way) onto a valid Selection key."""
    if value in _DISCOUNT_DIRECTIONS:
        return value
    if value in (False, None, ""):
        return value
    return "shopify_to_odoo"


def _parse_saved_datetime(raw):
    """ir.config_parameter string -> naive UTC datetime, or False."""
    text = (raw or "").strip()
    if not text:
        return False
    text = text.replace("T", " ").replace("Z", "")
    if len(text) == 10:
        text = text + " 00:00:00"
    try:
        return fields.Datetime.to_datetime(text)
    except (TypeError, ValueError):
        return False


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    shopify_shop_domain = fields.Char(
        string="Shop Domain",
        config_parameter=PREFIX + "shop_domain",
        help="e.g. my-store.myshopify.com",
    )
    shopify_access_token = fields.Char(
        string="Admin API Access Token",
        config_parameter=PREFIX + "access_token",
    )
    shopify_webhook_secret = fields.Char(
        string="Webhook Signing Secret",
        config_parameter=PREFIX + "webhook_secret",
    )
    shopify_location_id = fields.Char(
        string="Shopify Location ID",
        config_parameter=PREFIX + "location_id",
        help="Numeric Shopify location that inventory pushes target",
    )
    shopify_order_match_field = fields.Selection(
        [
            ("client_order_ref", "Customer Reference (client_order_ref)"),
            ("name", "Order Number (name)"),
            ("shopify_order_id", "Shopify Order ID field"),
        ],
        default="client_order_ref",
        config_parameter=PREFIX + "order_match_field",
    )
    shopify_auto_mark_paid = fields.Boolean(
        string="Auto re-mark invoice paid",
        default=True,
        config_parameter=PREFIX + "auto_mark_paid",
        help="After adding lines, re-apply payments when the Shopify order is fully paid",
    )
    shopify_payment_journal_id = fields.Many2one(
        "account.journal",
        string="Extra Payment Journal",
        domain="[('type', 'in', ('bank', 'cash'))]",
        config_parameter=PREFIX + "payment_journal_id",
        help="Journal used to register payment for the added amount when Shopify shows it collected",
    )
    shopify_inventory_sync_enabled = fields.Boolean(
        string="Inventory push",
        config_parameter=PREFIX + "inventory_sync_enabled",
    )
    shopify_fulfillment_sync_enabled = fields.Boolean(
        string="Fulfillment sync",
        config_parameter=PREFIX + "fulfillment_sync_enabled",
    )
    shopify_fulfillment_sync_direction = fields.Selection(
        [
            ("odoo_to_shopify", "Odoo -> Shopify"),
            ("shopify_to_odoo", "Shopify -> Odoo"),
            ("two_way", "Two-way"),
        ],
        default="odoo_to_shopify",
        config_parameter=PREFIX + "fulfillment_sync_direction",
        help="Odoo -> Shopify: done deliveries create Shopify fulfillments. "
        "Shopify -> Odoo: fulfillments in Shopify validate Odoo deliveries "
        "and copy tracking. Two-way runs both.",
    )
    shopify_fulfillment_notify_customer = fields.Boolean(
        string="Email customer on fulfillment",
        default=False,
        config_parameter=PREFIX + "fulfillment_notify_customer",
        help="When ON, Shopify sends the customer its shipping confirmation "
        "email (with tracking) for each pushed fulfillment and tracking "
        "update. Default OFF — fulfillments are silent.",
    )
    shopify_customer_sync_enabled = fields.Boolean(
        string="Customer sync",
        config_parameter=PREFIX + "customer_sync_enabled",
    )
    shopify_customer_sync_direction = fields.Selection(
        [
            ("shopify_to_odoo", "Shopify -> Odoo"),
            ("odoo_to_shopify", "Odoo -> Shopify"),
            ("two_way", "Two-way (last change wins)"),
        ],
        default="shopify_to_odoo",
        config_parameter=PREFIX + "customer_sync_direction",
    )
    shopify_product_sync_enabled = fields.Boolean(
        string="Product sync",
        config_parameter=PREFIX + "product_sync_enabled",
    )
    shopify_product_import_since = fields.Datetime(
        string="Import products updated since",
        help="Rewind the product pull watermark to this date and time and "
        "sync one batch. Re-run for more batches.",
    )
    shopify_product_sync_direction = fields.Selection(
        [
            ("shopify_to_odoo", "Shopify -> Odoo"),
            ("odoo_to_shopify", "Odoo -> Shopify"),
            ("two_way", "Two-way (last change wins)"),
        ],
        default="shopify_to_odoo",
        config_parameter=PREFIX + "product_sync_direction",
    )
    shopify_price_sync_enabled = fields.Boolean(
        string="Price sync",
        config_parameter=PREFIX + "price_sync_enabled",
    )
    shopify_price_sync_direction = fields.Selection(
        [
            ("odoo_to_shopify", "Odoo -> Shopify (ERP is price master)"),
            ("shopify_to_odoo", "Shopify -> Odoo"),
            ("two_way", "Two-way (last change wins)"),
        ],
        default="odoo_to_shopify",
        config_parameter=PREFIX + "price_sync_direction",
    )
    shopify_compare_at_sync_enabled = fields.Boolean(
        string="Sync compare-at (RRP) prices",
        default=True,
        config_parameter=PREFIX + "compare_at_sync_enabled",
        help="Keeps the Shopify compare-at price (RRP) in sync in the same "
        "direction as the price sync: Shopify -> Odoo fills the "
        "'Shopify Compare-at (RRP)' field, Odoo -> Shopify publishes it.",
    )
    shopify_metafield_sync_enabled = fields.Boolean(
        string="Metafield sync",
        config_parameter=PREFIX + "metafield_sync_enabled",
    )
    shopify_sync_from = fields.Char(
        string="Sync from",
        config_parameter=PREFIX + "sync_from",
        help="Only records changed after this date/time are synced. "
        "Records already linked to Shopify always keep syncing. "
        "Use 'Start syncing from now' to stop backlog processing.",
    )

    def action_set_sync_from_now(self):
        stamp = fields.Datetime.now().isoformat()
        self.env["ir.config_parameter"].sudo().set_param(
            PREFIX + "sync_from", stamp
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Sync scope",
                "message": "Syncs now only process records changed after %s."
                % stamp,
                "type": "success",
                "sticky": False,
            },
        }
    shopify_order_pull_enabled = fields.Boolean(
        string="Order pull",
        config_parameter=PREFIX + "order_pull_enabled",
    )
    shopify_order_pull_invoice = fields.Boolean(
        string="Create + post invoice on pull",
        default=True,
        config_parameter=PREFIX + "order_pull_invoice",
    )
    shopify_order_pull_auto_paid = fields.Boolean(
        string="Register payment when Shopify order is paid",
        default=True,
        config_parameter=PREFIX + "order_pull_auto_paid",
    )
    shopify_order_import_since = fields.Datetime(
        string="Import orders created since",
        help="Saved. Shopify orders created at or after this date and time "
        "(your timezone) are enqueued by Import historical orders. "
        "Stored as a string because config_parameter does not persist "
        "Datetime fields.",
    )

    # --- Order numbering ---------------------------------------------------
    shopify_use_shopify_order_numbers = fields.Boolean(
        string="Use Shopify order numbers in Odoo",
        default=True,
        config_parameter=PREFIX + "use_shopify_order_numbers",
        help="Shopify orders like #1011 become Odoo sale orders named #1011",
    )
    shopify_order_prefix = fields.Char(
        string="Order prefix",
        default="#",
        config_parameter=PREFIX + "order_prefix",
        help="Prefix applied to the Shopify order number (e.g. '#' or 'WEB-'). Applies to new records only.",
    )

    # --- Cancellation & refunds --------------------------------------------
    shopify_order_edit_enabled = fields.Boolean(
        string="Order edit sync (invoice re-post)",
        default=True,
        config_parameter=PREFIX + "order_edit_enabled",
        help="When off, orders/edited webhooks are acknowledged but no "
        "invoices are touched.",
    )
    shopify_cancel_shopify_to_odoo_enabled = fields.Boolean(
        string="Cancel in Odoo when cancelled in Shopify",
        default=True,
        config_parameter=PREFIX + "cancel_shopify_to_odoo_enabled",
        help="Shopify → Odoo. Leave on for orders cancelled in Shopify.",
    )
    shopify_cancel_odoo_to_shopify_enabled = fields.Boolean(
        string="Cancel in Shopify when cancelled in Odoo",
        default=False,
        config_parameter=PREFIX + "cancel_odoo_to_shopify_enabled",
        help="Odoo → Shopify. Leave off if warehouse change automations "
        "cancel and reconfirm orders in Odoo.",
    )
    shopify_order_update_push_enabled = fields.Boolean(
        string="Push order line additions to Shopify",
        default=False,
        config_parameter=PREFIX + "order_update_push_enabled",
        help="Adding a product line on a Shopify-linked sale order in Odoo adds it to the Shopify order (GraphQL order editing)",
    )
    shopify_refund_sync_enabled = fields.Boolean(
        string="Sync refunds (credit notes + restock)",
        default=False,
        config_parameter=PREFIX + "refund_sync_enabled",
    )

    # --- Order filters -------------------------------------------------------
    shopify_order_payment_status_filter = fields.Char(
        string="Payment statuses",
        default="paid,partially_paid",
        config_parameter=PREFIX + "order_payment_status_filter",
        help="Comma-separated Shopify financial statuses to pull (e.g. paid,partially_paid,authorized,pending)",
    )
    shopify_order_fulfillment_status_filter = fields.Char(
        string="Fulfillment statuses",
        config_parameter=PREFIX + "order_fulfillment_status_filter",
        help="Comma-separated fulfillment statuses to pull. Empty = all.",
    )
    shopify_order_source_filter = fields.Char(
        string="Order sources",
        config_parameter=PREFIX + "order_source_filter",
        help="Comma-separated order sources to pull (web, pos, draft_order, iphone, android...). Empty = all.",
    )
    shopify_order_date_range_enabled = fields.Boolean(
        string="Only sync within a date range",
        config_parameter=PREFIX + "order_date_range_enabled",
    )
    shopify_order_sync_date_from = fields.Char(
        string="Sync orders created from",
        config_parameter=PREFIX + "order_sync_date_from",
        help="ISO date, e.g. 2026-08-01 (char field: config_parameter does not support date type)",
    )
    shopify_order_sync_date_to = fields.Char(
        string="Sync orders created until",
        config_parameter=PREFIX + "order_sync_date_to",
        help="ISO date, e.g. 2026-08-31",
    )
    shopify_include_order_note = fields.Boolean(
        string="Include order note",
        default=True,
        config_parameter=PREFIX + "include_order_note",
    )
    shopify_include_order_tags = fields.Boolean(
        string="Include order tags",
        default=True,
        config_parameter=PREFIX + "include_order_tags",
        help="Copy Shopify order tags onto the sale order Tags field.",
    )
    shopify_single_customer_mode = fields.Boolean(
        string="Map orders to a single Odoo customer",
        config_parameter=PREFIX + "single_customer_mode",
    )
    shopify_single_customer_scope = fields.Selection(
        [("guest", "Guest orders only"), ("all", "All orders")],
        default="guest",
        config_parameter=PREFIX + "single_customer_scope",
    )
    shopify_single_customer_email = fields.Char(
        string="Single customer email",
        config_parameter=PREFIX + "single_customer_email",
        help="Orders are linked to the partner with this email",
    )
    shopify_skip_product_create = fields.Boolean(
        string="Skip product create",
        config_parameter=PREFIX + "skip_product_create",
        help="Order lines are built from Shopify data (title, SKU) without requiring an Odoo product",
    )
    shopify_address_propagation_enabled = fields.Boolean(
        string="Order address sync",
        default=True,
        config_parameter=PREFIX + "address_propagation_enabled",
        help="Sync Shopify order shipping with the Odoo delivery address "
        "(both ways) and billing with the sale order Invoice Address "
        "(Shopify → Odoo only — Shopify cannot update billing on an "
        "existing order). Does not sync the customer form address.",
    )
    shopify_address_sync_direction = fields.Selection(
        [
            ("shopify_to_odoo", "Shopify -> Odoo"),
            ("odoo_to_shopify", "Odoo -> Shopify"),
            ("two_way", "Two-way (last change wins)"),
        ],
        default="two_way",
        config_parameter=PREFIX + "address_sync_direction",
        help="Which direction the order delivery / shipping address should "
        "sync. Billing is always Shopify → Odoo.",
    )
    shopify_discount_sync_enabled = fields.Boolean(
        string="Sync order discounts",
        default=True,
        config_parameter=PREFIX + "discount_sync_enabled",
        help="Percentage coupons (Shopify → Odoo) land as Disc.% on the "
        "product line. Cart-wide fixed-amount codes use a dedicated "
        "SHOPIFY-DISCOUNT sale line. Draft invoices are updated; posted "
        "invoices are reset and re-posted like order-edit. Typing Disc.% in "
        "Odoo does not push to Shopify.",
    )
    shopify_discount_sync_direction = fields.Selection(
        [
            ("shopify_to_odoo", "Shopify -> Odoo"),
            ("odoo_to_shopify", "Odoo -> Shopify"),
            # Legacy value stored by 19.0.1.0.7; coerced to shopify_to_odoo.
            ("two_way", "Shopify -> Odoo"),
        ],
        default="shopify_to_odoo",
        config_parameter=PREFIX + "discount_sync_direction",
        help="Which direction order discounts should sync",
    )
    shopify_discount_catalogue_sync_enabled = fields.Boolean(
        string="Sync Shopify discount catalogue",
        default=True,
        config_parameter=PREFIX + "discount_catalogue_sync_enabled",
        help="Shopify → Odoo only. Pulls automatic sales and discount "
        "codes (title, %, dates, minimums, combinations) into Shopify "
        "Ops → Discounts. Needs the read_discounts Admin API scope.",
    )
    shopify_discount_apply_manual_orders = fields.Boolean(
        string="Apply catalogue discounts on manual Odoo orders",
        default=True,
        config_parameter=PREFIX + "discount_apply_manual_orders",
        help="While an automatic Shopify sale is active (e.g. Afterpay "
        "Day 15% off), apply it as Disc.% on manual quotations. Typed "
        "Shopify coupon codes on Other Info also apply. Shopify-pulled "
        "orders still use the discount already on the Shopify order.",
    )
    shopify_shipping_charge_sync_enabled = fields.Boolean(
        string="Sync shipping charges",
        default=True,
        config_parameter=PREFIX + "shipping_charge_sync_enabled",
        help="Copy Shopify shipping onto a dedicated Odoo sale line "
        "(Shopify → Odoo). Uses the net amount the customer paid after "
        "any shipping discounts.",
    )
    shopify_order_sync_after = fields.Datetime(
        string="Only pull / update orders created after",
        help="Shopify orders created before this date and time are ignored: "
        "no pull, no address / shipping / discount / tag updates, no "
        "order-edit. Leave empty to process all new webhooks (existing "
        "sale orders still will not get shipping or discount lines added).",
    )

    @api.model
    def _migrate_shopify_discount_direction(self):
        """Rewrite legacy two_way so Settings can load and save."""
        icp = self.env["ir.config_parameter"].sudo()
        if icp.get_param(PREFIX + "discount_sync_direction") == "two_way":
            icp.set_param(PREFIX + "discount_sync_direction", "shopify_to_odoo")

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        if "shopify_discount_sync_direction" in res:
            coerced = _coerce_discount_direction(
                res.get("shopify_discount_sync_direction")
            )
            res["shopify_discount_sync_direction"] = coerced or "shopify_to_odoo"
        return res

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if "shopify_discount_sync_direction" in vals:
                vals["shopify_discount_sync_direction"] = (
                    _coerce_discount_direction(
                        vals.get("shopify_discount_sync_direction")
                    )
                    or "shopify_to_odoo"
                )
        return super().create(vals_list)

    def write(self, vals):
        if "shopify_discount_sync_direction" in vals:
            vals = dict(vals)
            vals["shopify_discount_sync_direction"] = (
                _coerce_discount_direction(
                    vals.get("shopify_discount_sync_direction")
                )
                or "shopify_to_odoo"
            )
        return super().write(vals)

    shopify_map_payment_gateways = fields.Boolean(
        string="Map payment gateways to journals",
        config_parameter=PREFIX + "map_payment_gateways",
        help="Each Shopify gateway registers against a journal 'Card (Gateway)'; created automatically if missing",
    )

    # --- Stock sync ----------------------------------------------------------
    shopify_inventory_mode = fields.Selection(
        [
            ("scheduled", "Scheduled sync"),
            ("near_realtime", "Real-time (on stock change)"),
            ("disabled", "Disabled (manual only)"),
        ],
        default="scheduled",
        config_parameter=PREFIX + "inventory_mode",
    )
    shopify_stock_quantity_mode = fields.Selection(
        [
            ("on_hand", "On hand (physical stock)"),
            ("free", "Free (on hand - reserved)"),
            ("available", "Available (forecast)"),
        ],
        default="free",
        config_parameter=PREFIX + "stock_quantity_mode",
    )
    shopify_stock_sync_only_instock = fields.Boolean(
        string="Sync only in-stock products",
        config_parameter=PREFIX + "stock_sync_only_instock",
        help="Products with zero or negative stock are skipped during sync",
    )

    # --- Product settings ----------------------------------------------------
    shopify_product_create_mode = fields.Selection(
        [
            ("create_update", "Create new products + update existing"),
            ("update_only", "Update existing products only"),
        ],
        default="create_update",
        config_parameter=PREFIX + "product_create_mode",
    )
    shopify_product_auto_publish = fields.Boolean(
        string="Auto-publish new synced products",
        default=True,
        config_parameter=PREFIX + "product_auto_publish",
    )
    shopify_product_sync_only_instock = fields.Boolean(
        string="Sync only products with available stock in Odoo",
        config_parameter=PREFIX + "product_sync_only_instock",
    )
    shopify_product_collections_from_categories = fields.Boolean(
        string="Create Shopify collections from Odoo categories",
        config_parameter=PREFIX + "product_collections_from_categories",
    )
    shopify_product_sync_categories = fields.Char(
        string="Only sync these Odoo categories",
        config_parameter=PREFIX + "product_sync_categories",
        help="Comma-separated category names. Empty = all categories.",
    )
    shopify_product_sync_tags = fields.Char(
        string="Only sync products with these tags",
        config_parameter=PREFIX + "product_sync_tags",
        help="Comma-separated tag names. Products without any of these tags are skipped. Empty = all.",
    )
    shopify_product_price_tax_included = fields.Boolean(
        string="Send tax-included prices to Shopify",
        config_parameter=PREFIX + "product_price_tax_included",
    )
    shopify_product_sell_out_of_stock = fields.Boolean(
        string="Sell out of stock (continue selling)",
        config_parameter=PREFIX + "product_sell_out_of_stock",
    )
    shopify_price_from_pricelist = fields.Boolean(
        string="Shopify price from Odoo pricelist",
        config_parameter=PREFIX + "price_from_pricelist",
    )
    shopify_pricelist_id = fields.Many2one(
        "product.pricelist",
        string="Pricelist",
        config_parameter=PREFIX + "pricelist_id",
    )

    # --- Customer settings -----------------------------------------------------
    shopify_customer_create_as = fields.Selection(
        [("individual", "Individual"), ("company", "Company")],
        default="individual",
        config_parameter=PREFIX + "customer_create_as",
    )
    shopify_customer_b2b_sync = fields.Boolean(
        string="B2B sync (companies, contacts, pricelists, payment terms)",
        config_parameter=PREFIX + "customer_b2b_sync",
    )
    shopify_customer_min_orders_enabled = fields.Boolean(
        string="Minimum orders filter",
        config_parameter=PREFIX + "customer_min_orders_enabled",
    )
    shopify_customer_min_orders = fields.Integer(
        string="Minimum orders",
        default=0,
        config_parameter=PREFIX + "customer_min_orders",
    )
    shopify_customer_b2b_only = fields.Boolean(
        string="B2B customers only",
        config_parameter=PREFIX + "customer_b2b_only",
    )
    shopify_customer_sync_tags = fields.Char(
        string="Only sync customers with these tags",
        config_parameter=PREFIX + "customer_sync_tags",
    )

    @api.model
    def get_values(self):
        res = super().get_values()
        icp = self.env["ir.config_parameter"].sudo()
        res["shopify_order_import_since"] = _parse_saved_datetime(
            icp.get_param(PREFIX + "order_import_since")
        )
        res["shopify_order_sync_after"] = _parse_saved_datetime(
            icp.get_param(PREFIX + "order_sync_after")
            or icp.get_param(PREFIX + "order_charge_sync_from")
        )
        res["shopify_product_import_since"] = _parse_saved_datetime(
            icp.get_param(PREFIX + "product_import_since")
        )
        return res

    def set_values(self):
        super().set_values()
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param(
            PREFIX + "order_import_since",
            fields.Datetime.to_string(self.shopify_order_import_since) or "",
        )
        icp.set_param(
            PREFIX + "order_charge_sync_from",
            fields.Datetime.to_string(self.shopify_order_sync_after) or "",
        )
        icp.set_param(
            PREFIX + "order_sync_after",
            fields.Datetime.to_string(self.shopify_order_sync_after) or "",
        )
        icp.set_param(
            PREFIX + "product_import_since",
            fields.Datetime.to_string(self.shopify_product_import_since) or "",
        )

    # --- Manual sync buttons ---------------------------------------------------
    def action_manual_inventory_sync(self):
        self.env["shopify.inventory.sync"].cron_push_inventory(limit=500, manual=True)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": "Inventory sync", "message": "Inventory push finished — see Sync Logs for details.", "type": "success", "sticky": False},
        }

    def action_manual_discount_catalogue_sync(self):
        job = self.env["shopify.sync.job"].sudo().enqueue(
            "discount catalogue pull",
            "discount_catalogue_sync",
            {"full_sync": True},
        )
        cron = self.env.ref(
            "shopify_order_ops.ir_cron_process_jobs",
            raise_if_not_found=False,
        )
        if cron:
            try:
                cron.sudo()._trigger()
            except Exception:  # noqa: BLE001
                _logger.exception("Could not trigger discount catalogue job cron")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Discount catalogue",
                "message": "Pull queued (job %s). Open Discounts in a minute "
                "— see Sync Logs if it stays empty."
                % job.id,
                "type": "success",
                "sticky": False,
            },
        }

    def action_manual_product_sync(self):
        job = self.env["shopify.sync.job"].sudo().enqueue(
            "product sync (manual)",
            "product_sync",
            {"limit": 250, "topic": "manual"},
        )
        job._process_one()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Product sync",
                "message": (
                    "Product sync job %s running (batches of 250 continue "
                    "automatically). See Jobs + Sync Logs."
                    % job.id
                ),
                "type": "success",
                "sticky": False,
            },
        }

    def action_import_products_since(self):
        self.ensure_one()
        if not self.shopify_product_import_since:
            raise UserError(
                "Pick a date and time first — products updated since then "
                "will be synced."
            )
        stamp = fields.Datetime.to_string(self.shopify_product_import_since) or ""
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param(PREFIX + "product_import_since", stamp)
        job = self.env["shopify.sync.job"].sudo().enqueue(
            "product sync since %s" % stamp,
            "product_sync",
            {"limit": 250, "since": stamp, "topic": "import"},
        )
        job._process_one()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Product import",
                "message": (
                    "Job %s started for products updated since %s "
                    "(batches of up to 250; continues automatically). "
                    "Check Jobs + Sync Logs for created / SKU changes."
                    % (job.id, stamp)
                ),
                "type": "success",
                "sticky": False,
            },
        }

    def action_set_order_sync_after_now(self):
        stamp = fields.Datetime.to_string(fields.Datetime.now()) or ""
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param(PREFIX + "order_sync_after", stamp)
        icp.set_param(PREFIX + "order_charge_sync_from", stamp)
        self.shopify_order_sync_after = fields.Datetime.now()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Order sync window",
                "message": (
                    "Only Shopify orders created after %s will be pulled "
                    "or updated."
                    % stamp
                ),
                "type": "success",
                "sticky": False,
            },
        }

    def action_import_historical_orders(self):
        self.ensure_one()
        if not self.shopify_order_import_since:
            raise UserError(
                "Pick a date and time first — orders created since then "
                "will be imported."
            )
        stamp = fields.Datetime.to_string(self.shopify_order_import_since) or ""
        self.env["ir.config_parameter"].sudo().set_param(
            PREFIX + "order_import_since", stamp
        )
        summary = self.env["shopify.order.pull.engine"].enqueue_historical(
            self.shopify_order_import_since
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Historical import",
                "message": summary,
                "type": "success",
                "sticky": False,
            },
        }

    def action_register_shopify_webhooks(self):
        summary = self.env["shopify.webhook.registration"].register_webhooks()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Shopify webhooks",
                "message": summary,
                "type": "success",
                "sticky": False,
            },
        }

    def action_list_shopify_webhooks(self):
        summary = self.env["shopify.webhook.registration"].list_webhooks()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Shopify webhooks (this access token)",
                "message": summary,
                "type": "info",
                "sticky": True,
            },
        }

    def action_replace_shopify_webhooks(self):
        summary = self.env["shopify.webhook.registration"].replace_webhooks()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Shopify webhooks replaced",
                "message": summary,
                "type": "success",
                "sticky": True,
            },
        }
