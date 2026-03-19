# -*- coding: utf-8 -*-
import logging

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
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ('instance_uniq', 'unique(shopify_instance_id)',
         'Only one notification config per instance is allowed.'),
    ]
