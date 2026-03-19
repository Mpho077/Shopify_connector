# -*- coding: utf-8 -*-
import logging
import time

import requests

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30


class ShopifyPriceSync(models.Model):
    _name = 'shopify.price.sync'
    _description = 'Shopify Price Sync'
    _order = 'name'

    name = fields.Char(string="Name", required=True)
    active = fields.Boolean(default=True)
    shop_url = fields.Char(
        string="Shop URL", required=True,
        help="Your Shopify store URL, e.g. mystore.myshopify.com",
    )
    api_token = fields.Char(
        string="Admin API Access Token",
        help="From Shopify Admin > Settings > Apps > Develop apps",
        copy=False, groups='base.group_system',
    )
    api_version = fields.Char(
        string="API Version", default='2024-01',
    )
    pricelist_id = fields.Many2one(
        'product.pricelist', string="Pricelist",
        help="If set, prices will also be synced to this pricelist as fixed-price items",
    )
    update_list_price = fields.Boolean(
        string="Update Product Sale Price", default=True,
        help="Update the product's Sale Price (list_price) with the Shopify price",
    )
    last_sync = fields.Datetime(string="Last Price Sync", readonly=True)
    sync_log = fields.Text(string="Last Sync Log", readonly=True)
    company_id = fields.Many2one(
        'res.company', string="Company",
        default=lambda self: self.env.company,
    )
    webhook_secret = fields.Char(
        string="Webhook Secret",
        copy=False, groups='base.group_system',
        help="Secret key for verifying Shopify webhook signatures",
    )
    webhook_id = fields.Char(
        string="Shopify Webhook ID", readonly=True, copy=False,
    )
    webhook_url = fields.Char(
        string="Webhook URL", compute='_compute_webhook_url',
    )

    def _compute_webhook_url(self):
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
        for rec in self:
            rec.webhook_url = f"{base_url}/shopify/price_webhook/{rec.id}" if rec.id else ''

    # =====================
    # API CLIENT
    # =====================
    def _get_api_url(self, endpoint):
        self.ensure_one()
        shop = self.shop_url.strip().rstrip('/')
        if not shop.startswith('http'):
            shop = f"https://{shop}"
        version = self.api_version or '2024-01'
        return f"{shop}/admin/api/{version}/{endpoint}"

    def _get_headers(self):
        self.ensure_one()
        return {
            'X-Shopify-Access-Token': self.sudo().api_token,
            'Content-Type': 'application/json',
        }

    def _shopify_get(self, endpoint, params=None):
        """Make a GET request to Shopify API with rate-limit handling."""
        self.ensure_one()
        url = self._get_api_url(endpoint)
        headers = self._get_headers()

        try:
            resp = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as e:
            return {'error': str(e)}

        # Rate limit handling
        call_limit = resp.headers.get('X-Shopify-Shop-Api-Call-Limit', '')
        if call_limit:
            parts = call_limit.split('/')
            if len(parts) == 2 and int(parts[0]) / int(parts[1]) >= 0.9:
                time.sleep(1)

        if resp.status_code == 429:
            retry_after = float(resp.headers.get('Retry-After', 2))
            time.sleep(min(retry_after, 10))
            return self._shopify_get(endpoint, params=params)

        if resp.status_code >= 400:
            try:
                return resp.json()
            except Exception:
                return {'error': f'HTTP {resp.status_code}: {resp.text[:200]}'}

        return resp.json()

    def _shopify_get_all(self, endpoint, params=None):
        """Fetch all pages for a paginated Shopify endpoint."""
        self.ensure_one()
        import re
        all_items = []
        url = self._get_api_url(endpoint)
        headers = self._get_headers()
        params = dict(params or {})
        params.setdefault('limit', 250)

        while url:
            try:
                resp = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
            except requests.exceptions.RequestException as e:
                return {'error': str(e)}

            # Rate limit
            call_limit = resp.headers.get('X-Shopify-Shop-Api-Call-Limit', '')
            if call_limit:
                parts = call_limit.split('/')
                if len(parts) == 2 and int(parts[0]) / int(parts[1]) >= 0.9:
                    time.sleep(1)

            if resp.status_code == 429:
                retry_after = float(resp.headers.get('Retry-After', 2))
                time.sleep(min(retry_after, 10))
                continue

            if resp.status_code >= 400:
                try:
                    return resp.json()
                except Exception:
                    return {'error': f'HTTP {resp.status_code}'}

            data = resp.json()
            # Find the list key
            for key, val in data.items():
                if isinstance(val, list):
                    all_items.extend(val)
                    break

            # Pagination via Link header
            link_header = resp.headers.get('Link', '')
            match = re.search(r'<([^>]+)>;\s*rel="next"', link_header)
            if match:
                url = match.group(1)
                params = {}  # params are embedded in the next URL
            else:
                url = None

        # Return with the same key structure
        return {'products': all_items}

    # =====================
    # CONNECTION TEST
    # =====================
    def action_test_connection(self):
        self.ensure_one()
        if not self.api_token:
            raise UserError(_('Please enter your Shopify Admin API Access Token.'))

        result = self._shopify_get('shop.json')
        if 'error' in result or 'errors' in result:
            raise UserError(_('Connection failed: %s', result.get('error') or result.get('errors')))

        shop_name = result.get('shop', {}).get('name', self.shop_url)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Connection Successful'),
                'message': _('Connected to Shopify store: %s', shop_name),
                'type': 'success',
                'sticky': False,
            }
        }

    # =====================
    # PRICE SYNC
    # =====================
    def action_sync_prices(self):
        """Manual price sync trigger."""
        self.ensure_one()
        self._sync_prices()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Price Sync Complete'),
                'message': self.sync_log or 'Done',
                'type': 'success',
                'sticky': False,
            }
        }

    def _sync_prices(self):
        """Fetch all product prices from Shopify and update Odoo products by SKU."""
        self.ensure_one()
        result = self._shopify_get_all('products.json', params={'fields': 'id,variants'})

        if 'error' in result or 'errors' in result:
            msg = str(result.get('error') or result.get('errors'))
            self.write({'sync_log': f'Error: {msg}'})
            _logger.error('Shopify price sync failed: %s', msg)
            return

        products = result.get('products', [])
        updated = 0
        skipped = 0
        errors = 0

        for product_data in products:
            for variant in product_data.get('variants', []):
                sku = variant.get('sku')
                if not sku:
                    skipped += 1
                    continue

                price = float(variant.get('price', 0))
                compare_at_price = variant.get('compare_at_price')
                if compare_at_price:
                    compare_at_price = float(compare_at_price)

                # Find Odoo product by SKU
                odoo_product = self.env['product.product'].search(
                    [('default_code', '=', sku)], limit=1
                )
                if not odoo_product:
                    skipped += 1
                    continue

                try:
                    changed = False

                    # Update list_price on the product template
                    if self.update_list_price and odoo_product.lst_price != price:
                        odoo_product.lst_price = price
                        changed = True

                    # Update pricelist item
                    if self.pricelist_id:
                        self._sync_pricelist_item(odoo_product, price)
                        changed = True

                    if changed:
                        updated += 1

                except Exception as e:
                    _logger.exception('Price sync error for SKU %s: %s', sku, e)
                    errors += 1

        log_msg = f'Synced {updated} prices, skipped {skipped}, errors {errors} (from {len(products)} Shopify products)'
        self.write({
            'last_sync': fields.Datetime.now(),
            'sync_log': log_msg,
        })
        _logger.info('Shopify price sync: %s', log_msg)

    def _sync_pricelist_item(self, product, price):
        """Create or update a fixed-price pricelist item for the product."""
        if not self.pricelist_id:
            return

        PricelistItem = self.env['product.pricelist.item']
        existing = PricelistItem.search([
            ('pricelist_id', '=', self.pricelist_id.id),
            ('product_id', '=', product.id),
            ('compute_price', '=', 'fixed'),
        ], limit=1)

        if existing:
            if existing.fixed_price != price:
                existing.write({'fixed_price': price})
        else:
            PricelistItem.create({
                'pricelist_id': self.pricelist_id.id,
                'applied_on': '0_product_variant',
                'product_id': product.id,
                'product_tmpl_id': product.product_tmpl_id.id,
                'compute_price': 'fixed',
                'fixed_price': price,
                'min_quantity': 0,
            })

    # =====================
    # WEBHOOK
    # =====================
    def _shopify_post(self, endpoint, data):
        """Make a POST request to Shopify API."""
        self.ensure_one()
        url = self._get_api_url(endpoint)
        headers = self._get_headers()
        try:
            resp = requests.post(url, json=data, headers=headers, timeout=REQUEST_TIMEOUT)
            return resp.json() if resp.content else {}
        except requests.exceptions.RequestException as e:
            return {'error': str(e)}

    def _shopify_delete(self, endpoint):
        """Make a DELETE request to Shopify API."""
        self.ensure_one()
        url = self._get_api_url(endpoint)
        headers = self._get_headers()
        try:
            resp = requests.delete(url, headers=headers, timeout=REQUEST_TIMEOUT)
            return resp.status_code < 400
        except requests.exceptions.RequestException:
            return False

    def action_register_webhook(self):
        """Register a products/update webhook in Shopify."""
        self.ensure_one()
        if not self.api_token:
            raise UserError(_('Please enter your Shopify Admin API Access Token.'))

        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
        if not base_url:
            raise UserError(_('Web base URL is not configured. Go to Settings > Technical > System Parameters.'))

        callback_url = f"{base_url}/shopify/price_webhook/{self.id}"

        # Unregister existing webhook first
        if self.webhook_id:
            self._shopify_delete(f'webhooks/{self.webhook_id}.json')

        result = self._shopify_post('webhooks.json', {
            'webhook': {
                'topic': 'products/update',
                'address': callback_url,
                'format': 'json',
            }
        })

        if 'error' in result or 'errors' in result:
            raise UserError(_('Failed to register webhook: %s',
                              result.get('error') or result.get('errors')))

        webhook_data = result.get('webhook', {})
        self.webhook_id = str(webhook_data.get('id', ''))

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Webhook Registered'),
                'message': _('Shopify will now send real-time price updates to: %s', callback_url),
                'type': 'success',
                'sticky': False,
            }
        }

    def action_unregister_webhook(self):
        """Remove the products/update webhook from Shopify."""
        self.ensure_one()
        if self.webhook_id:
            self._shopify_delete(f'webhooks/{self.webhook_id}.json')
            self.webhook_id = False
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Webhook Removed'),
                'message': _('Real-time price sync webhook has been removed.'),
                'type': 'info',
                'sticky': False,
            }
        }

    def _process_product_webhook(self, data):
        """Process a products/update webhook payload and update prices."""
        self.ensure_one()
        variants = data.get('variants', [])
        updated = 0
        for variant in variants:
            sku = variant.get('sku')
            if not sku:
                continue
            price = float(variant.get('price', 0))
            odoo_product = self.env['product.product'].search(
                [('default_code', '=', sku)], limit=1
            )
            if not odoo_product:
                continue
            try:
                if self.update_list_price and odoo_product.lst_price != price:
                    odoo_product.lst_price = price
                if self.pricelist_id:
                    self._sync_pricelist_item(odoo_product, price)
                updated += 1
            except Exception:
                _logger.exception('Webhook price update error for SKU %s', sku)
        if updated:
            _logger.info('Shopify webhook: updated %d prices for sync_id=%d', updated, self.id)

    # =====================
    # CRON
    # =====================
    @api.model
    def _cron_sync_prices(self):
        """Cron job: sync prices for all active price sync records."""
        for rec in self.search([('active', '=', True)]):
            try:
                rec._sync_prices()
                rec.env.cr.commit()
            except Exception:
                _logger.exception('Price sync cron failed for %s', rec.name)
                rec.env.cr.rollback()
