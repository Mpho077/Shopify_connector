import json
import logging
import time

import requests

from odoo import models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

API_VERSION = "2025-01"
PARAM_PREFIX = "shopify_order_ops."


def normalize_shopify_variant_id(value):
    """Numeric REST variant id from Shopify line items or stored links."""
    if value in (None, False, ""):
        return False
    text = str(value).strip()
    if not text:
        return False
    if text.startswith("gid://"):
        text = text.rsplit("/", 1)[-1].strip()
    if text.isdigit():
        return text
    return False


class ShopifyApiClient(models.AbstractModel):
    """Thin wrapper around the Shopify Admin REST + GraphQL APIs.

    All configuration comes from ir.config_parameter keys prefixed with
    ``shopify_order_ops.`` (set via Settings -> Shopify Ops).
    """

    _name = "shopify.api.client"
    _description = "Shopify Admin API Client"

    # --- config helpers -------------------------------------------------
    def _param(self, key, default=None):
        return (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param(PARAM_PREFIX + key, default)
        )

    def _base_url(self):
        shop = (self._param("shop_domain") or "").strip()
        # Users paste full URLs all the time — normalize to bare domain.
        shop = shop.replace("https://", "").replace("http://", "")
        shop = shop.strip().strip("/").strip()
        if not shop:
            raise UserError(
                "Shopify shop domain is not configured (Settings -> Shopify Ops)."
            )
        return f"https://{shop}/admin/api/{API_VERSION}"

    def _headers(self):
        token = (self._param("access_token") or "").strip()
        if not token:
            raise UserError("Shopify Admin API access token is not configured.")
        return {
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json",
        }

    # --- low level ------------------------------------------------------
    def request(self, method, path, params=None, payload=None, retries=3):
        """REST call with throttle handling. Returns decoded JSON (dict)."""
        url = f"{self._base_url()}/{path.lstrip('/')}"
        for attempt in range(retries + 1):
            resp = requests.request(
                method,
                url,
                params=params,
                json=payload,
                headers=self._headers(),
                timeout=30,
            )
            if (resp.status_code == 429 or resp.status_code >= 500) and attempt < retries:
                wait = float(resp.headers.get("Retry-After", 2 * (attempt + 1)))
                _logger.warning(
                    "Shopify %s %s -> %s, retrying in %.1fs",
                    method, path, resp.status_code, wait,
                )
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                raise UserError(
                    f"Shopify API error {resp.status_code} on {method} {path}: "
                    f"{resp.text[:500]}"
                )
            return resp.json() if resp.text else {}
        return {}

    def get(self, path, params=None):
        return self.request("GET", path, params=params)

    def post(self, path, payload):
        return self.request("POST", path, payload=payload)

    def put(self, path, payload):
        return self.request("PUT", path, payload=payload)

    def delete(self, path):
        return self.request("DELETE", path)

    def graphql(self, query, variables=None, allow_partial=False):
        """Run a GraphQL query against the Admin API. Raises on errors."""
        resp = requests.post(
            f"{self._base_url()}/graphql.json",
            json={"query": query, "variables": variables or {}},
            headers=self._headers(),
            timeout=60,
        )
        if resp.status_code >= 400:
            raise UserError(
                f"Shopify GraphQL HTTP {resp.status_code}: {resp.text[:500]}"
            )
        data = resp.json()
        if data.get("errors"):
            if allow_partial and data.get("data"):
                _logger.warning(
                    "Shopify GraphQL returned partial data: %s",
                    json.dumps(data["errors"])[:500],
                )
                return data.get("data") or {}
            raise UserError(
                f"Shopify GraphQL errors: {json.dumps(data['errors'])[:500]}"
            )
        return data.get("data") or {}

    # --- convenience ----------------------------------------------------
    def sync_from(self):
        """Cutover timestamp for the 'sync from now' scope.

        Returns an ISO datetime string, or None when unset (sync everything).
        Engines use it to skip untouched historical records; records already
        linked to Shopify always keep syncing regardless."""
        value = (self._param("sync_from") or "").strip()
        return value or None

    def match_product_by_variant_id(self, variant_id):
        """Exact product match from Shopify variant id on the order line."""
        Product = self.env["product.product"]
        normalized = normalize_shopify_variant_id(variant_id)
        if not normalized:
            return Product.browse()
        product = Product.search(
            [("shopify_variant_id", "=", normalized)], limit=1
        )
        if product:
            return product
        gid = "gid://shopify/ProductVariant/%s" % normalized
        product = Product.search([("shopify_variant_id", "=", gid)], limit=1)
        if product:
            return product
        candidates = Product.search(
            [("shopify_variant_id", "ilike", normalized)], limit=2
        )
        if len(candidates) == 1:
            return candidates
        return Product.browse()

    def match_product_for_shopify_line(
        self, line_item, sku=None, name=None, title=None
    ):
        """Match an order line: Shopify variant id first, then SKU/name."""
        variant_id = None
        if isinstance(line_item, dict):
            variant_id = line_item.get("variant_id")
            sku = sku or (line_item.get("sku") or "").strip()
            title = title or (line_item.get("title") or "").strip() or None
            name = name or self.shopify_line_item_name(line_item) or None
        product = self.match_product_by_variant_id(variant_id)
        if product:
            return product
        return self.match_product_by_sku(
            sku, name=name, title=title, variant_id=variant_id
        )

    def match_product_by_sku(self, sku, name=None, title=None, variant_id=None):
        """Find the Odoo product for a SKU — duplicate-safe.

        When ``variant_id`` is supplied (Shopify order line), it is used to
        pick the correct product among duplicate Internal References. Falls
        back to Shopify line title/name, then a best-effort pick.
        """
        Product = self.env["product.product"]
        sku = (sku or "").strip()
        if not sku:
            if variant_id:
                return self.match_product_by_variant_id(variant_id)
            if name or title:
                return self.match_product_by_name(name, title=title)
            return Product.browse()
        for field_name in ("default_code", "barcode"):
            matches = Product.search([(field_name, "=", sku)], limit=10)
            if not matches:
                continue
            if len(matches) == 1:
                return matches[0]
            linked = matches.filtered(lambda p: p.shopify_variant_id)
            if len(linked) == 1:
                return linked
            picked = self._resolve_duplicate_matches(
                matches,
                sku,
                name=name,
                title=title,
                variant_id=variant_id,
            )
            _logger.warning(
                "Duplicate SKU %r on %d products (ids %s); resolved to "
                "product id %s (Shopify variant %s, line name %r).",
                sku,
                len(matches),
                matches.ids,
                picked.id,
                normalize_shopify_variant_id(variant_id) or variant_id or "",
                (name or title or "").strip(),
            )
            return picked
        by_variant = self.match_product_by_variant_id(variant_id)
        if by_variant:
            return by_variant
        if name or title:
            return self.match_product_by_name(name, title=title)
        return Product.browse()

    def _normalize_product_name(self, text):
        text = (text or "").strip().casefold()
        for sep in (" / ", " - ", " — ", "|", "[" , "]"):
            text = text.replace(sep, " ")
        return " ".join(text.split())

    def _names_similar(self, left, right):
        a = self._normalize_product_name(left)
        b = self._normalize_product_name(right)
        if not a or not b:
            return False
        if a == b:
            return True
        return a in b or b in a

    def _resolve_duplicate_matches(
        self, matches, sku, name=None, title=None, variant_id=None
    ):
        """Pick one product when several share the same SKU/barcode."""
        normalized_variant = normalize_shopify_variant_id(variant_id)
        if normalized_variant:
            variant_hit = matches.filtered(
                lambda p: normalize_shopify_variant_id(p.shopify_variant_id)
                == normalized_variant
            )
            if len(variant_hit) == 1:
                return variant_hit
        linked = matches.filtered(lambda p: p.shopify_variant_id)
        if len(linked) == 1:
            return linked

        labels = []
        for raw in (name, title):
            text = (raw or "").strip()
            if text and text not in labels:
                labels.append(text)

        for label in labels:
            exact = matches.filtered(
                lambda p, want=label: (p.name or "").strip() == want
            )
            if exact:
                return self._disambiguate_product_matches(exact, label)
            fuzzy = matches.filtered(
                lambda p, want=label: self._names_similar(want, p.name)
            )
            if fuzzy:
                return self._disambiguate_product_matches(fuzzy, label)

        if sku:
            in_name = matches.filtered(
                lambda p, want=sku: want.casefold() in (p.name or "").casefold()
            )
            if in_name:
                return self._disambiguate_product_matches(in_name, sku)

        by_name = self.match_product_by_name(name, title=title)
        if by_name:
            return by_name

        return self._disambiguate_product_matches(
            matches, sku or (name or title or "duplicate sku")
        )

    def match_product_by_name(self, name, title=None):
        """Best-effort product match from Shopify line title/name."""
        Product = self.env["product.product"]
        candidates = []
        for raw in (name, title):
            text = (raw or "").strip()
            if text and text not in candidates:
                candidates.append(text)
        if not candidates:
            return Product.browse()
        for candidate in candidates:
            matches = Product.search([("name", "=", candidate)], limit=10)
            if matches:
                return self._disambiguate_product_matches(matches, candidate)
            matches = Product.search([("name", "ilike", candidate)], limit=10)
            if matches:
                fuzzy = matches.filtered(
                    lambda p, want=candidate: self._names_similar(want, p.name)
                )
                pool = fuzzy or matches
                if pool:
                    return self._disambiguate_product_matches(pool, candidate)
        return Product.browse()

    def _disambiguate_product_matches(self, matches, label):
        linked = matches.filtered(lambda p: p.shopify_variant_id)
        if len(linked) == 1:
            return linked
        active = matches.filtered(lambda p: p.active and p.sale_ok)
        if len(active) == 1:
            return active
        if len(matches) == 1:
            return matches
        _logger.warning(
            "Ambiguous product name %r (%d ids %s); using lowest id.",
            label,
            len(matches),
            matches.ids,
        )
        return matches.sorted("id")[0]

    def get_order(self, shopify_order_id):
        return self.get(f"orders/{shopify_order_id}.json").get("order") or {}

    def get_order_fulfillments(self, shopify_order_id):
        order_id = str(shopify_order_id or "").strip()
        if not order_id:
            return []
        data = self.get(f"orders/{order_id}/fulfillments.json")
        return data.get("fulfillments") or []

    def get_product(self, shopify_product_id):
        product_id = normalize_shopify_variant_id(shopify_product_id)
        if not product_id:
            product_id = str(shopify_product_id or "").strip()
        if not product_id:
            return {}
        return self.get(f"products/{product_id}.json").get("product") or {}

    def get_variant(self, shopify_variant_id):
        variant_id = normalize_shopify_variant_id(shopify_variant_id)
        if not variant_id:
            return {}
        return self.get(f"variants/{variant_id}.json").get("variant") or {}

    def shopify_line_item_name(self, item):
        """Shopify REST line_item title, including variant when present."""
        if isinstance(item, str):
            return item.strip()
        if not isinstance(item, dict):
            return ""
        title = (item.get("title") or "").strip()
        variant = (item.get("variant_title") or "").strip()
        if variant and variant.casefold() != "default title":
            parts = [part for part in (title, variant) if part]
            if len(parts) > 1:
                return "%s / %s" % (parts[0], parts[1])
            return parts[0] if parts else ""
        return title

    def rotating_batch(self, model_name, domain, limit, cursor_key):
        """Return the next batch of records using a persisted id cursor.

        Cron sweeps that do ``search(domain, order='id', limit=N)`` re-process
        the same first N rows forever; on a large catalog (thousands of
        products) everything past the first page is never synced. This helper
        walks the table in id order across runs, wrapping around at the end,
        so every record gets covered. The cursor lives in ir.config_parameter
        under ``shopify_order_ops.<cursor_key>``.
        """
        icp = self.env["ir.config_parameter"].sudo()
        try:
            cursor = int(icp.get_param(PARAM_PREFIX + cursor_key) or 0)
        except (TypeError, ValueError):
            cursor = 0
        Model = self.env[model_name]
        batch = Model.search(domain + [("id", ">", cursor)], order="id", limit=limit)
        if len(batch) < limit:
            # End of the table: wrap around and top up from the start.
            batch |= Model.search(
                domain + [("id", "<=", cursor)], order="id", limit=limit - len(batch)
            )
        icp.set_param(PARAM_PREFIX + cursor_key, str(batch[-1].id if batch else 0))
        return batch

    def find_order_by_name(self, name):
        """name like '1001' or '#1001' (status=any to see archived/closed)."""
        clean = (name or "").lstrip("#")
        for candidate in (name, clean, f"#{clean}"):
            data = self.get(
                "orders.json",
                params={"name": candidate, "status": "any", "limit": 1},
            )
            orders = data.get("orders") or []
            if orders:
                return orders[0]
        return {}
