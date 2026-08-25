"""Shopify HTTPS webhook HMAC helpers. No Odoo imports."""

from __future__ import annotations

import base64
import hashlib
import hmac


def normalize_webhook_secret(secret):
    secret = (secret or "").strip().strip("\ufeff")
    if len(secret) >= 2 and secret[0] == secret[-1] and secret[0] in "\"'":
        secret = secret[1:-1].strip()
    return secret


def shopify_hmac_from_headers(headers, environ=None):
    """Return the Shopify HMAC header value, or empty string."""
    environ = environ or {}
    received = (
        (headers.get("X-Shopify-Hmac-Sha256") if headers else None)
        or (headers.get("X-Shopify-Hmac-SHA256") if headers else None)
        or environ.get("HTTP_X_SHOPIFY_HMAC_SHA256")
        or ""
    )
    if isinstance(received, bytes):
        received = received.decode("utf-8")
    return (received or "").strip()


def raw_http_body(httprequest):
    """Original POST bytes Shopify signed. Prefer get_data over .data."""
    data = b""
    if httprequest is None:
        return data
    getter = getattr(httprequest, "get_data", None)
    if callable(getter):
        try:
            data = getter(cache=True, as_text=False)
        except TypeError:
            data = getter(cache=True)
    if not data:
        data = getattr(httprequest, "data", None) or b""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return data or b""


def shopify_delivery_meta(headers):
    """Safe Shopify delivery headers for logs (no HMAC, no body)."""
    if not headers:
        return {}
    return {
        "shop": (headers.get("X-Shopify-Shop-Domain") or "").strip(),
        "webhook_id": (headers.get("X-Shopify-Webhook-Id") or "").strip(),
        "event_id": (headers.get("X-Shopify-Event-Id") or "").strip(),
        "api_version": (headers.get("X-Shopify-API-Version") or "").strip(),
    }


def _digest_bytes(secret, raw_body):
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()


def _hmac_candidates(digest):
    """Shopify documents standard base64; some proxies/docs use urlsafe or hex."""
    b64 = base64.b64encode(digest).decode("utf-8")
    url = base64.urlsafe_b64encode(digest).decode("utf-8")
    return (
        b64,
        url,
        b64.rstrip("="),
        url.rstrip("="),
        digest.hex(),
    )


def webhook_hmac_matches(secret, raw_body, received_hmac):
    secret = normalize_webhook_secret(secret)
    received_hmac = (received_hmac or "").strip()
    if not secret or not received_hmac:
        return False
    if isinstance(raw_body, str):
        raw_body = raw_body.encode("utf-8")
    raw_body = raw_body or b""
    bodies = [raw_body]
    if raw_body.startswith(b"\xef\xbb\xbf"):
        bodies.append(raw_body[3:])
    stripped = raw_body.rstrip(b"\r\n")
    if stripped != raw_body:
        bodies.append(stripped)
    received_l = received_hmac.lower()
    for body in bodies:
        digest = _digest_bytes(secret, body)
        for candidate in _hmac_candidates(digest):
            if len(candidate) != len(received_hmac):
                continue
            if hmac.compare_digest(candidate, received_hmac):
                return True
            if hmac.compare_digest(candidate.lower(), received_l):
                return True
    return False


def hmac_mismatch_detail(secret, raw_body, received_hmac, headers=None):
    """Human-readable reason for a reject log (no secrets)."""
    body_len = len(raw_body or b"")
    header = "present" if (received_hmac or "").strip() else "MISSING"
    secret_len = len(normalize_webhook_secret(secret) or "")
    parts = [
        "body %s bytes" % body_len,
        "hmac header %s" % header,
        "secret %s chars" % secret_len,
    ]
    meta = shopify_delivery_meta(headers)
    if meta.get("shop"):
        parts.append("shop %s" % meta["shop"])
    if meta.get("api_version"):
        parts.append("api %s" % meta["api_version"])
    if meta.get("webhook_id"):
        parts.append("webhook_id %s" % meta["webhook_id"])
    return ", ".join(parts)
