# -*- coding: utf-8 -*-
from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    shopify_auto_create_invoice = fields.Boolean(
        string="Auto-Create Invoice for Shopify Orders",
        config_parameter='corvex_shopify_connector.auto_create_invoice',
    )
    shopify_auto_register_payment = fields.Boolean(
        string="Auto-Register Payment",
        config_parameter='corvex_shopify_connector.auto_register_payment',
    )
    shopify_payment_journal_id = fields.Many2one(
        'account.journal',
        string="Shopify Payment Journal",
        domain="[('type', 'in', ('bank', 'cash'))]",
    )

    def set_values(self):
        super().set_values()
        self.env['ir.config_parameter'].sudo().set_param(
            'corvex_shopify_connector.payment_journal_id',
            str(self.shopify_payment_journal_id.id) if self.shopify_payment_journal_id else '0',
        )

    @api.model
    def get_values(self):
        res = super().get_values()
        journal_id = int(self.env['ir.config_parameter'].sudo().get_param(
            'corvex_shopify_connector.payment_journal_id', '0'
        ))
        if journal_id:
            journal = self.env['account.journal'].browse(journal_id).exists()
            res['shopify_payment_journal_id'] = journal.id if journal else False
        return res
