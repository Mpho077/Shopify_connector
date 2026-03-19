# -*- coding: utf-8 -*-
import logging

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    is_pickup = fields.Boolean(string="Is Pickup Order", copy=False)

    # ----------------------------------------------------------------
    # Auto-Invoice for Shopify Orders
    # ----------------------------------------------------------------
    def action_confirm(self):
        res = super().action_confirm()
        for order in self:
            order._shopify_auto_invoice()
        return res

    def _shopify_auto_invoice(self):
        """Auto-create invoice and register payment for Shopify orders."""
        ICP = self.env['ir.config_parameter'].sudo()
        if ICP.get_param('corvex_shopify_connector.auto_create_invoice') != 'True':
            return
        if self.state != 'sale':
            return
        if not self.shopify_instance_id:
            return
        try:
            invoice = self._create_invoices()
            invoice.action_post()
            if ICP.get_param('corvex_shopify_connector.auto_register_payment') == 'True':
                journal_id = int(ICP.get_param('corvex_shopify_connector.payment_journal_id', '0'))
                if journal_id:
                    journal = self.env['account.journal'].browse(journal_id).exists()
                    if journal:
                        payment_register = self.env['account.payment.register'].with_context(
                            active_model='account.move',
                            active_ids=invoice.ids,
                        ).create({'journal_id': journal.id})
                        payment_register.action_create_payments()
                    else:
                        _logger.warning('Shopify auto-payment: journal %d not found', journal_id)
                else:
                    _logger.warning('Shopify auto-payment: no payment journal configured')
        except Exception as e:
            _logger.exception('Auto-invoice failed for order %s: %s', self.name, e)
