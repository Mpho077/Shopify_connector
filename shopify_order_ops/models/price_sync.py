from odoo import fields, models

PRICE_TOLERANCE = 0.001


class ProductProduct(models.Model):
    _inherit = "product.product"

    shopify_last_pushed_price = fields.Float(
        string="Last price pushed to Shopify",
        default=0.0,
        copy=False,
        help="Last price known to be in sync with Shopify. In two-way mode "
        "this is the anti-oscillation marker for BOTH directions: a side is "
        "only treated as changed when its price differs from this value.",
    )
    shopify_compare_at_price = fields.Float(
        string="Shopify Compare-at (RRP)",
        default=0.0,
        copy=False,
        help="Recommended retail price shown struck-through on the Shopify "
        "storefront (compare_at_price). 0 = no compare-at price.",
    )
    shopify_last_pushed_compare_at = fields.Float(
        string="Last compare-at pushed to Shopify",
        default=0.0,
        copy=False,
        help="Anti-oscillation marker for the compare-at price, same role "
        "as shopify_last_pushed_price for the selling price.",
    )


class ProductTemplate(models.Model):
    """Edit the RRP on the product form; writes through to the variant."""

    _inherit = "product.template"

    shopify_compare_at_price = fields.Float(
        related="product_variant_id.shopify_compare_at_price",
        readonly=False,
        string="Shopify Compare-at (RRP)",
    )


class ShopifyPriceSync(models.AbstractModel):
    _name = "shopify.price.sync"
    _description = "Keeps variant prices aligned between Shopify and Odoo"

    def cron_sync_prices(self, limit=200):
        """Push or pull variant prices per the configured direction.

        Contract (implemented by agent task F):
        - Respect the `price_sync_enabled` flag; read `price_sync_direction`
          ('odoo_to_shopify' default — ERP is the price master — or
          'shopify_to_odoo', or 'two_way' — no price master).
        - Only product.product records with shopify_variant_id set.
        - odoo_to_shopify: if abs(product.lst_price - shopify_last_pushed_price)
          > 0.001, PUT variants/{id}.json {'variant': {'id': vid, 'price':
          str(price)}}; then store shopify_last_pushed_price.
        - shopify_to_odoo: GET variants/{id}.json, write price into
          product.lst_price when it differs; update shopify_last_pushed_price.
        - two_way: PULL phase then PUSH phase over the same product set, both
          keyed on shopify_last_pushed_price as the last-SYNCED marker (see
          _sync_two_way). Last change wins; cannot oscillate.
        - Currency guard: fetch shop.json once per run; if shop currency differs
          from the company currency, log one warning and proceed unchanged.
        - Per-product try/except: log and continue.
        """
        log = self.env["shopify.sync.log"]
        api = self.env["shopify.api.client"]
        if not self._flag_enabled(api, "price_sync_enabled"):
            log.log_event("info", "Price sync is disabled; skipping run.", source="price")
            return
        direction = api._param("price_sync_direction", "odoo_to_shopify") or "odoo_to_shopify"
        compare_at = self._compare_at_enabled(api)
        self._check_currency(api, log)
        products = api.rotating_batch(
            "product.product",
            [("shopify_variant_id", "!=", False)],
            limit,
            "price_sync_cursor",
        )
        if direction == "two_way":
            self._sync_two_way(api, log, products, compare_at)
            return
        stats = {"processed": 0, "updated": 0, "skipped": 0, "errors": 0}
        for product in products:
            stats["processed"] += 1
            try:
                if direction == "shopify_to_odoo":
                    outcome = self._pull_price(api, product, compare_at)
                else:
                    outcome = self._push_price(api, product, compare_at)
                stats[outcome] += 1
            except Exception as exc:  # noqa: BLE001 - per-product isolation
                stats["errors"] += 1
                log.log_event(
                    "error",
                    f"Price sync failed for product {product.id} "
                    f"(variant {product.shopify_variant_id}): {exc}",
                    source="price",
                )
        log.log_event(
            "info",
            f"Price sync run complete (direction={direction}): "
            f"processed={stats['processed']}, updated={stats['updated']}, "
            f"skipped={stats['skipped']}, errors={stats['errors']}.",
            source="price",
        )

    # --- helpers --------------------------------------------------------
    @staticmethod
    def _flag_enabled(api, key):
        """ir.config_parameter stores booleans as strings; accept truthy spellings."""
        return str(api._param(key) or "").strip().lower() in ("1", "true", "yes", "on")

    @staticmethod
    def _compare_at_enabled(api):
        """compare_at_sync_enabled: default ON (unset = on), explicit falsy disables."""
        raw = api._param("compare_at_sync_enabled")
        if raw is None or str(raw).strip() == "":
            return True
        return str(raw).strip().lower() in ("1", "true", "yes", "on")

    def _check_currency(self, api, log):
        """Fetch shop.json once per run; warn (once) on currency mismatch, then proceed."""
        try:
            shop = api.get("shop.json").get("shop") or {}
        except Exception as exc:  # noqa: BLE001 - guard must not kill the run
            log.log_event(
                "warning",
                f"Could not fetch shop.json for the currency check: {exc}. "
                "Proceeding with the price sync unchanged.",
                source="price",
            )
            return
        shop_currency = shop.get("currency")
        company_currency = self.env.company.currency_id.name
        if shop_currency and company_currency and shop_currency != company_currency:
            log.log_event(
                "warning",
                f"Shop currency {shop_currency} differs from the Odoo company "
                f"currency {company_currency}; prices are synced as-is.",
                source="price",
            )

    @staticmethod
    def _push_price(api, product, compare_at=False):
        """Odoo is the price master: push what moved since the last push.

        With compare_at enabled, the RRP field rides along in the same
        variant PUT (compare_at_price, cleared with null when Odoo is 0)."""
        payload = {}
        if abs(product.lst_price - product.shopify_last_pushed_price) > PRICE_TOLERANCE:
            payload["price"] = str(product.lst_price)
        if compare_at and abs(
            product.shopify_compare_at_price - product.shopify_last_pushed_compare_at
        ) > PRICE_TOLERANCE:
            payload["compare_at_price"] = (
                str(product.shopify_compare_at_price)
                if product.shopify_compare_at_price > 0
                else None
            )
        if not payload:
            return "skipped"
        vid = product.shopify_variant_id
        api.put(f"variants/{vid}.json", {"variant": dict(payload, id=int(vid))})
        if "price" in payload:
            product.shopify_last_pushed_price = product.lst_price
        if "compare_at_price" in payload:
            product.shopify_last_pushed_compare_at = product.shopify_compare_at_price
        return "updated"

    @staticmethod
    def _pull_price(api, product, compare_at=False):
        """Shopify is the price master: pull the variant price into lst_price.

        With compare_at enabled, the Shopify compare_at_price (RRP) is pulled
        into shopify_compare_at_price as well (null -> 0)."""
        data = api.get(f"variants/{product.shopify_variant_id}.json")
        variant = data.get("variant") or {}
        price = float(variant.get("price"))
        updated = "skipped"
        if abs(price - product.lst_price) > PRICE_TOLERANCE:
            product.lst_price = price
            updated = "updated"
        product.shopify_last_pushed_price = price
        if compare_at:
            remote_rrp = ShopifyPriceSync._remote_compare_at(variant)
            if abs(remote_rrp - product.shopify_compare_at_price) > PRICE_TOLERANCE:
                product.shopify_compare_at_price = remote_rrp
                updated = "updated"
            product.shopify_last_pushed_compare_at = remote_rrp
        return updated

    @staticmethod
    def _remote_compare_at(variant):
        """Shopify compare_at_price as float; null/absent -> 0.0."""
        raw = variant.get("compare_at_price")
        if raw in (None, ""):
            return 0.0
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0

    # --- two-way (no price master) ----------------------------------------
    def _sync_two_way(self, api, log, products, compare_at=False):
        """Last change wins in either direction, without oscillation.

        shopify_last_pushed_price doubles as the last-SYNCED marker for both
        sides: a side is treated as changed only when its current price
        differs from the marker by more than 0.001. Whichever phase writes a
        price also re-bases the marker, so the other phase of the same run
        (and the next run) sees "no change" and stays silent.

        PULL runs before PUSH so a same-cycle Shopify edit lands in Odoo
        before Odoo pushes anything back. If both sides changed since the
        last run, the pull wins that cycle — the documented last-change-wins
        behavior.
        """
        self._run_phase(
            api, log, products, self._pull_price_two_way, "PULL (Shopify -> Odoo)",
            compare_at,
        )
        self._run_phase(
            api, log, products, self._push_price, "PUSH (Odoo -> Shopify)",
            compare_at,
        )

    @staticmethod
    def _pull_price_two_way(api, product, compare_at=False):
        """Two-way pull: adopt the Shopify price only if it moved since the last sync."""
        data = api.get(f"variants/{product.shopify_variant_id}.json")
        variant = data.get("variant") or {}
        price = float(variant.get("price"))
        updated = "skipped"
        if abs(price - product.shopify_last_pushed_price) > PRICE_TOLERANCE:
            product.lst_price = price
            product.shopify_last_pushed_price = price
            updated = "updated"
        if compare_at:
            remote_rrp = ShopifyPriceSync._remote_compare_at(variant)
            if abs(remote_rrp - product.shopify_last_pushed_compare_at) > PRICE_TOLERANCE:
                product.shopify_compare_at_price = remote_rrp
                product.shopify_last_pushed_compare_at = remote_rrp
                updated = "updated"
        return updated

    def _run_phase(self, api, log, products, handler, label, compare_at=False):
        """Run one sync phase over all products with per-product isolation."""
        stats = {"processed": 0, "updated": 0, "skipped": 0, "errors": 0}
        for product in products:
            stats["processed"] += 1
            try:
                outcome = handler(api, product, compare_at)
                stats[outcome] += 1
            except Exception as exc:  # noqa: BLE001 - per-product isolation
                stats["errors"] += 1
                log.log_event(
                    "error",
                    f"Price sync failed for product {product.id} "
                    f"(variant {product.shopify_variant_id}): {exc}",
                    source="price",
                )
        log.log_event(
            "info",
            f"Two-way price sync {label} phase complete: "
            f"processed={stats['processed']}, updated={stats['updated']}, "
            f"skipped={stats['skipped']}, errors={stats['errors']}.",
            source="price",
        )
        return stats

    # ------------------------------------------------------------------
    # webhook-driven price updates (Shopify -> Odoo, real-time)
    # ------------------------------------------------------------------
    def process_price_update(self, job):
        """Handle a products/update webhook: pull changed variant prices.

        The products/update payload already contains the product with its
        variants and prices, so no extra API fetch is needed. Honored only
        when the price direction accepts Shopify-side changes ('two_way' or
        'shopify_to_odoo'); in master mode ('odoo_to_shopify') the push side
        owns prices and the webhook is acknowledged but ignored.
        """
        log = self.env["shopify.sync.log"]
        api = self.env["shopify.api.client"]

        if not self._flag_enabled(api, "price_sync_enabled"):
            log.log_event("info", "Price update webhook ignored: price sync disabled.", source="price", job=job)
            return
        direction = api._param("price_sync_direction", "odoo_to_shopify") or "odoo_to_shopify"
        if direction == "odoo_to_shopify":
            log.log_event(
                "info",
                "Price update webhook ignored: direction is Odoo -> Shopify (master mode).",
                source="price",
                job=job,
            )
            return

        payload = job.payload_dict()
        raw = payload.get("raw") or {}
        variants = raw.get("variants") or []
        compare_at = self._compare_at_enabled(api)
        if not variants:
            log.log_event(
                "warning",
                "Price update webhook for product %s carried no variants; nothing to do."
                % raw.get("id"),
                source="price",
                job=job,
            )
            return

        Product = self.env["product.product"].sudo()
        updated = skipped = unmatched = errors = 0
        for variant in variants:
            try:
                vid = str(variant.get("id") or "")
                try:
                    price = float(variant.get("price") or 0)
                except (TypeError, ValueError):
                    price = 0.0
                product = Product.search([("shopify_variant_id", "=", vid)], limit=1)
                if not product:
                    sku = (variant.get("sku") or "").strip()
                    if sku:
                        product = api.match_product_by_sku(sku)
                        if product and not product.shopify_variant_id:
                            product.shopify_variant_id = vid
                if not product:
                    unmatched += 1
                    continue
                vals = {}
                if abs(product.shopify_last_pushed_price - price) > PRICE_TOLERANCE:
                    vals["lst_price"] = price
                    vals["shopify_last_pushed_price"] = price
                if compare_at:
                    remote_rrp = self._remote_compare_at(variant)
                    if abs(product.shopify_last_pushed_compare_at - remote_rrp) > PRICE_TOLERANCE:
                        vals["shopify_compare_at_price"] = remote_rrp
                        vals["shopify_last_pushed_compare_at"] = remote_rrp
                if not vals:
                    skipped += 1  # echo of the last sync; nobody edited
                    continue
                product.write(vals)
                updated += 1
                log.log_event(
                    "info",
                    "Webhook price update: %s (variant %s) -> %s in Odoo."
                    % (
                        product.display_name,
                        vid,
                        ", ".join(
                            "%s %.2f" % (label, vals[key])
                            for key, label in (
                                ("lst_price", "price"),
                                ("shopify_compare_at_price", "RRP"),
                            )
                            if key in vals
                        ),
                    ),
                    source="price",
                    job=job,
                )
            except Exception as exc:  # noqa: BLE001 - per-variant isolation
                errors += 1
                log.log_event(
                    "error",
                    "Webhook price update failed for variant %s: %s"
                    % (variant.get("id"), exc),
                    source="price",
                    job=job,
                )
        log.log_event(
            "info",
            "Price update webhook processed: updated=%d, unchanged=%d, "
            "unmatched=%d, errors=%d." % (updated, skipped, unmatched, errors),
            source="price",
            job=job,
        )
