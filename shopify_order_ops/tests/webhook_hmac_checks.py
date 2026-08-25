"""Standalone checks for Shopify webhook HMAC helpers.

Run: python tests/webhook_hmac_checks.py
Odoo will not auto-discover this file (no test_ prefix).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "shopify_webhook_hmac",
    ROOT / "models" / "shopify_webhook_hmac.py",
)
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)


def check(name, cond, detail=""):
    if not cond:
        raise SystemExit("FAIL %s %s" % (name, detail))
    print("OK", name)


class FakeRequest:
    def __init__(self, body):
        self._body = body
        self.data = body

    def get_data(self, cache=True, as_text=False):
        if as_text:
            return self._body.decode("utf-8")
        return self._body


def main():
    secret = "shpss_test_secret"
    body = b'{"id":1011,"email":"a@b.com"}'
    digest = base64.b64encode(
        hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    ).decode("utf-8")

    check("match", mod.webhook_hmac_matches(secret, body, digest))
    check("wrong secret", not mod.webhook_hmac_matches("other", body, digest))
    check("empty secret", not mod.webhook_hmac_matches("", body, digest))
    check("missing header", not mod.webhook_hmac_matches(secret, body, ""))
    check(
        "quoted secret",
        mod.webhook_hmac_matches('"%s"' % secret, body, digest),
    )
    check(
        "get_data body",
        mod.raw_http_body(FakeRequest(body)) == body,
    )
    check(
        "header from map",
        mod.shopify_hmac_from_headers({"X-Shopify-Hmac-Sha256": digest}) == digest,
    )
    hex_sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    check("hex header also accepted", mod.webhook_hmac_matches(secret, body, hex_sig))
    bom_body = b"\xef\xbb\xbf" + body
    check(
        "BOM-prefixed body still matches original signature",
        mod.webhook_hmac_matches(secret, bom_body, digest),
    )
    detail = mod.hmac_mismatch_detail(
        secret,
        b"",
        "",
        headers={"X-Shopify-Shop-Domain": "example-shop.myshopify.com"},
    )
    check("detail empty body", "body 0 bytes" in detail)
    check("detail missing header", "MISSING" in detail)
    check("detail includes shop", "example-shop.myshopify.com" in detail)

    print("\nAll standalone webhook HMAC checks passed.")


if __name__ == "__main__":
    main()
