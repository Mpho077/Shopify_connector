from odoo import models
from odoo.http import request

WEBHOOK_PREFIX = "/shopify_order_ops/webhook"


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    @classmethod
    def _pre_dispatch(cls, rule, args):
        # Capture the raw POST before HttpDispatcher reads form/json data.
        # HMAC must use the exact bytes Shopify signed.
        httprequest = request.httprequest
        path = httprequest.path or ""
        if path.startswith(WEBHOOK_PREFIX):
            getter = getattr(httprequest, "get_data", None)
            raw = b""
            if callable(getter):
                try:
                    raw = getter(cache=True, as_text=False)
                except TypeError:
                    raw = getter(cache=True)
            if not raw:
                raw = getattr(httprequest, "data", None) or b""
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            request.shopify_webhook_raw_body = raw or b""
        return super()._pre_dispatch(rule, args)
