import json
import logging

from odoo import fields, http
from odoo.http import request
from werkzeug import Response

from ..models.shopify_webhook_hmac import (
    hmac_mismatch_detail,
    normalize_webhook_secret,
    raw_http_body,
    shopify_hmac_from_headers,
    webhook_hmac_matches,
)
from ..models.discount_catalogue import extract_discount_gid
from ..models.sync_job import IMMEDIATE_JOB_TYPES

_logger = logging.getLogger(__name__)

SECRET_PARAM = "shopify_order_ops.webhook_secret"


def _webhook_job_type(env, topic):
    if "cancelled" in topic:
        return "order_cancel"
    if "edited" in topic:
        return "order_edit"
    if "refunds" in topic:
        return "order_refund"
    if topic.startswith("products/"):
        return "product_update"
    if topic.startswith("customers/"):
        return "customer_update"
    if topic.startswith("discounts/"):
        return "discount_update"
    if topic.startswith("fulfillments/"):
        return "fulfillment_pull"
    if topic == "orders/updated":
        # One Shopify topic covers tags, addresses, charges, and quantities.
        return "order_address_sync"
    return "order_pull"


def _extract_order_id(payload):
    """Pull the Shopify numeric order id out of a webhook payload.

    - orders/edited payloads carry ``order_edit.order_id`` (some senders also
      mirror a top-level ``order_id``).
    - orders/updated payloads are plain order objects with a top-level ``id``.
    """
    if not isinstance(payload, dict):
        return None
    order_edit = payload.get("order_edit")
    if isinstance(order_edit, dict) and order_edit.get("order_id"):
        return order_edit["order_id"]
    return payload.get("order_id") or payload.get("id")


class ShopifyOpsWebhookController(http.Controller):
    @http.route(
        "/shopify_order_ops/webhook/orders-edited",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        save_session=False,
    )
    def orders_edited(self, **kwargs):
        """Receive Shopify orders/edited (and orders/updated) webhooks.

        Verify the HMAC signature, extract the Shopify order id, enqueue an
        ``order_edit`` job (deduplicated) and acknowledge with HTTP 200 as
        fast as possible. Returns 401 when the signature cannot be verified.
        """
        env = request.env
        log = env["shopify.sync.log"].sudo()  # auth='public' -> needs sudo

        httprequest = request.httprequest
        raw_body = getattr(request, "shopify_webhook_raw_body", None)
        if raw_body is None:
            raw_body = raw_http_body(httprequest)
        headers = httprequest.headers
        received_hmac = shopify_hmac_from_headers(
            headers, getattr(httprequest, "environ", None)
        )
        topic = headers.get("X-Shopify-Topic") or ""

        # --- 1. HMAC verification (fail closed) --------------------------
        secret = normalize_webhook_secret(
            env["ir.config_parameter"].sudo().get_param(SECRET_PARAM) or ""
        )
        if not secret:
            log.log_event(
                "error",
                "Rejected webhook (topic %s): %s is not configured; "
                "refusing to verify signatures." % (topic or "unknown", SECRET_PARAM),
                source="webhook",
            )
            return Response("Unauthorized", status=401)

        if not webhook_hmac_matches(secret, raw_body, received_hmac):
            log.log_event(
                "error",
                "Rejected webhook (topic %s): HMAC signature mismatch (%s)."
                % (
                    topic or "unknown",
                    hmac_mismatch_detail(
                        secret, raw_body, received_hmac, headers
                    ),
                ),
                source="webhook",
            )
            return Response("Unauthorized", status=401)

        # --- 2. Parse body and find the order id -------------------------
        try:
            payload = json.loads(raw_body)
        except (ValueError, TypeError):
            log.log_event(
                "warning",
                "Webhook body was not valid JSON (topic %s); acknowledged "
                "and dropped." % (topic or "unknown"),
                source="webhook",
            )
            return "OK"

        if (topic or "").startswith("discounts/"):
            icp = env["ir.config_parameter"].sudo()
            now_iso = fields.Datetime.now().isoformat()
            icp.set_param("shopify_order_ops.last_webhook_at", now_iso)
            icp.set_param("shopify_order_ops.last_webhook_topic", topic or "unknown")
            icp.set_param(
                "shopify_order_ops.last_webhook_%s" % topic.replace("/", "_"),
                now_iso,
            )
            gid = extract_discount_gid(payload) or ""
            jobs = env["shopify.sync.job"].sudo()
            pending = jobs.search(
                [
                    ("job_type", "=", "discount_update"),
                    ("state", "in", ["pending", "processing"]),
                ]
            )
            existing = next(
                (
                    j
                    for j in pending
                    if str(j.payload_dict().get("discount_id") or "") == str(gid)
                    and gid
                ),
                None,
            )
            if existing:
                return "OK"
            job = jobs.enqueue_and_process(
                name="discount_update %s" % (gid or "unknown"),
                job_type="discount_update",
                payload_dict={
                    "discount_id": gid,
                    "topic": topic,
                    "raw": payload,
                },
            )
            log.log_event(
                "info",
                "Queued discount_update job %s (topic %s, %s)."
                % (job.id, topic, gid or "no id"),
                source="webhook",
                job=job,
            )
            return "OK"

        if (topic or "").startswith("fulfillments/"):
            icp = env["ir.config_parameter"].sudo()
            now_iso = fields.Datetime.now().isoformat()
            icp.set_param("shopify_order_ops.last_webhook_at", now_iso)
            icp.set_param("shopify_order_ops.last_webhook_topic", topic or "unknown")
            if topic:
                icp.set_param(
                    "shopify_order_ops.last_webhook_%s" % topic.replace("/", "_"),
                    now_iso,
                )
            order_id = payload.get("order_id")
            if not order_id:
                log.log_event(
                    "warning",
                    "Fulfillment webhook had no order_id (topic %s); dropped."
                    % (topic or "unknown"),
                    source="webhook",
                )
                return "OK"
            order_ref = str(order_id)
            jobs = env["shopify.sync.job"].sudo()
            job = jobs.enqueue_and_process(
                name="fulfillment_pull %s" % order_ref,
                job_type="fulfillment_pull",
                payload_dict={
                    "order_id": order_id,
                    "topic": topic,
                    "raw_fulfillment": payload,
                },
            )
            log.log_event(
                "info",
                "Queued fulfillment_pull job %s (topic %s, order %s)."
                % (job.id, topic, order_ref),
                source="webhook",
                job=job,
                shopify_order_ref=order_ref,
            )
            return "OK"

        order_id = _extract_order_id(payload)
        if not order_id:
            log.log_event(
                "warning",
                "Webhook payload had no recognizable order id (topic %s); "
                "acknowledged and dropped." % (topic or "unknown"),
                source="webhook",
            )
            return "OK"
        order_ref = str(order_id)

        icp = env["ir.config_parameter"].sudo()
        now_iso = fields.Datetime.now().isoformat()
        icp.set_param("shopify_order_ops.last_webhook_at", now_iso)
        icp.set_param("shopify_order_ops.last_webhook_topic", topic or "unknown")
        if topic:
            icp.set_param(
                "shopify_order_ops.last_webhook_%s" % topic.replace("/", "_"),
                now_iso,
            )

        # --- 3. orders/updated splits into tag / address / charge / qty ---
        if topic == "orders/updated":
            created = (
                env["shopify.order.pull.engine"]
                .sudo()
                .enqueue_split_order_updates(
                    order_id, topic, payload=payload, order=payload
                )
            )
            if not created:
                return "OK"
            types = ", ".join(created.mapped("job_type"))
            log.log_event(
                "info",
                "Shopify order %s updated: queued %s."
                % (order_ref, types),
                source="webhook",
                shopify_order_ref=order_ref,
                job=created[:1],
            )
            return "OK"

        job_type = _webhook_job_type(env, topic)

        # --- 3. Deduplicate against queued/running jobs ------------------
        # NOTE: substring matching (payload ilike '%1016%') silently drops
        # events when an unrelated queued job merely CONTAINS the digits
        # (order 10160 blocks order 1016). Compare parsed order ids exactly.
        jobs = env["shopify.sync.job"].sudo()
        pending = jobs.search(
            [("job_type", "=", job_type), ("state", "in", ["pending", "processing"])],
        )
        existing = next(
            (
                j for j in pending
                if str(j.payload_dict().get("order_id") or "") == order_ref
            ),
            None,
        )
        if existing:
            log.log_event(
                "info",
                "Duplicate webhook for order %s (topic %s); job %s already "
                "queued." % (order_ref, topic or "unknown", existing.id),
                source="webhook",
                shopify_order_ref=order_ref,
            )
            return "OK"

        # --- 4. Enqueue (and process immediately for real-time job types) -
        job = jobs.enqueue_and_process(
            name="{} {}".format(job_type, order_ref),
            job_type=job_type,
            payload_dict={
                "order_id": order_id,
                "topic": topic,
                "raw": payload,
            },
        )
        log.log_event(
            "info",
            "Queued %s job %s for Shopify order %s (topic %s)%s."
            % (
                job_type,
                job.id,
                order_ref,
                topic or "unknown",
                " (real-time processing triggered)"
                if job_type in IMMEDIATE_JOB_TYPES
                else "",
            ),
            source="webhook",
            job=job,
            shopify_order_ref=order_ref,
        )
        return "OK"
