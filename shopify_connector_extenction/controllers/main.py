# -*- coding: utf-8 -*-
import base64
import hashlib
import hmac
import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class ShopifyPriceWebhookController(http.Controller):

    @http.route(
        '/shopify/price_webhook/<int:sync_id>',
        type='http', methods=['POST'], auth='public', csrf=False,
    )
    def price_webhook(self, sync_id, **kwargs):
        """Receive real-time price updates from Shopify for a price-sync record."""
        body = request.httprequest.data
        received_hmac = request.httprequest.headers.get('X-Shopify-Hmac-Sha256', '')

        sync_rec = request.env['shopify.price.sync'].sudo().browse(sync_id)
        if not sync_rec.exists() or not sync_rec.active:
            return request.make_response('Not Found', status=404)

        if sync_rec.webhook_secret:
            if not self._verify_hmac(body, sync_rec.webhook_secret, received_hmac):
                _logger.warning('Shopify price webhook: HMAC failed for sync_id=%d', sync_id)
                return request.make_response('Unauthorized', status=401)

        try:
            data = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            return request.make_response('Bad Request', status=400)

        try:
            sync_rec._process_product_webhook(data)
        except Exception:
            _logger.exception('Shopify price webhook error for sync_id=%d', sync_id)

        return request.make_response('OK', status=200)

    @staticmethod
    def _verify_hmac(body_bytes, secret, received_hmac):
        if not received_hmac:
            return False
        computed = base64.b64encode(
            hmac.new(secret.encode('utf-8'), body_bytes, hashlib.sha256).digest()
        ).decode('utf-8')
        return hmac.compare_digest(computed, received_hmac)
