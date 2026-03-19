# -*- coding: utf-8 -*-
import logging
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class ShopifyInstanceInherit(models.Model):
    _inherit = 'shopify.instance.ept'

    # ------------------------------------------------------------------
    # Dashboard data provider (called from our OWL dashboard component)
    # ------------------------------------------------------------------
    def get_dashboard_data(self, period='current_week'):
        """Return KPI data for the Shopify dashboard.

        :param period: 'current_week', 'current_month', or 'current_year'
        :returns: dict with dashboard metrics
        """
        self.ensure_one()
        cr = self.env.cr
        instance_id = self.id
        now = fields.Datetime.now()

        # Determine date range for the selected period
        if period == 'current_month':
            start = now.replace(day=1, hour=0, minute=0, second=0)
            prev_start = (start - timedelta(days=1)).replace(day=1)
            prev_end = start
        elif period == 'current_year':
            start = now.replace(month=1, day=1, hour=0, minute=0, second=0)
            prev_start = start.replace(year=start.year - 1)
            prev_end = start
        else:  # current_week
            start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0)
            prev_start = start - timedelta(weeks=1)
            prev_end = start

        # Total sales & order count for the period
        cr.execute("""
            SELECT COALESCE(SUM(amount_total), 0), COUNT(*)
            FROM sale_order
            WHERE shopify_instance_id = %s AND state IN ('sale', 'done')
              AND date_order >= %s AND date_order <= %s
        """, (instance_id, start, now))
        total_sales, order_count_period = cr.fetchone()

        # Previous period sales
        cr.execute("""
            SELECT COALESCE(SUM(amount_total), 0)
            FROM sale_order
            WHERE shopify_instance_id = %s AND state IN ('sale', 'done')
              AND date_order >= %s AND date_order < %s
        """, (instance_id, prev_start, prev_end))
        prev_sales = cr.fetchone()[0]

        pct_change = 0
        if prev_sales:
            pct_change = ((total_sales - prev_sales) / prev_sales) * 100

        avg_order = total_sales / order_count_period if order_count_period else 0

        # All-time counts
        cr.execute("""
            SELECT COUNT(*) FROM sale_order
            WHERE shopify_instance_id = %s AND state IN ('sale', 'done')
        """, (instance_id,))
        order_count = cr.fetchone()[0]

        # Product count
        cr.execute("""
            SELECT COUNT(*) FROM shopify_product_template_ept
            WHERE shopify_instance_id = %s
        """, (instance_id,))
        product_count = cr.fetchone()[0]

        # Customer count (via Emipro's bridge model)
        cr.execute("""
            SELECT COUNT(DISTINCT partner_id) FROM shopify_res_partner_ept
            WHERE shopify_instance_id = %s
        """, (instance_id,))
        customer_count = cr.fetchone()[0]

        # Shipped order count
        cr.execute("""
            SELECT COUNT(*) FROM stock_picking
            WHERE shopify_instance_id = %s AND updated_in_shopify = TRUE
        """, (instance_id,))
        shipped_order_count = cr.fetchone()[0]

        # Refund count
        cr.execute("""
            SELECT COUNT(*) FROM account_move
            WHERE shopify_instance_id = %s AND move_type = 'out_refund'
        """, (instance_id,))
        refund_count = cr.fetchone()[0]

        # Unacknowledged error count (Emipro common log lines)
        unacknowledged_error_count = 0
        try:
            cr.execute("""
                SELECT COUNT(*) FROM common_log_lines_ept cll
                JOIN common_log_book_ept clb ON clb.id = cll.log_book_id
                WHERE clb.shopify_instance_id = %s AND clb.log_type = 'error'
            """, (instance_id,))
            unacknowledged_error_count = cr.fetchone()[0]
        except Exception:
            cr.execute("ROLLBACK TO SAVEPOINT dashboard_err_count")

        # Queue counts (across all Emipro queue types)
        queue_draft = queue_progress = queue_failed = 0
        for table in ('shopify_order_data_queue_ept', 'shopify_product_data_queue',
                       'shopify_customer_data_queue_ept', 'shopify_export_stock_queue_ept'):
            try:
                cr.execute("""
                    SELECT state, COUNT(*) FROM {} WHERE shopify_instance_id = %s
                    GROUP BY state
                """.format(table), (instance_id,))
                for state, count in cr.fetchall():
                    if state == 'draft':
                        queue_draft += count
                    elif state in ('partially_completed', 'processing'):
                        queue_progress += count
                    elif state == 'failed':
                        queue_failed += count
            except Exception:
                pass

        return {
            'instance_name': self.name,
            'total_sales': total_sales,
            'avg_order_value': avg_order,
            'percentage_change': pct_change,
            'order_count_period': order_count_period,
            'product_count': product_count,
            'customer_count': customer_count,
            'order_count': order_count,
            'shipped_order_count': shipped_order_count,
            'refund_count': refund_count,
            'unacknowledged_error_count': unacknowledged_error_count,
            'queue_draft': queue_draft,
            'queue_progress': queue_progress,
            'queue_failed': queue_failed,
        }
