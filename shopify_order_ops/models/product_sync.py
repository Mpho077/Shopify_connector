import base64
import logging

import requests

from odoo import api, fields, models
from odoo.exceptions import UserError

from .shopify_discount import CHARGE_PRODUCT_SKUS, is_charge_sku
from .shopify_api import normalize_shopify_variant_id

_logger = logging.getLogger(__name__)

SOURCE = "product"
WATERMARK_PARAM = "shopify_order_ops.last_product_sync"
# Conversion factors to kilograms for Shopify weight units.
WEIGHT_TO_KG = {"g": 1 / 1000, "kg": 1.0, "oz": 0.0283495, "lb": 0.453592}
FALLBACK_CATEGORY = "Shopify Imported"


class ProductProduct(models.Model):
    _inherit = "product.product"

    shopify_product_id = fields.Char(
        string="Shopify Product ID", index=True, copy=False
    )
    shopify_variant_id = fields.Char(
        string="Shopify Variant ID", index=True, copy=False
    )
    shopify_content_sig = fields.Char(
        string="Shopify Content Signature",
        copy=False,
        help="Signature of the content last synced with Shopify "
             "(name|description_sale|barcode|weight). Used by two-way sync "
             "to push only genuine Odoo-side content changes.",
    )


class ShopifyProductSync(models.AbstractModel):
    _name = "shopify.product.sync"
    _description = "Syncs the product catalog between Shopify and Odoo"

    # ------------------------------------------------------------------
    # public entry point
    # ------------------------------------------------------------------
    def cron_sync_products(self, limit=50, since_id=0, window_start=None):
        """Sync products per the configured direction.

        Contract (implemented by agent task E):
        - Respect the `product_sync_enabled` flag; read `product_sync_direction`
          ('shopify_to_odoo' default, 'odoo_to_shopify', or 'two_way').
        - Shopify -> Odoo: watermark `shopify_order_ops.last_product_sync`
          (ISO datetime) in ir.config_parameter; fetch products.json with
          updated_at_min + limit=250 + since_id pagination. For EVERY variant:
          match Odoo product by shopify_variant_id, then default_code=sku,
          then barcode. SIMPLIFICATION: each Shopify variant maps to its own
          simple product.template (no attribute matrix); name = 'Title' or
          'Title / Option1 Option2' for multi-variant products. Set
          default_code (SKU) on create and on later Shopify SKU changes,
          barcode, list_price (create-time only — ongoing price
          changes are the price sync's job), weight (convert Shopify
          weight+weight_unit to kg), image_1920 from the first image URL
          (fetch bytes, base64), product_type -> find/create product.category,
          status 'archived' -> active=False. Store shopify_product_id and
          shopify_variant_id on the variant.
        - When a pull batch is full (`limit` products), do not advance the
          watermark; queue another product_sync job with since_id so the rest
          of the window is processed. Only advance the watermark when the
          window is exhausted (short batch).
        - Odoo -> Shopify: for product.template records with a default_code,
          sale_ok=True, no shopify_variant_id on their variant: create a
          Shopify product with one variant (sku, barcode, price=list_price —
          create-time only), image from image_1920, description from
          description_sale; store the returned ids. Update title/description
          on existing linked products.
        - Two-way: run the Shopify -> Odoo watermark sweep first (unchanged
          behavior), then the Odoo -> Shopify push. Creates work as in
          single-direction mode; content updates are pushed ONLY when the
          content signature (name|description_sale|barcode|weight) differs
          from product.product.shopify_content_sig. The signature is
          re-stored after every pull upsert, push create, and push update,
          so a change synced in one direction never bounces back as a new
          "change" in the other (no ping-pong). Price/inventory-policy in
          push payloads keep following the settings pack (price sync has its
          own two-way mode).
        - Never delete anything on either side; never duplicate products.
        - Process at most `limit` Shopify products (or Odoo templates) per run;
          log failures per item and continue.

        Product settings pack (agent task N) — all via ir.config_parameter
        keys prefixed shopify_order_ops.:
        - product_create_mode: 'create_update' (default) | 'update_only'.
          update_only never creates new products on either side — only
          already-linked/matched products are updated.
        - product_auto_publish: default ON. OFF creates new Shopify products
          with status 'draft' instead of 'active'.
        - product_sync_only_instock: Odoo->Shopify creation filter — skip
          products whose variant free_qty <= 0.
        - product_collections_from_categories: after a Shopify product
          create/update, ensure a custom collection named after the Odoo
          category exists and contains the product (idempotent).
        - product_sync_categories / product_sync_tags: comma-separated
          Odoo->Shopify scope filters (category complete_name contains, or at
          least one matching product tag; case-insensitive; empty = all).
        - product_price_tax_included: multiply the outgoing price by
          (1 + sum of percent taxes_id amounts / 100).
        - product_sell_out_of_stock: variant payload gets
          inventory_policy 'continue' (ON) or 'deny' (OFF).
        - price_from_pricelist + pricelist_id: outgoing price comes from the
          configured pricelist (_get_product_price); any error falls back to
          list_price with one warning per run.
        """
        api = self.env["shopify.api.client"]
        if not self._is_truthy(api._param("product_sync_enabled")):
            self._log("info", "Product sync is disabled (product_sync_enabled); run skipped.")
            return False
        direction = api._param("product_sync_direction", "shopify_to_odoo") or "shopify_to_odoo"
        try:
            limit = max(1, int(limit or 50))
        except (TypeError, ValueError):
            limit = 50
        try:
            since_id = max(0, int(since_id or 0))
        except (TypeError, ValueError):
            since_id = 0
        settings = self._product_settings(api)
        if direction == "odoo_to_shopify":
            self._sync_odoo_to_shopify(api, limit, settings)
            return False
        if direction == "two_way":
            # Pull first so Shopify-side edits land (and get re-signed)
            # before the push decides what Odoo-side changes to send.
            cont = self.with_context(shopify_sync_origin="shopify")._sync_shopify_to_odoo(
                api, limit, settings, since_id=since_id, window_start=window_start
            )
            # Only push when this pull window is finished (avoid push mid-window).
            if not cont:
                self._sync_odoo_to_shopify(api, limit, settings, two_way=True)
            return cont
        return self.with_context(shopify_sync_origin="shopify")._sync_shopify_to_odoo(
            api, limit, settings, since_id=since_id, window_start=window_start
        )

    # ------------------------------------------------------------------
    # settings pack
    # ------------------------------------------------------------------
    def _product_settings(self, api):
        """Read the product settings pack once per run into a plain dict."""
        settings = {
            "create_mode": (api._param("product_create_mode") or "create_update").strip(),
            "auto_publish": self._is_truthy(api._param("product_auto_publish"), default=True),
            "only_instock": self._is_truthy(api._param("product_sync_only_instock")),
            "collections": self._is_truthy(api._param("product_collections_from_categories")),
            "categories": self._csv_filter(api._param("product_sync_categories")),
            "tags": self._csv_filter(api._param("product_sync_tags")),
            "tax_included": self._is_truthy(api._param("product_price_tax_included")),
            "sell_oos": self._is_truthy(api._param("product_sell_out_of_stock")),
            "pricelist": None,
            # run-scoped state:
            "pricelist_warned": False,
            "collection_cache": {},
        }
        if self._is_truthy(api._param("price_from_pricelist")):
            pricelist = self._configured_pricelist(api)
            if pricelist is None:
                self._log(
                    "warning",
                    "price_from_pricelist is enabled but no valid pricelist is "
                    "configured (pricelist_id); falling back to list_price.",
                )
            settings["pricelist"] = pricelist
        return settings

    @staticmethod
    def _csv_filter(raw):
        return [part.strip().lower() for part in (raw or "").split(",") if part.strip()]

    def _configured_pricelist(self, api):
        try:
            pricelist_id = int(api._param("pricelist_id") or 0)
        except (TypeError, ValueError):
            return None
        if not pricelist_id:
            return None
        pricelist = self.env["product.pricelist"].sudo().browse(pricelist_id)
        return pricelist if pricelist.exists() else None

    # ------------------------------------------------------------------
    # Shopify -> Odoo
    # ------------------------------------------------------------------
    def _sync_shopify_to_odoo(
        self, api, limit, settings, since_id=0, window_start=None
    ):
        """Pull Shopify products. Returns True when another batch was queued."""
        params = self.env["ir.config_parameter"].sudo()
        watermark = (window_start or "").strip() or params.get_param(WATERMARK_PARAM)
        # 'Sync from' cutover: a first-ever pull starts at the cutover, not
        # at the beginning of Shopify history.
        if not watermark:
            watermark = api.sync_from()
        # Capture the run start BEFORE the first API call; the watermark is
        # only advanced after the full window is exhausted (not mid-batch).
        run_start = fields.Datetime.now()

        products = []
        cursor = max(0, int(since_id or 0))
        while len(products) < limit:
            query = {"limit": 250, "since_id": cursor}
            if watermark:
                query["updated_at_min"] = watermark
            page = api.get("products.json", params=query).get("products") or []
            if not page:
                break
            products.extend(page)
            cursor = max(p.get("id") or 0 for p in page)
            if len(page) < 250:  # short page -> no more results
                break
        products = products[:limit]
        last_id = max((p.get("id") or 0) for p in products) if products else 0

        created = updated = failed = skipped = unchanged = 0
        updated_refs = []
        created_refs = []
        skipped_refs = []
        for shop_product in products:
            try:
                for variant in shop_product.get("variants") or []:
                    result = self._upsert_variant(shop_product, variant, settings)
                    sku = (variant.get("sku") or "").strip()
                    ref = sku or ("variant:%s" % (variant.get("id") or "?"))
                    title = (shop_product.get("title") or "").strip()
                    label = "%s (%s)" % (ref, title) if title else ref
                    if result == "created":
                        created += 1
                        if len(created_refs) < 80:
                            created_refs.append(label)
                    elif result == "updated":
                        updated += 1
                        if len(updated_refs) < 80:
                            updated_refs.append(label)
                    elif result == "skipped":
                        skipped += 1
                        if len(skipped_refs) < 40:
                            skipped_refs.append(label)
                    else:
                        unchanged += 1
            except Exception as exc:
                failed += 1
                self._log(
                    "error",
                    "Shopify – Odoo: failed Shopify product %s '%s': %s"
                    % (shop_product.get("id"), shop_product.get("title"), exc),
                )

        if created_refs:
            more_n = created - len(created_refs)
            self._log(
                "info",
                "Shopify – Odoo created: %s%s"
                % (
                    ", ".join(created_refs),
                    (" … +%d more" % more_n) if more_n > 0 else "",
                ),
            )
        if updated_refs:
            more_n = updated - len(updated_refs)
            self._log(
                "info",
                "Shopify – Odoo updated: %s%s"
                % (
                    ", ".join(updated_refs),
                    (" … +%d more" % more_n) if more_n > 0 else "",
                ),
            )
        if skipped_refs:
            more_n = skipped - len(skipped_refs)
            self._log(
                "warning",
                "Shopify – Odoo skipped (create mode is update only, or no "
                "match): %s%s"
                % (
                    ", ".join(skipped_refs),
                    (" … +%d more" % more_n) if more_n > 0 else "",
                ),
            )

        more = bool(products) and len(products) >= limit and last_id
        if more:
            # Keep the same window; next batch continues after last_id.
            if watermark:
                params.set_param(WATERMARK_PARAM, watermark)
            self._log(
                "info",
                "Product sync Shopify – Odoo batch: %d created, %d updated, "
                "%d unchanged, %d skipped, %d failed (batch full at %d; "
                "continuing after Shopify product id %s, window %s)."
                % (
                    created,
                    updated,
                    unchanged,
                    skipped,
                    failed,
                    limit,
                    last_id,
                    watermark or "(all)",
                ),
            )
            self._queue_product_sync_continuation(limit, watermark, last_id)
            return True

        params.set_param(WATERMARK_PARAM, run_start.isoformat())
        self._log(
            "info",
            "Product sync Shopify – Odoo finished: %d created, %d updated, "
            "%d unchanged, %d skipped (update_only / no match), %d failed "
            "(watermark advanced to %s)."
            % (
                created,
                updated,
                unchanged,
                skipped,
                failed,
                run_start.isoformat(),
            ),
        )
        return False

    def _queue_product_sync_continuation(self, limit, window_start, since_id):
        """Enqueue the next pull batch and process it (depth-capped)."""
        Job = self.env["shopify.sync.job"].sudo()
        depth = int(self.env.context.get("product_sync_continue_depth") or 0)
        job = Job.enqueue(
            "product sync continue after id %s" % since_id,
            "product_sync",
            {
                "limit": limit,
                "since": window_start or "",
                "since_id": since_id,
                "topic": "continue",
            },
        )
        self._log(
            "info",
            "Queued product_sync continuation job %s (after id %s, depth %d)."
            % (job.id, since_id, depth),
        )
        # Cap chained inline runs so a huge window cannot hold one worker forever.
        if depth >= 20:
            cron = self.env.ref(
                "shopify_order_ops.ir_cron_process_jobs",
                raise_if_not_found=False,
            )
            if cron:
                try:
                    cron.sudo()._trigger()
                except Exception:  # noqa: BLE001
                    pass
            return
        try:
            job.with_context(product_sync_continue_depth=depth + 1)._process_one()
        except Exception:  # noqa: BLE001 - cron/backstop retries the job
            _logger.exception(
                "Shopify Ops: product sync continuation job %s failed inline",
                job.id,
            )

    def _upsert_variant(self, shop_product, variant, settings):
        """Create or update the Odoo product for one Shopify variant.

        Returns 'created', 'updated', or 'skipped' (update_only mode with no
        existing match).
        """
        Product = self.env["product.product"].sudo()
        shopify_product_id = str(shop_product.get("id"))
        shopify_variant_id = str(variant.get("id"))
        sku = (variant.get("sku") or "").strip()

        product = Product.search(
            [("shopify_variant_id", "=", shopify_variant_id)], limit=1
        )
        if not product and sku:
            # Duplicate-safe SKU/barcode match; name fallback on duplicates.
            product = self.env["shopify.api.client"].match_product_by_sku(
                sku,
                name=self._variant_name(shop_product, variant),
                title=shop_product.get("title"),
            )
        # SKU rename on Shopify: old Internal Reference no longer matches, but
        # the same Shopify product id may already be linked in Odoo.
        if not product and shopify_product_id:
            siblings = Product.search(
                [("shopify_product_id", "=", shopify_product_id)]
            )
            if len(siblings) == 1:
                product = siblings
            elif shopify_variant_id and len(siblings) > 1:
                # Prefer the sibling that still has no variant id, else leave
                # unmatched so we do not attach the wrong variant.
                unlinked = siblings.filtered(lambda p: not p.shopify_variant_id)
                if len(unlinked) == 1:
                    product = unlinked

        name = self._variant_name(shop_product, variant)
        weight_kg = self._weight_to_kg(variant)
        status = shop_product.get("status")

        if not product:
            if settings["create_mode"] == "update_only":
                return "skipped"
            template = self.env["product.template"].sudo().create(
                {
                    "name": name,
                    "default_code": sku or False,
                    "barcode": variant.get("barcode") or False,
                    "list_price": self._safe_float(variant.get("price")),
                    "weight": weight_kg,
                    "categ_id": self._find_or_create_category(
                        shop_product.get("product_type")
                    ).id,
                    "image_1920": self._variant_image_b64(shop_product, variant),
                    "is_storable": True,
                    "sale_ok": True,
                    "active": status != "archived",
                }
            )
            template.product_variant_id.write(
                {
                    "shopify_product_id": shopify_product_id,
                    "shopify_variant_id": shopify_variant_id,
                    "shopify_content_sig": self._content_signature(template),
                }
            )
            return "created"

        vals = {}
        old_sku = (product.default_code or "").strip()
        if product.name != name:
            vals["name"] = name
        # Match is by shopify_variant_id first, so SKU renames still find the
        # row; then write the new Internal Reference.
        if sku and old_sku != sku:
            vals["default_code"] = sku
        if variant.get("barcode") and (product.barcode or "") != (
            variant.get("barcode") or ""
        ):
            vals["barcode"] = variant["barcode"]
        if (shop_product.get("product_type") or "").strip():
            categ = self._find_or_create_category(shop_product["product_type"])
            if product.categ_id != categ:
                vals["categ_id"] = categ.id
        if abs((product.weight or 0.0) - (weight_kg or 0.0)) > 0.0001:
            vals["weight"] = weight_kg
        if status == "archived" and product.active:
            vals["active"] = False
        elif status == "active" and not product.active:
            vals["active"] = True
        if not product.shopify_product_id:
            vals["shopify_product_id"] = shopify_product_id
        if not product.shopify_variant_id:
            vals["shopify_variant_id"] = shopify_variant_id
        if vals:
            product.write(vals)
            # Re-stamp the content signature so a Shopify-side edit just pulled
            # into Odoo is not pushed straight back as an Odoo-side "change".
            sig = self._content_signature(product.product_tmpl_id)
            if (product.shopify_content_sig or "") != sig:
                product.shopify_content_sig = sig
            if "default_code" in vals:
                self._log(
                    "info",
                    "Shopify – Odoo SKU change: %s → %s (%s)"
                    % (old_sku or "(empty)", sku, name),
                )
            return "updated"
        sig = self._content_signature(product.product_tmpl_id)
        if (product.shopify_content_sig or "") != sig:
            product.shopify_content_sig = sig
        return "unchanged"

    def ensure_product_for_order_line(self, line_item, _log=None):
        """Import or link a Shopify variant when order pull/edit cannot match it."""
        api = self.env["shopify.api.client"]
        Product = self.env["product.product"]
        if not isinstance(line_item, dict):
            return Product.browse()

        product = api.match_product_for_shopify_line(line_item)
        if product:
            return product

        variant_id = normalize_shopify_variant_id(line_item.get("variant_id"))
        if not variant_id:
            return Product.browse()

        if not self._is_truthy(api._param("product_sync_enabled")):
            if _log:
                _log(
                    "warning",
                    "Shopify variant %s (SKU %s) is not in Odoo and product "
                    "sync is off — enable product sync or create the product "
                    "manually."
                    % (
                        variant_id,
                        (line_item.get("sku") or "").strip(),
                    ),
                )
            return Product.browse()

        settings = self._product_settings(api)
        if settings["create_mode"] == "update_only":
            if _log:
                _log(
                    "warning",
                    "Shopify variant %s (SKU %s) is not in Odoo and product "
                    "create mode is update only."
                    % (
                        variant_id,
                        (line_item.get("sku") or "").strip(),
                    ),
                )
            return Product.browse()

        variant = api.get_variant(variant_id)
        if not variant:
            if _log:
                _log(
                    "warning",
                    "Shopify variant %s was not found via the Admin API."
                    % variant_id,
                )
            return Product.browse()

        shop_product_id = variant.get("product_id") or line_item.get("product_id")
        shop_product = api.get_product(shop_product_id) if shop_product_id else {}
        if not shop_product:
            if _log:
                _log(
                    "warning",
                    "Shopify product %s for variant %s was not found via the "
                    "Admin API."
                    % (shop_product_id, variant_id),
                )
            return Product.browse()

        full_variant = variant
        for candidate in shop_product.get("variants") or []:
            if str(candidate.get("id")) == variant_id:
                full_variant = candidate
                break

        try:
            outcome = self.with_context(
                shopify_sync_origin="shopify"
            )._upsert_variant(shop_product, full_variant, settings)
        except Exception as exc:  # noqa: BLE001 - order job should surface this
            if _log:
                _log(
                    "error",
                    "Could not import Shopify variant %s (SKU %s): %s"
                    % (
                        variant_id,
                        (line_item.get("sku") or "").strip(),
                        exc,
                    ),
                )
            return Product.browse()

        product = api.match_product_by_variant_id(variant_id)
        if product and _log:
            _log(
                "info",
                "Imported Shopify variant %s (SKU %s) as Odoo product id %s "
                "(%s)."
                % (
                    variant_id,
                    (line_item.get("sku") or "").strip(),
                    product.id,
                    outcome,
                ),
            )
        return product

    def match_or_import_for_order_line(
        self, sku, line_item=None, name=None, title=None, _log=None
    ):
        """Match by variant id / SKU, else import the variant from Shopify."""
        api = self.env["shopify.api.client"]
        if line_item:
            product = api.match_product_for_shopify_line(
                line_item, sku=sku, name=name, title=title
            )
        else:
            product = api.match_product_by_sku(
                sku, name=name, title=title
            )
        if not product and line_item:
            product = self.ensure_product_for_order_line(line_item, _log=_log)
        return product

    # ------------------------------------------------------------------
    # Odoo -> Shopify
    # ------------------------------------------------------------------
    def _sync_odoo_to_shopify(self, api, limit, settings, two_way=False):
        # Candidates are resolved at product.product level (simple templates
        # have exactly one variant); product.template data is used via
        # product_tmpl_id. Batch both creates and updates within `limit`.
        # two_way=True gates content updates on the content signature.
        Product = self.env["product.product"].sudo()
        base_domain = [
            ("default_code", "!=", False),
            ("default_code", "not in", list(CHARGE_PRODUCT_SKUS)),
            ("sale_ok", "=", True),
            ("active", "=", True),
        ]
        # 'Sync from' cutover: new-product pushes only cover products touched
        # after the cutover. Already-linked products always keep syncing.
        sync_from = self.env["shopify.api.client"].sync_from()
        create_domain = list(base_domain) + [("shopify_variant_id", "=", False)]
        if sync_from:
            create_domain.append(("write_date", ">=", sync_from))
        to_create = (
            api.rotating_batch("product.product", create_domain, limit, "product_push_create_cursor")
            if settings["create_mode"] != "update_only"
            else Product.browse()
        )
        remaining = limit - len(to_create)
        to_update = (
            api.rotating_batch(
                "product.product",
                base_domain + [("shopify_product_id", "!=", False)],
                remaining,
                "product_push_update_cursor",
            )
            if remaining > 0
            else Product.browse()
        )

        created = updated = failed = filtered = unchanged = 0
        for variant in to_create:
            template = variant.product_tmpl_id
            if self._candidate_excluded_reason(settings, template, for_create=True):
                filtered += 1
                continue
            try:
                self._push_new_product(api, template, settings)
                created += 1
            except Exception as exc:
                failed += 1
                self._log(
                    "error",
                    "Odoo – Shopify: failed to create product for template %d '%s': %s"
                    % (template.id, template.name, exc),
                )
        for variant in to_update:
            template = variant.product_tmpl_id
            if self._candidate_excluded_reason(settings, template, for_create=False):
                filtered += 1
                continue
            if two_way and (variant.shopify_content_sig or "") == self._content_signature(
                template
            ):
                # Content unchanged since the last sync in either direction.
                unchanged += 1
                continue
            try:
                self._push_product_update(api, template, settings)
                updated += 1
            except Exception as exc:
                failed += 1
                self._log(
                    "error",
                    "Odoo – Shopify: failed to update Shopify product %s (template %d): %s"
                    % (
                        variant.shopify_product_id,
                        template.id,
                        exc,
                    ),
                )
        if two_way:
            self._log(
                "info",
                "Product sync Odoo – Shopify (two-way push) finished: %d created, "
                "%d updated, %d unchanged (content signature match), %d filtered "
                "(category/tag/stock scope), %d failed."
                % (created, updated, unchanged, filtered, failed),
            )
        else:
            self._log(
                "info",
                "Product sync Odoo – Shopify finished: %d created, %d updated, %d filtered "
                "(category/tag/stock scope), %d failed."
                % (created, updated, filtered, failed),
            )

    def _candidate_excluded_reason(self, settings, template, for_create):
        """Return a short reason when the template is outside the sync scope.

        Category/tag filters apply to creates and updates; the in-stock filter
        is a creation-only filter per the settings contract.
        """
        if settings["categories"]:
            complete_name = (template.categ_id.complete_name or "").lower()
            if not any(name in complete_name for name in settings["categories"]):
                return "category"
        if settings["tags"]:
            tag_names = {tag.name.lower() for tag in template.product_tag_ids if tag.name}
            if not tag_names.intersection(settings["tags"]):
                return "tags"
        if for_create and settings["only_instock"]:
            variant = template.product_variant_id
            if variant and variant.free_qty <= 0:
                return "out of stock"
        return False

    def _push_new_product(self, api, template, settings):
        product_payload = {
            "title": template.name,
            "body_html": template.description_sale or "",
            "status": "active" if settings["auto_publish"] else "draft",
            "variants": [
                {
                    "sku": template.default_code,
                    "barcode": template.barcode or None,
                    "price": str(self._price_to_send(template, settings)),
                    "weight": template.weight,
                    "weight_unit": "kg",
                    "inventory_policy": (
                        "continue" if settings["sell_oos"] else "deny"
                    ),
                }
            ],
        }
        if template.image_1920:
            image = template.image_1920
            if isinstance(image, bytes):
                image = image.decode("ascii")
            product_payload["images"] = [{"attachment": image}]
        data = api.post("products.json", {"product": product_payload}).get("product") or {}
        product_id = data.get("id")
        variants = data.get("variants") or []
        variant_id = variants[0].get("id") if variants else None
        if not product_id or not variant_id:
            raise ValueError(
                "Shopify product create returned no ids: %s" % str(data)[:200]
            )
        template.product_variant_id.write(
            {
                "shopify_product_id": str(product_id),
                "shopify_variant_id": str(variant_id),
                "shopify_content_sig": self._content_signature(template),
            }
        )
        self._ensure_category_collection(api, settings, template, str(product_id))

    def _push_product_update(self, api, template, settings):
        variant = template.product_variant_id
        shopify_product_id = variant.shopify_product_id
        if not shopify_product_id:
            return
        api.put(
            "products/%s.json" % shopify_product_id,
            {
                "product": {
                    "title": template.name,
                    "body_html": template.description_sale or "",
                }
            },
        )
        # Variant-level: price (tax/pricelist aware) + inventory policy.
        # Never sku.
        if variant.shopify_variant_id:
            api.put(
                "variants/%s.json" % variant.shopify_variant_id,
                {
                    "variant": {
                        "price": str(self._price_to_send(template, settings)),
                        "inventory_policy": (
                            "continue" if settings["sell_oos"] else "deny"
                        ),
                    }
                },
            )
        # Content was just pushed: re-stamp the signature so two-way mode
        # does not treat this product as changed on the next run.
        variant.shopify_content_sig = self._content_signature(template)
        self._ensure_category_collection(api, settings, template, shopify_product_id)

    # ------------------------------------------------------------------
    # pricing / variant options
    # ------------------------------------------------------------------
    def _price_to_send(self, template, settings):
        """Compute the Shopify price for a template per the settings pack.

        Base is the configured pricelist price when enabled (falling back to
        list_price on any error, warned once per run); the tax-included option
        then multiplies by (1 + sum of percent taxes_id amounts / 100).
        """
        price = template.list_price
        pricelist = settings["pricelist"]
        if pricelist is not None:
            try:
                price = float(pricelist._get_product_price(template, 1.0))
            except Exception as exc:
                price = template.list_price
                if not settings["pricelist_warned"]:
                    settings["pricelist_warned"] = True
                    self._log(
                        "warning",
                        "Pricelist %d price lookup failed (%s); falling back to "
                        "list_price for the rest of this run." % (pricelist.id, exc),
                    )
        if settings["tax_included"]:
            percent = sum(
                tax.amount
                for tax in template.taxes_id
                if tax.amount_type == "percent"
            )
            price = price * (1 + percent / 100.0)
        return round(price, 2)

    # ------------------------------------------------------------------
    # collections from Odoo categories
    # ------------------------------------------------------------------
    def _ensure_category_collection(self, api, settings, template, shopify_product_id):
        """Ensure the Shopify custom collection for the Odoo category exists
        and contains the product. Collection failures never fail the product
        itself (logged as warning)."""
        if not settings["collections"] or not shopify_product_id:
            return
        try:
            title = (template.categ_id.name or "").strip()
            if not title:
                return
            collection_id = self._collection_id_by_title(api, settings, title)
            if collection_id:
                self._add_product_to_collection(api, shopify_product_id, collection_id)
        except Exception as exc:
            self._log(
                "warning",
                "Collections sync failed for Shopify product %s (template %d): %s"
                % (shopify_product_id, template.id, exc),
            )

    def _collection_id_by_title(self, api, settings, title):
        """Find (or create) a custom collection by title; cached per run."""
        cache = settings["collection_cache"]
        key = title.lower()
        if key in cache:
            return cache[key]
        data = api.get("custom_collections.json", params={"title": title})
        collections = data.get("custom_collections") or []
        match = next(
            (
                c
                for c in collections
                if (c.get("title") or "").strip().lower() == key
            ),
            None,
        )
        if match is None and collections:
            match = collections[0]
        if match is None:
            created = api.post(
                "custom_collections.json", {"custom_collection": {"title": title}}
            )
            match = created.get("custom_collection") or {}
        collection_id = match.get("id")
        cache[key] = collection_id
        return collection_id

    def _add_product_to_collection(self, api, shopify_product_id, collection_id):
        existing = (
            api.get("collects.json", params={"product_id": shopify_product_id}).get(
                "collects"
            )
            or []
        )
        if any(str(c.get("collection_id")) == str(collection_id) for c in existing):
            return
        try:
            api.post(
                "collects.json",
                {
                    "collect": {
                        "product_id": shopify_product_id,
                        "collection_id": collection_id,
                    }
                },
            )
        except UserError as exc:
            # 422 = duplicate collect (product already in collection).
            if "422" in str(exc):
                return
            raise

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _log(self, level, message):
        self.env["shopify.sync.log"].log_event(level, message, source=SOURCE)

    @staticmethod
    def _is_truthy(value, default=False):
        if value is None or str(value).strip() == "":
            return default
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    @staticmethod
    def _safe_float(value, default=0.0):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _weight_to_kg(self, variant):
        weight = self._safe_float(variant.get("weight"))
        unit = (variant.get("weight_unit") or "kg").strip().lower()
        return weight * WEIGHT_TO_KG.get(unit, 1.0)

    def _variant_name(self, shop_product, variant):
        title = shop_product.get("title") or "Shopify product"
        if len(shop_product.get("variants") or []) <= 1:
            return title
        options = [
            opt
            for opt in (
                variant.get("option1"),
                variant.get("option2"),
                variant.get("option3"),
            )
            if opt and opt != "Default Title"
        ]
        return "%s / %s" % (title, " ".join(options)) if options else title

    @staticmethod
    def _content_signature(template):
        """Signature of the content the push writes to Shopify.

        Built from the template fields that the Odoo->Shopify payload is
        derived from (title/name, body_html/description_sale, variant
        barcode, variant weight). Two-way mode compares this against
        product.product.shopify_content_sig and only pushes an update when
        they differ; the stored signature is refreshed after every
        successful pull upsert, push create, and push update.
        """
        return "%s|%s|%s|%s" % (
            template.name or "",
            template.description_sale or "",
            template.barcode or "",
            template.weight or 0.0,
        )

    def _find_or_create_category(self, product_type):
        name = (product_type or "").strip() or FALLBACK_CATEGORY
        Category = self.env["product.category"].sudo()
        categ = Category.search([("name", "=", name)], limit=1)
        return categ or Category.create({"name": name})

    def _variant_image_b64(self, shop_product, variant):
        """Return base64 bytes of the first available image, or False.

        Prefers the image matched by variant.image_id, then the product's
        main image, then the first image in the list. Fetch failures are
        skipped silently (server log only).
        """
        images = shop_product.get("images") or []
        url = None
        image_id = variant.get("image_id")
        if image_id:
            url = next(
                (img.get("src") for img in images if img.get("id") == image_id),
                None,
            )
        if not url:
            url = (shop_product.get("image") or {}).get("src")
        if not url and images:
            url = images[0].get("src")
        if not url:
            return False
        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code == 200 and resp.content:
                return base64.b64encode(resp.content)
        except Exception:
            _logger.warning("Product sync: could not fetch image %s", url)
        return False

    # ------------------------------------------------------------------
    # manual one-product push (product form button)
    # ------------------------------------------------------------------
    def push_single_product(self, product):
        """Immediately push one product to Shopify (create or update),
        bypassing schedule, batch limits and the content-signature gate.
        Returns a human-readable message; raises UserError with the reason
        on failure so the button shows it in a dialog."""
        log = self.env["shopify.sync.log"]
        api = self.env["shopify.api.client"]
        template = product.product_tmpl_id
        if not product.default_code:
            raise UserError(
                "Set an Internal Reference (SKU) on %s first — Shopify "
                "matching relies on it." % template.name
            )
        try:
            if product.shopify_variant_id and product.shopify_product_id:
                self._push_product_update(api, template, settings=self._product_settings(api))
                msg = "Product '%s' updated in Shopify." % template.name
            else:
                self._push_new_product(api, template, settings=self._product_settings(api))
                msg = "Product '%s' created in Shopify." % template.name
        except UserError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface as dialog
            raise UserError(
                "Shopify push failed for '%s': %s" % (template.name, exc)
            )
        log.log_event("info", "Manual push: %s" % msg, source="product")
        return msg


class ProductTemplate(models.Model):
    _inherit = "product.template"

    def action_push_to_shopify(self):
        """'Push to Shopify' button on the product form."""
        self.ensure_one()
        message = self.env["shopify.product.sync"].push_single_product(
            self.product_variant_id
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Shopify product sync",
                "message": message,
                "type": "success",
                "sticky": False,
            },
        }

class ShopifyProductSyncWebhook(models.AbstractModel):
    """Extension adding the webhook + instant-push entry points to the
    product sync model."""

    _inherit = "shopify.product.sync"

    # ------------------------------------------------------------------
    # webhook: Shopify -> Odoo (products/create + products/update)
    # ------------------------------------------------------------------
    def process_product_webhook(self, job):
        """Real-time product sync from Shopify. The products/create and
        products/update payloads carry the full product with variants, so
        no API fetch is needed. Catalog upsert honors product_sync_enabled
        + direction (skipped in push-only mode); price updates always
        delegate to the price sync (which self-guards)."""
        log = self.env["shopify.sync.log"]
        api = self.env["shopify.api.client"]
        payload = job.payload_dict()
        raw = payload.get("raw") or {}
        topic = payload.get("topic") or ""
        shopify_product_id = str(raw.get("id") or "")
        variants = raw.get("variants") or []

        if not shopify_product_id or not variants:
            log.log_event(
                "warning",
                "Product webhook carried no usable product payload; nothing to do.",
                source="product",
                job=job,
            )
            return

        direction = (api._param("product_sync_direction") or "shopify_to_odoo").strip()
        if (
            self._is_truthy(api._param("product_sync_enabled"))
            and direction != "odoo_to_shopify"
        ):
            settings = self._product_settings(api)
            guarded = self.with_context(shopify_sync_origin="shopify")
            outcomes = []
            for variant in variants:
                try:
                    outcomes.append(
                        str(guarded._upsert_variant(raw, variant, settings))
                    )
                except Exception as exc:  # noqa: BLE001 - per-variant isolation
                    outcomes.append("error")
                    log.log_event(
                        "error",
                        "Product webhook upsert failed for variant %s: %s"
                        % (variant.get("id"), exc),
                        source="product",
                        job=job,
                    )
            log.log_event(
                "info",
                "Product webhook (%s) processed %d variant(s) for product %s: %s."
                % (topic, len(variants), shopify_product_id, ", ".join(outcomes)),
                source="product",
                job=job,
            )
        else:
            log.log_event(
                "info",
                "Product webhook (%s): catalog sync skipped (disabled or "
                "push-only mode)." % topic,
                source="product",
                job=job,
            )

        # Price part: always delegate — process_price_update self-guards on
        # price_sync_enabled and direction.
        self.env["shopify.price.sync"].process_price_update(job)

    # ------------------------------------------------------------------
    # instant push: Odoo -> Shopify (write/create hooks + job engine)
    # ------------------------------------------------------------------
    def process_product_update_push(self, job):
        """Push one product to Shopify immediately (queued by the write
        hooks for template/variant edits and new products)."""
        log = self.env["shopify.sync.log"]
        payload = job.payload_dict()
        product = (
            self.env["product.product"]
            .sudo()
            .browse(payload.get("product_id") or 0)
        )
        if not product.exists():
            log.log_event(
                "warning",
                "Product push job %s: product %s no longer exists; skipped."
                % (job.name, payload.get("product_id")),
                source="product",
                job=job,
            )
            return
        # push_single_product raises UserError with a clear message; let it
        # propagate so the job retries and fails visibly if unresolved.
        message = self.push_single_product(product)
        log.log_event("info", "Instant push: %s" % message, source="product", job=job)


_PUSH_TEMPLATE_FIELDS = {
    "name", "list_price", "description_sale", "weight", "active",
    "sale_ok", "categ_id",
}
_PUSH_VARIANT_FIELDS = {"default_code", "barcode", "list_price", "lst_price"}


class ProductTemplatePushHook(models.Model):
    """Queue an instant Shopify push when a product is edited or created
    in Odoo. Guarded by the shopify_sync_origin context flag so sync
    engine writes never echo back out."""

    _inherit = "product.template"

    @api.model_create_multi
    def create(self, vals_list):
        templates = super().create(vals_list)
        try:
            templates._enqueue_product_push()
        except Exception:  # noqa: BLE001 - hooks never break product edits
            _logger.exception("product_update_push: create hook failed")
        return templates

    def write(self, vals):
        res = super().write(vals)
        if set(vals) & _PUSH_TEMPLATE_FIELDS:
            try:
                self._enqueue_product_push()
            except Exception:  # noqa: BLE001
                _logger.exception("product_update_push: write hook failed")
        return res

    def _enqueue_product_push(self):
        if self.env.context.get("shopify_sync_origin") == "shopify":
            return
        icp = self.env["ir.config_parameter"].sudo()
        enabled = str(
            icp.get_param("shopify_order_ops.product_sync_enabled") or ""
        ).strip().lower() in ("1", "true", "yes", "on")
        if not enabled:
            return
        direction = (
            icp.get_param("shopify_order_ops.product_sync_direction") or ""
        ).strip()
        if direction not in ("odoo_to_shopify", "two_way"):
            return
        Job = self.env["shopify.sync.job"].sudo()
        for template in self:
            variant = template.product_variant_id
            if not variant or not variant.default_code or not template.sale_ok:
                continue
            if is_charge_sku(variant.default_code):
                continue
            pending = Job.search_count(
                [
                    ("job_type", "=", "product_update_push"),
                    ("state", "in", ["pending", "processing"]),
                    ("payload", "ilike", '"product_id": %s' % variant.id),
                ]
            )
            if pending:
                continue
            Job.enqueue(
                name="product_update_push %s" % template.display_name,
                job_type="product_update_push",
                payload_dict={
                    "product_id": variant.id,
                    "template_id": template.id,
                    "name": template.display_name,
                },
            )


class ProductProductPushHook(models.Model):
    """SKU/barcode live on the variant — hook its writes too."""

    _inherit = "product.product"

    def write(self, vals):
        res = super().write(vals)
        if set(vals) & _PUSH_VARIANT_FIELDS:
            try:
                self.mapped("product_tmpl_id")._enqueue_product_push()
            except Exception:  # noqa: BLE001
                _logger.exception("product_update_push: variant write hook failed")
        return res
