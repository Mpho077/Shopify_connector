# -*- coding: utf-8 -*-
import logging
from datetime import timedelta

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)


class ShopifyErrorNotification(models.Model):
    _name = 'shopify.error.notification'
    _description = 'Shopify Error Notification Config'
    _order = 'shopify_instance_id'

    shopify_instance_id = fields.Many2one(
        'shopify.instance.ept', string="Instance",
        required=True, ondelete='cascade',
    )
    user_ids = fields.Many2many(
        'res.users', 'shopify_error_notification_user_rel',
        'notification_id', 'user_id',
        string="Users to Notify",
        help="These users will receive email and Odoo notifications when errors occur.",
    )
    notify_on_error = fields.Boolean(
        string="Notify on Errors", default=True,
    )
    notify_on_warning = fields.Boolean(
        string="Notify on Warnings", default=False,
    )
    resend_interval_minutes = fields.Integer(
        string="Resend Interval (minutes)", default=30,
        help="Resend notifications every X minutes until the error is acknowledged.",
    )
    last_notified = fields.Datetime(
        string="Last Notification Sent", readonly=True,
    )
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ('instance_uniq', 'unique(shopify_instance_id)',
         'Only one notification config per instance is allowed.'),
    ]

    # ------------------------------------------------------------------
    # CRON — Scan for failed orders & queue errors, then notify
    # ------------------------------------------------------------------
    @api.model
    def _cron_check_errors(self):
        """Scan every active notification config for new errors and notify."""
        configs = self.search([('active', '=', True)])
        for config in configs:
            try:
                config._check_and_notify()
                config.env.cr.commit()
            except Exception:
                _logger.exception(
                    'Error notification check failed for instance %s',
                    config.shopify_instance_id.name,
                )
                config.env.cr.rollback()

    def _check_and_notify(self):
        self.ensure_one()
        if not self.user_ids:
            return

        # Respect resend interval
        now = fields.Datetime.now()
        if self.last_notified:
            next_allowed = self.last_notified + timedelta(minutes=self.resend_interval_minutes)
            if now < next_allowed:
                return

        instance = self.shopify_instance_id
        problems = []

        # 1) Failed order queue lines (orders that came via webhook but failed to import)
        failed_queue_lines = self._find_failed_order_queue_lines(instance)
        if failed_queue_lines:
            problems.append({
                'category': 'Failed Order Imports',
                'count': len(failed_queue_lines),
                'details': failed_queue_lines,
            })

        # 2) Common log book errors (Emipro's generic error log)
        if self.notify_on_error:
            error_logs = self._find_log_errors(instance, 'error')
            if error_logs:
                problems.append({
                    'category': 'Sync Errors',
                    'count': len(error_logs),
                    'details': error_logs,
                })

        # 3) Common log book warnings
        if self.notify_on_warning:
            warning_logs = self._find_log_errors(instance, 'warning')
            if warning_logs:
                problems.append({
                    'category': 'Sync Warnings',
                    'count': len(warning_logs),
                    'details': warning_logs,
                })

        # 4) Stuck queues (draft for more than 2 hours = likely stuck)
        stuck_queues = self._find_stuck_queues(instance)
        if stuck_queues:
            problems.append({
                'category': 'Stuck Queues (draft > 2 hours)',
                'count': len(stuck_queues),
                'details': stuck_queues,
            })

        if not problems:
            return

        # Send notifications
        self._send_notifications(problems)
        self.write({'last_notified': now})

    def _find_failed_order_queue_lines(self, instance):
        """Find order data queue lines that failed to process."""
        OrderQueueLine = self.env['shopify.order.data.queue.line.ept']
        failed_lines = OrderQueueLine.search([
            ('shopify_instance_id', '=', instance.id),
            ('state', '=', 'failed'),
        ], limit=50)
        results = []
        for line in failed_lines:
            results.append({
                'name': line.shopify_order_id or line.name or 'Unknown',
                'message': line.common_log_lines_ids[:1].message if line.common_log_lines_ids else 'No error message',
                'date': str(line.write_date or ''),
            })
        return results

    def _find_log_errors(self, instance, log_type='error'):
        """Find recent unresolved log lines from Emipro's common log."""
        LogLine = self.env['common.log.lines.ept']
        cutoff = fields.Datetime.now() - timedelta(hours=24)
        lines = LogLine.search([
            ('shopify_instance_id', '=', instance.id),
            ('log_line_type', '=', 'fail' if log_type == 'error' else 'warning'),
            ('write_date', '>=', cutoff),
        ], limit=50)
        results = []
        for line in lines:
            results.append({
                'name': line.log_book_id.name if line.log_book_id else 'Log',
                'message': (line.message or '')[:200],
                'date': str(line.write_date or ''),
            })
        return results

    def _find_stuck_queues(self, instance):
        """Find order queues stuck in draft state for more than 2 hours."""
        OrderQueue = self.env['shopify.order.data.queue.ept']
        cutoff = fields.Datetime.now() - timedelta(hours=2)
        stuck = OrderQueue.search([
            ('shopify_instance_id', '=', instance.id),
            ('state', '=', 'draft'),
            ('create_date', '<=', cutoff),
        ], limit=50)
        results = []
        for q in stuck:
            results.append({
                'name': q.name or f'Queue #{q.id}',
                'message': f'Created {q.create_date}, still in draft',
                'date': str(q.create_date or ''),
            })
        return results

    # ------------------------------------------------------------------
    # Notification Dispatch
    # ------------------------------------------------------------------
    def _send_notifications(self, problems):
        """Send email and Odoo bus notifications to configured users."""
        self.ensure_one()
        instance_name = self.shopify_instance_id.name

        # Build summary
        total_issues = sum(p['count'] for p in problems)
        subject = _("⚠ Shopify Alert: %d issue(s) on %s") % (total_issues, instance_name)

        body_lines = [
            f"<h3>Shopify Error Alert — {instance_name}</h3>",
            f"<p><strong>{total_issues} issue(s)</strong> detected that need attention:</p>",
        ]
        for problem in problems:
            body_lines.append(
                f"<h4>{problem['category']} ({problem['count']})</h4><ul>"
            )
            for detail in problem['details'][:10]:
                body_lines.append(
                    f"<li><strong>{detail['name']}</strong>: "
                    f"{detail['message']} "
                    f"<em>({detail['date']})</em></li>"
                )
            if problem['count'] > 10:
                body_lines.append(f"<li><em>...and {problem['count'] - 10} more</em></li>")
            body_lines.append("</ul>")

        body_lines.append(
            "<p>Log in to Odoo to review and resolve these issues.</p>"
        )
        body_html = "\n".join(body_lines)

        # Send email via mail.mail
        for user in self.user_ids:
            if not user.email:
                continue
            mail_values = {
                'subject': subject,
                'body_html': body_html,
                'email_to': user.email,
                'email_from': self.env.company.email or 'noreply@odoo.com',
                'auto_delete': True,
            }
            self.env['mail.mail'].sudo().create(mail_values).send()

        # Send Odoo internal notification (bus)
        for user in self.user_ids:
            self.env['bus.bus']._sendone(
                user.partner_id,
                'simple_notification',
                {
                    'title': _("Shopify Alert: %s", instance_name),
                    'message': _("%d issue(s) found. Check your email for details.") % total_issues,
                    'type': 'danger',
                    'sticky': True,
                },
            )
