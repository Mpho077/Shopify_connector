# -*- coding: utf-8 -*-
from odoo import fields, models


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    is_pickup = fields.Boolean(string="Is Pickup", copy=False)
