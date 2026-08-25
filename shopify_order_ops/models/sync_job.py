import json
import logging

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

JOB_TYPES = [
    ("order_pull", "Order Pull"),
    ("order_edit", "Order Edit"),
    ("order_cancel", "Order Cancel"),
    ("order_refund", "Order Refund"),
    ("order_update_push", "Order Update Push"),
    ("order_address_push", "Order Address Push"),
    ("order_address_sync", "Order Address Sync"),
    ("order_tag_sync", "Order Tag Sync"),
    ("order_charge_sync", "Order Charge Sync"),
    ("order_qty_sync", "Order Quantity Sync"),
    ("order_discount_push", "Order Discount Push"),
    ("price_update", "Price Update"),
    ("customer_update", "Customer Update"),
    ("product_update", "Product Update"),
    ("product_update_push", "Product Update Push"),
    ("discount_update", "Discount Catalogue Update"),
    ("discount_catalogue_sync", "Discount Catalogue Pull"),
    ("product_sync", "Product Sync"),
    ("fulfillment_pull", "Fulfillment Pull"),
]

# Webhook- or UI-enqueued jobs that should run immediately (cron is backstop).
IMMEDIATE_JOB_TYPES = frozenset(
    {
        "order_address_sync",
        "order_address_push",
        "order_tag_sync",
        "order_charge_sync",
        "order_qty_sync",
        "order_edit",
        "order_discount_push",
        "fulfillment_pull",
        "customer_update",
        "discount_update",
        "product_sync",
    }
)


class ShopifySyncJob(models.Model):
    """Durable queue for Shopify-driven events.

    Webhook intake enqueues jobs; the cron (`_cron_process_jobs`) dispatches them
    to the engine registered per job type. Failures retry with a max-attempt cap.
    """

    _name = "shopify.sync.job"
    _description = "Shopify Sync Job Queue"
    _order = "id asc"

    name = fields.Char(required=True)
    job_type = fields.Selection(JOB_TYPES, required=True)
    payload = fields.Text(string="Payload (JSON)")
    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("processing", "Processing"),
            ("done", "Done"),
            ("failed", "Failed"),
        ],
        default="pending",
        required=True,
        index=True,
    )
    attempts = fields.Integer(default=0)
    max_attempts = fields.Integer(default=5)
    error = fields.Text()
    processed_date = fields.Datetime()

    def enqueue(self, name, job_type, payload_dict):
        return self.sudo().create(
            {
                "name": name,
                "job_type": job_type,
                "payload": json.dumps(payload_dict or {}),
            }
        )

    def enqueue_and_process(self, name, job_type, payload_dict):
        """Enqueue and, for real-time job types, run as soon as possible.

        Preferred path: trigger the queue cron so the job runs within seconds
        in a cron worker (webhook responses stay fast — Shopify only waits
        ~5 s before treating the delivery as failed). If triggering fails for
        any reason, fall back to inline processing; the 2-minute cron remains
        the backstop either way.
        """
        job = self.enqueue(name, job_type, payload_dict)
        if job_type in IMMEDIATE_JOB_TYPES:
            try:
                cron = self.env.ref(
                    "shopify_order_ops.ir_cron_process_jobs",
                    raise_if_not_found=False,
                )
                if cron:
                    cron.sudo()._trigger()
                else:
                    job._process_one()
            except Exception:  # noqa: BLE001 - cron retries; never break caller
                _logger.exception(
                    "Immediate processing failed for Shopify job %s", job.name
                )
        return job

    def payload_dict(self):
        self.ensure_one()
        try:
            return json.loads(self.payload or "{}")
        except (ValueError, TypeError):
            return {}

    def action_process_now(self):
        """Run this job immediately from current Shopify data. Does not edit Shopify."""
        for job in self:
            if job.state in ("done", "failed", "processing"):
                job.write(
                    {
                        "state": "pending",
                        "attempts": 0,
                        "error": False,
                        "processed_date": False,
                    }
                )
            job._process_one()
            if job.state == "failed":
                raise UserError(
                    job.error or "Job failed. See Shopify Ops → Sync Logs."
                )
        return True

    @api.model
    def _cron_process_jobs(self, limit=20):
        jobs = self.search([("state", "=", "pending")], limit=limit)
        for job in jobs:
            job._process_one()

    def _process_one(self):
        self.ensure_one()
        # attempts/state bump happens OUTSIDE the savepoint so retries persist
        self.attempts += 1
        self.state = "processing"
        try:
            with self.env.cr.savepoint():
                self._dispatch()
            self.write(
                {
                    "state": "done",
                    "processed_date": fields.Datetime.now(),
                    "error": False,
                }
            )
        except Exception as exc:  # noqa: BLE001 - job isolation is intentional
            _logger.exception("Shopify job %s failed", self.name)
            permanent = self.attempts >= self.max_attempts
            self.write(
                {
                    "state": "failed" if permanent else "pending",
                    "error": str(exc),
                }
            )

    def _dispatch(self):
        self.ensure_one()
        if self.job_type == "order_pull":
            self.env["shopify.order.pull.engine"].process_order_pull(self)
        elif self.job_type == "order_edit":
            self.env["shopify.order.edit.engine"].process_order_edit(self)
        elif self.job_type == "order_cancel":
            self.env["shopify.cancel.sync"].process_order_cancel(self)
        elif self.job_type == "order_refund":
            self.env["shopify.refund.sync"].process_order_refund(self)
        elif self.job_type == "order_update_push":
            self.env["shopify.order.update.engine"].process_order_update_push(self)
        elif self.job_type == "order_address_push":
            self.env["shopify.order.update.engine"].process_order_address_push(self)
        elif self.job_type == "order_address_sync":
            self.env["shopify.order.pull.engine"].process_order_address_sync(self)
        elif self.job_type == "order_tag_sync":
            self.env["shopify.order.pull.engine"].process_order_tag_sync(self)
        elif self.job_type == "order_charge_sync":
            self.env["shopify.order.pull.engine"].process_order_charge_sync(self)
        elif self.job_type == "order_qty_sync":
            self.env["shopify.order.pull.engine"].process_order_qty_sync(self)
        elif self.job_type == "order_discount_push":
            self.env["shopify.order.update.engine"].process_order_discount_push(self)
        elif self.job_type == "price_update":
            self.env["shopify.price.sync"].process_price_update(self)
        elif self.job_type == "customer_update":
            self.env["shopify.customer.sync"].process_customer_webhook(self)
        elif self.job_type == "product_update":
            self.env["shopify.product.sync"].process_product_webhook(self)
        elif self.job_type == "product_update_push":
            self.env["shopify.product.sync"].process_product_update_push(self)
        elif self.job_type == "discount_update":
            self.env["shopify.discount.catalogue.sync"].process_discount_webhook(
                self
            )
        elif self.job_type == "discount_catalogue_sync":
            self.env["shopify.discount.catalogue.sync"].cron_sync_discounts(
                limit=1000
            )
        elif self.job_type == "product_sync":
            payload = self.payload_dict()
            try:
                limit = max(1, int(payload.get("limit") or 250))
            except (TypeError, ValueError):
                limit = 250
            try:
                since_id = max(0, int(payload.get("since_id") or 0))
            except (TypeError, ValueError):
                since_id = 0
            since = (payload.get("since") or "").strip()
            if since and not since_id:
                # Fresh import window — rewind watermark. Continuations keep
                # the same window via window_start without resetting the cursor.
                self.env["ir.config_parameter"].sudo().set_param(
                    "shopify_order_ops.last_product_sync", since
                )
            self.env["shopify.product.sync"].cron_sync_products(
                limit=limit,
                since_id=since_id,
                window_start=since or None,
            )
        elif self.job_type == "fulfillment_pull":
            self.env["shopify.fulfillment.sync"].process_fulfillment_pull(self)
        else:
            raise ValueError(f"No dispatcher for job type {self.job_type}")
