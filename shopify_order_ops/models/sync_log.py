import logging

from odoo import fields, models

_logger = logging.getLogger(__name__)


class ShopifySyncLog(models.Model):
    _name = "shopify.sync.log"
    _description = "Shopify Sync Log"
    _order = "id desc"

    level = fields.Selection(
        [("info", "Info"), ("warning", "Warning"), ("error", "Error")],
        default="info",
        required=True,
    )
    message = fields.Text(required=True)
    source = fields.Char(string="Source", help="Subsystem, e.g. order_edit, inventory, fulfillment, webhook")
    job_id = fields.Many2one("shopify.sync.job", string="Job", ondelete="set null")
    shopify_order_ref = fields.Char(string="Shopify Order")

    def log_event(self, level, message, source=None, job=None, shopify_order_ref=None):
        """Create a persistent log record and mirror it to the Odoo server log."""
        rec = self.sudo().create(
            {
                "level": level,
                "message": message,
                "source": source or False,
                "job_id": job.id if job else False,
                "shopify_order_ref": shopify_order_ref or False,
            }
        )
        log_fn = {
            "info": _logger.info,
            "warning": _logger.warning,
            "error": _logger.error,
        }.get(level, _logger.info)
        log_fn("[%s] %s", source or "shopify_ops", message)
        return rec
