import logging
from urllib.parse import urlparse

from odoo import models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

WEBHOOK_PATH = "/shopify_order_ops/webhook/orders-edited"
TOPICS = (
    "orders/create",
    "orders/edited",
    "orders/updated",
    "orders/cancelled",
    "refunds/create",
    "products/update",
    "products/create",
    "customers/create",
    "customers/update",
    "discounts/create",
    "discounts/update",
    "discounts/delete",
    "fulfillments/create",
    "fulfillments/update",
)
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


class ShopifyWebhookRegistration(models.AbstractModel):
    _name = "shopify.webhook.registration"
    _description = "Registers Shopify webhooks for the ops module"

    def register_webhooks(self):
        """Ensure Shopify subscriptions exist for our webhook endpoint.

        Idempotent: an existing subscription with the same topic+address is
        kept; only missing topics are created. Returns a human-readable
        summary string for the notification bubble.
        """
        log = self.env["shopify.sync.log"]

        # --- base URL / address ------------------------------------------
        base_url = (
            self.env["ir.config_parameter"].sudo().get_param("web.base.url") or ""
        ).strip().rstrip("/")
        if not base_url:
            raise UserError(
                "web.base.url is not configured; cannot build the Shopify "
                "webhook address."
            )

        warnings = []
        parsed = urlparse(base_url)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme != "https":
            warnings.append("base URL is not HTTPS (Shopify requires HTTPS)")
        if hostname in LOCAL_HOSTS or hostname.endswith(".local"):
            warnings.append(
                "base URL looks like localhost and is not reachable from Shopify"
            )

        address = base_url + WEBHOOK_PATH

        # --- diff against existing subscriptions --------------------------
        client = self.env["shopify.api.client"]
        try:
            existing = client.get("webhooks.json", params={"limit": 250}).get(
                "webhooks"
            ) or []
            existing_pairs = {
                (hook.get("topic"), hook.get("address")) for hook in existing
            }

            created, skipped = [], []
            for topic in TOPICS:
                if (topic, address) in existing_pairs:
                    skipped.append(topic)
                    continue
                client.post(
                    "webhooks.json",
                    {
                        "webhook": {
                            "topic": topic,
                            "address": address,
                            "format": "json",
                        }
                    },
                )
                created.append(topic)
        except Exception as exc:  # noqa: BLE001 - log, then surface to the user
            log.log_event(
                "error",
                "Webhook registration failed for %s: %s" % (address, exc),
                source="webhook",
            )
            raise

        # --- report --------------------------------------------------------
        if created:
            log.log_event(
                "info",
                "Registered Shopify webhooks at %s: %s"
                % (address, ", ".join(created)),
                source="webhook",
            )
        if skipped:
            log.log_event(
                "info",
                "Shopify webhooks already present at %s, skipped: %s"
                % (address, ", ".join(skipped)),
                source="webhook",
            )
        if warnings:
            log.log_event(
                "warning",
                "Webhook registration warnings for %s: %s"
                % (address, "; ".join(warnings)),
                source="webhook",
            )

        parts = []
        if created:
            parts.append("created: " + ", ".join(created))
        if skipped:
            parts.append("already registered: " + ", ".join(skipped))
        summary = "Webhook address %s — %s." % (
            address,
            "; ".join(parts) if parts else "nothing to do",
        )
        if warnings:
            summary += " Warning: " + "; ".join(warnings) + "."
        return summary

    def list_webhooks(self):
        """Describe subscriptions this Admin API token can see."""
        client = self.env["shopify.api.client"]
        existing = client.get("webhooks.json", params={"limit": 250}).get(
            "webhooks"
        ) or []
        if not existing:
            return (
                "This access token sees no webhook subscriptions. Other apps "
                "(leftover custom apps, Settings → Notifications) can still "
                "POST to the same URL; those are signed with a different "
                "secret."
            )
        lines = []
        for hook in existing:
            lines.append(
                "%s → %s (id %s)"
                % (
                    hook.get("topic") or "?",
                    hook.get("address") or "?",
                    hook.get("id") or "?",
                )
            )
        return (
            "Webhooks owned by the current access token (%s):\n%s\n"
            "GET webhooks.json cannot see other apps or Admin → Notifications "
            "hooks. Those still hit Odoo and fail HMAC with this Secret."
            % (len(existing), "\n".join(lines))
        )

    def _webhook_address(self):
        base_url = (
            self.env["ir.config_parameter"].sudo().get_param("web.base.url") or ""
        ).strip().rstrip("/")
        if not base_url:
            raise UserError(
                "web.base.url is not configured; cannot build the Shopify "
                "webhook address."
            )
        return base_url + WEBHOOK_PATH

    def replace_webhooks(self):
        """Delete this app's ops-module subscriptions, then recreate for this Odoo.

        Removes every subscription whose address contains WEBHOOK_PATH (production
        and staging Odoo URLs), then registers a fresh set at web.base.url.
        """
        log = self.env["shopify.sync.log"]
        address = self._webhook_address()
        client = self.env["shopify.api.client"]
        existing = client.get("webhooks.json", params={"limit": 250}).get(
            "webhooks"
        ) or []
        deleted = []
        for hook in existing:
            hook_addr = hook.get("address") or ""
            hook_id = hook.get("id")
            if WEBHOOK_PATH not in hook_addr or not hook_id:
                continue
            client.delete("webhooks/%s.json" % hook_id)
            deleted.append("%s %s" % (hook.get("topic") or "?", hook_addr))
        if deleted:
            log.log_event(
                "info",
                "Deleted %s Shopify ops webhook(s): %s"
                % (len(deleted), "; ".join(deleted)),
                source="webhook",
            )
        created_summary = self.register_webhooks()
        return "Deleted %s old subscription(s). %s" % (len(deleted), created_summary)
