import json
import logging
from datetime import datetime, time, timedelta

import pytz

from odoo import fields, http
from odoo.http import request
from werkzeug import Response

_logger = logging.getLogger(__name__)

PARAM_PREFIX = "shopify_order_ops."

# Queue engines: status is derived from shopify.sync.job rows of that type.
QUEUE_ENGINES = [
    ("order_pull", "Order Pull"),
    ("order_edit", "Order Edit → Invoice"),
]

# Address sync engine: one tile covering both directions.
ADDRESS_JOB_TYPES = ["order_address_sync", "order_address_push"]

# Cron engines: (key, label, config flag, log source). Status is derived from
# the *_sync_enabled flag plus shopify.sync.log entries from that source.
CRON_ENGINES = [
    ("inventory", "Inventory Push", "inventory_sync_enabled", "inventory"),
    # fulfillment is handled separately (_fulfillment_engine) to show job queue activity
    ("customer", "Customer Sync", "customer_sync_enabled", "customer"),
    ("product", "Product Sync", "product_sync_enabled", "product"),
    ("price", "Price Sync", "price_sync_enabled", "price"),
    ("metafield", "Metafield Sync", "metafield_sync_enabled", "metafield"),
    ("discount_catalogue", "Discount Catalogue", "discount_catalogue_sync_enabled", "discount_catalogue"),
]

WEBHOOK_TOPICS = [
    "orders/create",
    "orders/edited",
    "orders/updated",
    "fulfillments/create",
    "fulfillments/update",
]


def _parse_iso(value):
    """Parse a stored datetime string into a naive datetime.

    Webhook timestamps are written via ``fields.Datetime.now().isoformat()``
    (naive UTC). Accept Odoo's default datetime format as a fallback.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except (ValueError, TypeError):
            continue
    return None


class ShopifyOpsDashboardData(http.Controller):
    @http.route(
        "/shopify_order_ops/dashboard/data",
        type="http",
        auth="user",
        methods=["GET"],
        csrf=False,
    )
    def dashboard_data(self, **kwargs):
        """Aggregate everything the OWL dashboard renders.

        Contract (implemented by agent task G). Return a werkzeug Response
        with content_type 'application/json'. JSON shape:

        {
          "kpis": {
            "pulled_today": int,           # order_pull jobs done today
            "pulled_week": int,            # done in the last 7 days
            "success_rate": float|null,    # done/(done+failed) last 24h, 0-100, 1 decimal
            "success_done": int, "success_total": int,
            "queue_count": int,            # pending jobs
            "queue_oldest_seconds": int|null,
            "failed_count": int,           # jobs currently in state failed
            "failed_latest_ref": str,      # e.g. "order_edit · #1038" or ""
            "last_webhook_seconds": int|null,   # from param shopify_order_ops.last_webhook_at
            "last_webhook_topic": str,
          },
          "engines": [                      # always all 8, in this order
            {"key": "order_pull",  "label": "Order Pull",            "status": "ok|warn|fail|off", "meta": "..."},
            {"key": "order_edit",  "label": "Order Edit → Invoice",  "status": ..., "meta": "..."},
            {"key": "inventory",   "label": "Inventory Push",        ...},
            {"key": "fulfillment", "label": "Fulfillment Sync",      ...},
            {"key": "customer",    "label": "Customer Sync",         ...},
            {"key": "product",     "label": "Product Sync",          ...},
            {"key": "price",       "label": "Price Sync",            ...},
            {"key": "metafield",   "label": "Metafield Sync",        ...},
          ],
          "jobs": [                         # latest N shopify.sync.job (jobs_limit, cap 10000)
            {"id": int, "time": "HH:MM", "type": str, "name": str,
             "state": "pending|processing|done|failed",
             "attempts": int, "duration": str,       # e.g. "2.1s" or ""
             "error": str, "payload_preview": str},  # first ~600 chars
          ],
          "webhooks": [                     # topics orders/create, orders/edited, orders/updated
            {"topic": str, "active": bool, "last_received_seconds": int|null},
          ],
          "events": [                       # latest 10 shopify.sync.log entries
            {"time": "HH:MM:SS", "level": "info|warning|error",
             "source": str, "message": str},
          ],
          "generated_at": "ISO-8601",
        }

        Engine status rules:
        - order_pull/order_edit: any failed job of that type -> "fail";
          else if a job of that type exists today -> "ok"; else "off".
        - inventory/fulfillment/customer/product/price/metafield: when the
          matching `*_sync_enabled` config flag is off -> "off"
          (meta "disabled"); else error-level log entries from that source in
          the last 24h -> "fail"; warning-level in the last 24h -> "warn";
          else "ok". meta = "last {HH:MM} · {n} today" using the newest log
          entry from that source (n = today's entries), or "never run".
        - webhook "active": the topic has a last_webhook_<topic> param set.
        """
        try:
            data = self._build_dashboard_data(request.env, kwargs)
        except Exception as exc:  # noqa: BLE001 - surface failure as JSON, not HTML
            _logger.exception("Shopify Ops dashboard data aggregation failed")
            return Response(
                json.dumps({"error": str(exc)}),
                status=500,
                content_type="application/json",
            )
        return Response(json.dumps(data, default=str), content_type="application/json")

    # ------------------------------------------------------------------
    # aggregation
    # ------------------------------------------------------------------

    def _build_dashboard_data(self, env, params=None):
        jobs = env["shopify.sync.job"].sudo()
        logs = env["shopify.sync.log"].sudo()
        icp = env["ir.config_parameter"].sudo()
        params = params or {}
        try:
            jobs_limit = int(params.get("jobs_limit") or 50)
        except (TypeError, ValueError):
            jobs_limit = 50
        jobs_limit = max(25, min(jobs_limit, 10000))
        jobs_q = (params.get("jobs_q") or params.get("q") or "").strip()

        now = fields.Datetime.now()
        tz_name = env.context.get("tz") or env.user.tz or "UTC"
        try:
            user_tz = pytz.timezone(tz_name)
        except pytz.UnknownTimeZoneError:
            user_tz = pytz.utc

        def disp(dt, fmt):
            """Format a naive-UTC datetime in the user's timezone."""
            if not dt:
                return ""
            return pytz.utc.localize(dt).astimezone(user_tz).strftime(fmt)

        local_now = pytz.utc.localize(now).astimezone(user_tz)
        today_start = (
            user_tz.localize(
                datetime.combine(local_now.date(), time.min), is_dst=False
            )
            .astimezone(pytz.utc)
            .replace(tzinfo=None)
        )
        day_ago = now - timedelta(hours=24)
        week_ago = now - timedelta(days=7)

        engines = self._engines(jobs, logs, icp, today_start, day_ago, disp)
        return {
            "kpis": self._kpis(jobs, icp, now, today_start, day_ago, week_ago),
            "engines": engines,
            "jobs": self._jobs(jobs, disp, jobs_limit, jobs_q=jobs_q),
            "jobs_total": jobs.search_count(self._jobs_domain(jobs_q)),
            "jobs_limit": jobs_limit,
            "jobs_q": jobs_q,
            "products": self._products(env, disp),
            "webhooks": self._webhooks(icp, now),
            "events": self._events(logs, disp),
            "health": {
                "all_healthy": not any(e["status"] == "fail" for e in engines),
                "failing": [e["label"] for e in engines if e["status"] == "fail"],
                "last_sync": disp(now, "%I:%M %p").lstrip("0"),
            },
            "links": self._links(env),
            "generated_at": now.isoformat(),
        }

    # ---- links -----------------------------------------------------------

    @staticmethod
    def _links(env):
        """Action-based URLs for the dashboard's navigation elements.

        Hash URLs of the form /web#model=X fall back to the default app for
        custom models; the registered act_window action ids resolve reliably.
        """

        def action_id(xmlid):
            rec = env.ref("shopify_order_ops." + xmlid, raise_if_not_found=False)
            return rec.id if rec else None

        jobs_action = action_id("action_shopify_sync_job")
        logs_action = action_id("action_shopify_sync_log")
        maps_action = action_id("action_shopify_metafield_map")
        discounts_action = action_id("action_shopify_discount")
        alerts_action = action_id("action_shopify_ops_alerts")
        health_action = action_id("action_shopify_ops_health")
        integrations_action = action_id("action_shopify_ops_integrations")
        pulled_action = action_id("action_shopify_ops_jobs_pulled")
        queue_action = action_id("action_shopify_ops_jobs_queue")
        failures_action = action_id("action_shopify_ops_jobs_failed")
        return {
            "jobs": ("/web#action=%s" % jobs_action) if jobs_action else "/web#model=shopify.sync.job&view_type=list",
            "logs": ("/web#action=%s" % logs_action) if logs_action else "/web#model=shopify.sync.log&view_type=list",
            "mappings": ("/web#action=%s" % maps_action) if maps_action else "/web#model=shopify.metafield.map&view_type=list",
            "discounts": ("/web#action=%s" % discounts_action) if discounts_action else "/web#model=shopify.discount&view_type=list",
            "job_form_base": ("/web#action=%s&view_type=form&id=" % jobs_action) if jobs_action else "/web#model=shopify.sync.job&view_type=form&id=",
            "alerts": ("/web#action=%s" % alerts_action) if alerts_action else "/web#model=shopify.sync.log&view_type=list",
            "health": ("/web#action=%s" % health_action) if health_action else "/web#model=ir.cron&view_type=list",
            "integrations": ("/web#action=%s" % integrations_action) if integrations_action else "/web#model=res.config.settings&view_type=list",
            "configurations": ("/web#action=%s" % maps_action) if maps_action else "/web#model=shopify.metafield.map&view_type=list",
            "pulled": ("/web#action=%s" % pulled_action) if pulled_action else "/web#model=shopify.sync.job&view_type=list",
            "queue": ("/web#action=%s" % queue_action) if queue_action else "/web#model=shopify.sync.job&view_type=list",
            "failures": ("/web#action=%s" % failures_action) if failures_action else "/web#model=shopify.sync.job&view_type=list",
        }

    # ---- KPIs ---------------------------------------------------------

    def _kpis(self, jobs, icp, now, today_start, day_ago, week_ago):
        pulled_today = jobs.search_count(
            [
                ("job_type", "=", "order_pull"),
                ("state", "=", "done"),
                ("processed_date", ">=", today_start),
            ]
        )
        pulled_week = jobs.search_count(
            [
                ("job_type", "=", "order_pull"),
                ("state", "=", "done"),
                ("processed_date", ">=", week_ago),
            ]
        )

        success_done = jobs.search_count(
            [("state", "=", "done"), ("processed_date", ">=", day_ago)]
        )
        success_failed = jobs.search_count(
            [("state", "=", "failed"), ("write_date", ">=", day_ago)]
        )
        success_total = success_done + success_failed
        success_rate = (
            round(100.0 * success_done / success_total, 1) if success_total else None
        )

        queue_count = jobs.search_count([("state", "=", "pending")])
        oldest = jobs.search(
            [("state", "=", "pending")], order="create_date asc", limit=1
        )
        queue_oldest_seconds = (
            max(0, int((now - oldest.create_date).total_seconds())) if oldest else None
        )

        failed_count = jobs.search_count([("state", "=", "failed")])
        latest_failed = jobs.search(
            [("state", "=", "failed")], order="write_date desc", limit=1
        )
        failed_latest_ref = ""
        if latest_failed:
            job = latest_failed[0]
            ref = job.name or ""
            prefix = (job.job_type or "") + " "
            if ref.startswith(prefix):
                ref = ref[len(prefix):]
            failed_latest_ref = (
                "{} · #{}".format(job.job_type, ref) if ref else (job.job_type or "")
            )

        last_hook_dt = _parse_iso(icp.get_param(PARAM_PREFIX + "last_webhook_at"))
        last_webhook_seconds = (
            max(0, int((now - last_hook_dt).total_seconds())) if last_hook_dt else None
        )
        last_webhook_topic = icp.get_param(PARAM_PREFIX + "last_webhook_topic") or ""

        # ---- yesterday comparisons for the delta lines -------------------
        yesterday_start = today_start - timedelta(days=1)

        pulled_yesterday = jobs.search_count(
            [
                ("job_type", "=", "order_pull"),
                ("state", "=", "done"),
                ("processed_date", ">=", yesterday_start),
                ("processed_date", "<", today_start),
            ]
        )
        pulled_delta_pct = (
            round(100.0 * (pulled_today - pulled_yesterday) / pulled_yesterday, 1)
            if pulled_yesterday
            else None
        )

        y_done = jobs.search_count(
            [
                ("state", "=", "done"),
                ("processed_date", ">=", yesterday_start),
                ("processed_date", "<", today_start),
            ]
        )
        y_failed = jobs.search_count(
            [
                ("state", "=", "failed"),
                ("write_date", ">=", yesterday_start),
                ("write_date", "<", today_start),
            ]
        )
        y_total = y_done + y_failed
        rate_yesterday = (100.0 * y_done / y_total) if y_total else None
        success_delta_pts = (
            round(success_rate - rate_yesterday, 1)
            if (success_rate is not None and rate_yesterday is not None)
            else None
        )

        def _avg_duration(domain):
            recs = jobs.search(
                domain + [("state", "=", "done"), ("processed_date", "!=", False)],
                limit=500,
            )
            durations = [
                (r.processed_date - r.create_date).total_seconds()
                for r in recs
                if r.create_date and r.processed_date
            ]
            return (sum(durations) / len(durations)) if durations else None

        avg_queue_seconds = _avg_duration([("processed_date", ">=", today_start)])
        avg_queue_yesterday = _avg_duration(
            [("processed_date", ">=", yesterday_start), ("processed_date", "<", today_start)]
        )
        avg_queue_delta_seconds = (
            round(avg_queue_seconds - avg_queue_yesterday)
            if (avg_queue_seconds is not None and avg_queue_yesterday is not None)
            else None
        )

        failed_today = jobs.search_count(
            [("state", "=", "failed"), ("write_date", ">=", today_start)]
        )
        failed_delta = failed_today - y_failed

        return {
            "pulled_today": pulled_today,
            "pulled_week": pulled_week,
            "pulled_delta_pct": pulled_delta_pct,
            "success_rate": success_rate,
            "success_done": success_done,
            "success_total": success_total,
            "success_delta_pts": success_delta_pts,
            "queue_count": queue_count,
            "queue_oldest_seconds": queue_oldest_seconds,
            "avg_queue_seconds": avg_queue_seconds,
            "avg_queue_delta_seconds": avg_queue_delta_seconds,
            "failed_count": failed_count,
            "failed_latest_ref": failed_latest_ref,
            "failed_delta": failed_delta,
            "last_webhook_seconds": last_webhook_seconds,
            "last_webhook_topic": last_webhook_topic,
        }

    # ---- engines ------------------------------------------------------

    def _engines(self, jobs, logs, icp, today_start, day_ago, disp):
        engines = []
        for key, label in QUEUE_ENGINES:
            engines.append(self._queue_engine(jobs, key, label, today_start, disp))
        engines.append(self._address_engine(jobs, icp, today_start, disp))
        engines.append(
            self._fulfillment_engine(jobs, logs, icp, today_start, day_ago, disp)
        )
        for key, label, flag, source in CRON_ENGINES:
            engines.append(
                self._cron_engine(
                    logs, icp, key, label, flag, source, today_start, day_ago, disp
                )
            )
        return engines

    def _address_engine(self, jobs, icp, today_start, disp):
        """Address push/pull tile (tags/charges are separate job types)."""
        raw = icp.get_param(PARAM_PREFIX + "address_propagation_enabled")
        enabled = not (
            raw is not None and str(raw).strip().lower() in ("false", "0")
        )
        if not enabled:
            return {
                "key": "address",
                "label": "Address Sync",
                "status": "off",
                "meta": "disabled",
            }
        failed = jobs.search_count(
            [("job_type", "in", ADDRESS_JOB_TYPES), ("state", "=", "failed")]
        )
        today_count = jobs.search_count(
            [
                ("job_type", "in", ADDRESS_JOB_TYPES),
                ("create_date", ">=", today_start),
            ]
        )
        latest = jobs.search(
            [("job_type", "in", ADDRESS_JOB_TYPES)],
            order="create_date desc",
            limit=1,
        )
        status = "fail" if failed else "ok"
        meta = (
            "last {} · {} today".format(disp(latest.create_date, "%H:%M"), today_count)
            if latest
            else "waiting for first address change"
        )
        return {
            "key": "address",
            "label": "Address Sync",
            "status": status,
            "meta": meta,
        }

    def _fulfillment_engine(self, jobs, logs, icp, today_start, day_ago, disp):
        """Fulfillment tile: combines cron log status with pull job queue."""
        enabled = (icp.get_param(PARAM_PREFIX + "fulfillment_sync_enabled") or "False") == "True"
        if not enabled:
            return {"key": "fulfillment", "label": "Fulfillment Sync", "status": "off", "meta": "disabled"}

        direction = (icp.get_param(PARAM_PREFIX + "fulfillment_sync_direction") or "odoo_to_shopify").strip()
        parts = []
        if direction in ("odoo_to_shopify", "two_way"):
            parts.append("push")
        if direction in ("shopify_to_odoo", "two_way"):
            parts.append("pull")
        label = "Fulfillment Sync (%s)" % " + ".join(parts) if parts else "Fulfillment Sync"

        errors = logs.search_count([
            ("source", "=", "fulfillment"), ("level", "=", "error"),
            ("create_date", ">=", day_ago),
        ])
        warnings = logs.search_count([
            ("source", "=", "fulfillment"), ("level", "=", "warning"),
            ("create_date", ">=", day_ago),
        ])
        pull_failed = jobs.search_count([
            ("job_type", "=", "fulfillment_pull"), ("state", "=", "failed"),
        ])
        pull_pending = jobs.search_count([
            ("job_type", "=", "fulfillment_pull"), ("state", "=", "pending"),
        ])
        pull_today = jobs.search_count([
            ("job_type", "=", "fulfillment_pull"),
            ("create_date", ">=", today_start),
        ])

        status = "fail" if (errors or pull_failed) else ("warn" if warnings else "ok")

        log_today = logs.search_count([
            ("source", "=", "fulfillment"), ("create_date", ">=", today_start),
        ])
        latest = logs.search(
            [("source", "=", "fulfillment")], order="create_date desc", limit=1,
        )
        meta_parts = []
        if latest:
            meta_parts.append("last %s" % disp(latest.create_date, "%H:%M"))
        if pull_pending:
            meta_parts.append("%d queued" % pull_pending)
        if pull_today:
            meta_parts.append("%d pulled today" % pull_today)
        if log_today:
            meta_parts.append("%d logs today" % log_today)
        meta = " · ".join(meta_parts) if meta_parts else "never run"

        return {"key": "fulfillment", "label": label, "status": status, "meta": meta}

    def _queue_engine(self, jobs, key, label, today_start, disp):
        failed = jobs.search_count([("job_type", "=", key), ("state", "=", "failed")])
        today_count = jobs.search_count(
            [("job_type", "=", key), ("create_date", ">=", today_start)]
        )
        if failed:
            status = "fail"
        elif today_count:
            status = "ok"
        else:
            status = "off"
        latest = jobs.search(
            [("job_type", "=", key)], order="create_date desc", limit=1
        )
        meta = (
            "last {} · {} today".format(disp(latest.create_date, "%H:%M"), today_count)
            if latest
            else "never run"
        )
        return {"key": key, "label": label, "status": status, "meta": meta}

    def _cron_engine(
        self, logs, icp, key, label, flag, source, today_start, day_ago, disp
    ):
        enabled = (icp.get_param(PARAM_PREFIX + flag) or "False") == "True"
        if not enabled:
            return {"key": key, "label": label, "status": "off", "meta": "disabled"}
        errors = logs.search_count(
            [
                ("source", "=", source),
                ("level", "=", "error"),
                ("create_date", ">=", day_ago),
            ]
        )
        warnings = logs.search_count(
            [
                ("source", "=", source),
                ("level", "=", "warning"),
                ("create_date", ">=", day_ago),
            ]
        )
        status = "fail" if errors else ("warn" if warnings else "ok")
        today_count = logs.search_count(
            [("source", "=", source), ("create_date", ">=", today_start)]
        )
        latest = logs.search(
            [("source", "=", source)], order="create_date desc", limit=1
        )
        meta = (
            "last {} · {} today".format(disp(latest.create_date, "%H:%M"), today_count)
            if latest
            else "never run"
        )
        return {"key": key, "label": label, "status": status, "meta": meta}

    # ---- jobs ---------------------------------------------------------

    @staticmethod
    def _job_order_ref(job):
        """Friendly order reference for display: the Shopify order name
        (#1011) when the payload carries it, else the job name."""
        try:
            raw = job.payload_dict() if hasattr(job, "payload_dict") else {}
        except Exception:  # noqa: BLE001
            raw = {}
        raw_payload = raw.get("raw") or {}
        name = (
            raw_payload.get("name")
            or raw.get("shopify_order_name")
            or raw.get("order_name")
            or ""
        )
        if name:
            return name
        return job.name or ""

    @staticmethod
    def _jobs_domain(jobs_q):
        """Domain for dashboard job list / search (order #, id, SKU, text)."""
        q = (jobs_q or "").strip()
        if not q:
            return []
        # Strip leading # so "#14655" and "14655" both match.
        bare = q[1:] if q.startswith("#") else q
        terms = [q]
        if bare and bare != q:
            terms.append(bare)
        clauses = []
        for term in terms:
            like = "%%%s%%" % term.replace("%", "\\%")
            clauses.extend(
                [
                    ("name", "ilike", like),
                    ("payload", "ilike", like),
                    ("error", "ilike", like),
                    ("job_type", "ilike", like),
                ]
            )
        return ["|"] * (len(clauses) - 1) + clauses if clauses else []

    def _jobs(self, jobs, disp, limit=50, jobs_q=None):
        records = jobs.search(
            self._jobs_domain(jobs_q),
            order="create_date desc, id desc",
            limit=limit,
        )
        result = []
        for job in records:
            duration = ""
            if job.create_date and job.processed_date:
                seconds = (job.processed_date - job.create_date).total_seconds()
                duration = self._format_duration(seconds)
            result.append(
                {
                    "id": job.id,
                    "time": disp(job.create_date, "%H:%M"),
                    "type": job.job_type or "",
                    "name": job.name or "",
                    "order_ref": self._job_order_ref(job),
                    "state": job.state or "",
                    "attempts": job.attempts or 0,
                    "duration": duration,
                    "error": job.error or "",
                    "payload_preview": (job.payload or "")[:600],
                }
            )
        return result

    @staticmethod
    def _format_duration(seconds):
        if seconds < 0:
            return ""
        if seconds < 60:
            return "{:.1f}s".format(seconds)
        if seconds < 3600:
            return "{}m {:02d}s".format(int(seconds // 60), int(seconds % 60))
        return "{}h {:02d}m".format(int(seconds // 3600), int((seconds % 3600) // 60))

    # ---- products --------------------------------------------------------

    def _products(self, env, disp):
        """Product sync status rows for the dashboard: linked products first,
        then by recent write; 25 max."""
        Product = env["product.product"].sudo()
        records = Product.search(
            [("default_code", "!=", False), ("sale_ok", "=", True)],
            order="shopify_variant_id desc, write_date desc, id desc",
            limit=25,
        )
        result = []
        for product in records:
            result.append(
                {
                    "id": product.id,
                    "name": product.display_name,
                    "sku": product.default_code or "",
                    "linked": bool(product.shopify_variant_id),
                    "price": product.lst_price,
                    "price_pushed": product.shopify_last_pushed_price,
                    "qty": product.free_qty,
                    "qty_pushed": product.shopify_last_pushed_qty,
                    "synced_at": disp(product.write_date, "%H:%M"),
                }
            )
        return result

    # ---- webhooks -----------------------------------------------------

    def _webhooks(self, icp, now):
        result = []
        for topic in WEBHOOK_TOPICS:
            param = PARAM_PREFIX + "last_webhook_" + topic.replace("/", "_")
            received = _parse_iso(icp.get_param(param))
            result.append(
                {
                    "topic": topic,
                    "active": received is not None,
                    "last_received_seconds": (
                        max(0, int((now - received).total_seconds()))
                        if received
                        else None
                    ),
                }
            )
        return result

    # ---- product push ------------------------------------------------------

    @http.route(
        "/shopify_order_ops/product/<int:product_id>/push",
        type="http",
        auth="user",
        methods=["POST"],
        csrf=False,
    )
    def product_push(self, product_id, **kwargs):
        """Dashboard 'Push now' per product: immediate single-product push."""
        try:
            product = request.env["product.product"].sudo().browse(product_id)
            if not product.exists():
                return Response(
                    json.dumps({"ok": False, "error": "product not found"}),
                    status=404,
                    content_type="application/json",
                )
            message = request.env["shopify.product.sync"].push_single_product(product)
            return Response(
                json.dumps({"ok": True, "message": message}),
                content_type="application/json",
            )
        except Exception as exc:  # noqa: BLE001 - surface message to the UI
            return Response(
                json.dumps({"ok": False, "error": str(exc)}),
                status=400,
                content_type="application/json",
            )

    # ---- job retry ------------------------------------------------------

    @http.route(
        "/shopify_order_ops/job/<int:job_id>/retry",
        type="http",
        auth="user",
        methods=["POST"],
        csrf=False,
    )
    def job_retry(self, job_id, **kwargs):
        """Requeue a failed/pending job from the dashboard 'Retry now' button."""
        job = request.env["shopify.sync.job"].sudo().browse(job_id)
        if not job.exists():
            return Response(
                json.dumps({"ok": False, "error": "job not found"}),
                status=404,
                content_type="application/json",
            )
        job.write({"state": "pending", "error": False, "attempts": 0})
        request.env["shopify.sync.log"].sudo().log_event(
            "info",
            "Job %s manually requeued from the dashboard." % job.name,
            source="dashboard",
            job=job,
        )
        return Response(json.dumps({"ok": True}), content_type="application/json")

    # ---- job clear ------------------------------------------------------

    @http.route(
        "/shopify_order_ops/job/<int:job_id>/clear",
        type="http",
        auth="user",
        methods=["POST"],
        csrf=False,
    )
    def job_clear(self, job_id, **kwargs):
        """Remove a job from the queue via the dashboard 'Clear' button."""
        job = request.env["shopify.sync.job"].sudo().browse(job_id)
        if not job.exists():
            return Response(
                json.dumps({"ok": False, "error": "job not found"}),
                status=404,
                content_type="application/json",
            )
        if job.state == "processing":
            return Response(
                json.dumps({"ok": False, "error": "job is currently processing"}),
                status=400,
                content_type="application/json",
            )
        request.env["shopify.sync.log"].sudo().log_event(
            "info",
            "Job %s cleared from the dashboard by %s."
            % (job.name, request.env.user.name),
            source="dashboard",
            job=job,
        )
        job.unlink()
        return Response(json.dumps({"ok": True}), content_type="application/json")

    # ---- events -------------------------------------------------------

    IMPORTANT_SOURCES = (
        "order_pull",
        "order_edit",
        "order_cancel",
        "order_refund",
        "order_address_sync",
        "order_address_push",
        "order_tag_sync",
        "order_charge_sync",
        "order_qty_sync",
        "order_discount_push",
        "webhook",
        "dashboard",
    )

    def _events(self, logs, disp):
        # The feed favors meaningful events: every warning/error, plus all
        # order-lifecycle/webhook/dashboard entries. Routine cron heartbeat
        # lines stay in the log but don't crowd out the feed.
        records = logs.search(
            [
                "|",
                ("level", "in", ("warning", "error")),
                ("source", "in", list(self.IMPORTANT_SOURCES)),
            ],
            order="create_date desc, id desc",
            limit=10,
        )
        return [
            {
                "time": disp(rec.create_date, "%H:%M:%S"),
                "level": rec.level or "info",
                "source": rec.source or "",
                "message": rec.message or "",
            }
            for rec in records
        ]
