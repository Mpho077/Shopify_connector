import logging

from odoo import fields, models

_logger = logging.getLogger(__name__)

# GraphQL: resolve a variant (and its inventory item) by exact SKU.
_VARIANT_BY_SKU_QUERY = """
query($q: String!) {
  productVariants(first: 3, query: $q) {
    edges {
      node {
        sku
        inventoryItem {
          id
        }
      }
    }
  }
}
"""


class ProductProduct(models.Model):
    _inherit = "product.product"

    shopify_last_pushed_qty = fields.Float(
        string="Last qty pushed to Shopify", default=0.0, copy=False
    )
    shopify_stock_dirty = fields.Boolean(
        string="Shopify stock changed",
        default=False,
        copy=False,
        index=True,
        help="Flagged by done stock moves in near-real-time mode; "
             "cleared once the level has been pushed to Shopify.",
    )


class ShopifyInventorySync(models.AbstractModel):
    _name = "shopify.inventory.sync"
    _description = "Pushes Odoo stock levels to Shopify"

    # --- config helpers ---------------------------------------------------
    def _inventory_sync_enabled(self):
        raw = self.env["shopify.api.client"]._param("inventory_sync_enabled")
        return raw is True or str(raw or "").strip() in ("True", "1")

    def _inventory_mode(self):
        """'scheduled' (default) | 'near_realtime' | 'disabled'."""
        raw = self.env["shopify.api.client"]._param("inventory_mode")
        return (raw or "scheduled").strip()

    def _only_instock(self):
        raw = self.env["shopify.api.client"]._param("stock_sync_only_instock")
        return raw is True or str(raw or "").strip() in ("True", "1")

    def _quantity(self, product, location=None):
        """Return the configured stock quantity for a product.

        ``stock_quantity_mode``: 'on_hand' -> qty_available,
        'free' (default when unset) -> free_qty,
        'available' -> virtual_available.
        When a location (record or id) is given, compute within that
        location via context.
        """
        raw = self.env["shopify.api.client"]._param("stock_quantity_mode")
        mode = (raw or "free").strip()
        if location:
            loc_id = location.id if hasattr(location, "id") else int(location)
            product = product.with_context(location=loc_id)
        field_name = {
            "on_hand": "qty_available",
            "available": "virtual_available",
        }.get(mode, "free_qty")
        return float(getattr(product, field_name, 0.0) or 0.0)

    def _find_inventory_item_id(self, client, sku):
        """Return the numeric Shopify inventory item id for an exact SKU match."""
        data = client.graphql(_VARIANT_BY_SKU_QUERY, {"q": f"sku:{sku}"})
        edges = (data.get("productVariants") or {}).get("edges") or []
        for edge in edges:
            node = edge.get("node") or {}
            if (node.get("sku") or "").strip() != sku:
                continue
            gid = (node.get("inventoryItem") or {}).get("id") or ""
            # gid looks like 'gid://shopify/InventoryItem/12345'
            tail = gid.rstrip("/").rsplit("/", 1)[-1]
            if tail.isdigit():
                return int(tail)
        return None

    # --- push machinery -----------------------------------------------------
    def _push_product(self, client, product, sku, mappings, default_location_id):
        """Push one product's inventory level(s) to Shopify.

        Returns 'pushed', 'skipped' (nothing to do: unchanged or filtered
        out) or 'failed' (worth retrying later). Raises nothing itself for
        expected skip conditions; API errors propagate to the caller's
        per-product guard.
        """
        log = self.env["shopify.sync.log"]
        only_instock = self._only_instock()

        if mappings:
            loc_qtys = [
                (mapping, self._quantity(product, mapping.odoo_location_id))
                for mapping in mappings
            ]
            aggregate = sum(qty for _, qty in loc_qtys)
        else:
            loc_qtys = []
            aggregate = self._quantity(product)

        # Aggregate guard: nothing changed since the last successful push.
        if abs(aggregate - (product.shopify_last_pushed_qty or 0.0)) < 0.001:
            return "skipped"
        # In-stock filter: never push zero/negative aggregate availability.
        if only_instock and aggregate <= 0:
            return "skipped"

        inventory_item_id = self._find_inventory_item_id(client, sku)
        if not inventory_item_id:
            log.log_event(
                "warning",
                f"No Shopify variant found for SKU {sku!r}; "
                "skipping inventory push.",
                source="inventory",
            )
            return "failed"

        if mappings:
            pushed_any = False
            for mapping, qty in loc_qtys:
                if only_instock and qty <= 0:
                    continue  # don't zero out a location under the filter
                shopify_loc = int((mapping.shopify_location_id or "").strip())
                client.post(
                    "inventory_levels/set.json",
                    {
                        "location_id": shopify_loc,
                        "inventory_item_id": inventory_item_id,
                        "available": int(qty),
                    },
                )
                pushed_any = True
                log.log_event(
                    "info",
                    f"Pushed inventory for SKU {sku}: available={int(qty)} "
                    f"(location {shopify_loc} "
                    f"{mapping.shopify_location_name or ''}).",
                    source="inventory",
                )
            if not pushed_any:
                return "skipped"
        else:
            client.post(
                "inventory_levels/set.json",
                {
                    "location_id": default_location_id,
                    "inventory_item_id": inventory_item_id,
                    "available": int(aggregate),
                },
            )
            log.log_event(
                "info",
                f"Pushed inventory for SKU {sku}: available={int(aggregate)} "
                f"(location {default_location_id}).",
                source="inventory",
            )

        product.shopify_last_pushed_qty = aggregate
        return "pushed"

    def _push_products(self, products):
        """Push a batch of products, isolated per product.

        Returns {product_id: 'pushed'|'skipped'|'failed'}. Returns {} when
        the run is aborted by configuration (logged once here).
        """
        log = self.env["shopify.sync.log"]
        client = self.env["shopify.api.client"]

        mappings = self.env["shopify.location.map"].sudo().search(
            [("active", "=", True)], order="id"
        )
        default_location_id = None
        if not mappings:
            location_id = (client._param("location_id") or "").strip()
            if not location_id:
                log.log_event(
                    "error",
                    "Inventory push aborted: Shopify location_id is not "
                    "configured (Settings -> Shopify Ops) and no active "
                    "location mappings exist.",
                    source="inventory",
                )
                return {}
            try:
                default_location_id = int(location_id)
            except ValueError:
                log.log_event(
                    "error",
                    f"Inventory push aborted: location_id {location_id!r} "
                    "is not numeric.",
                    source="inventory",
                )
                return {}

        results = {}
        for product in products:
            sku = (product.default_code or "").strip()
            try:
                results[product.id] = self._push_product(
                    client, product, sku, mappings, default_location_id
                )
            except Exception as exc:  # keep the cron alive on bad records
                log.log_event(
                    "error",
                    f"Inventory push failed for SKU {sku or product.id}: {exc}",
                    source="inventory",
                )
                results[product.id] = "failed"
        return results

    # --- cron entry points --------------------------------------------------
    def cron_push_inventory(self, limit=200, manual=False):
        """Scheduled sweep: push quantities to Shopify inventory levels.

        - Respects the `inventory_sync_enabled` master flag; no-op when off.
        - `inventory_mode` == 'disabled' skips this scheduled path (logged);
          pass manual=True (Settings button) to force a run anyway.
          'scheduled' and 'near_realtime' both run (the sweep is the
          backstop for near-real-time mode).
        - Only storable products (is_storable) with a default_code (SKU).
        - Quantity comes from _quantity() per `stock_quantity_mode`;
          `stock_sync_only_instock` skips zero/negative quantities.
        - With active shopify.location.map records, pushes per mapped
          Odoo location to its Shopify location; otherwise pushes the
          aggregate to the configured `location_id`.
        - shopify_last_pushed_qty is an aggregate guard: skip when
          unchanged; update after a successful push.
        - Failures are logged per product (source='inventory'); the rest
          of the batch continues.
        """
        log = self.env["shopify.sync.log"]

        if not self._inventory_sync_enabled():
            log.log_event(
                "info",
                "Inventory push skipped: inventory_sync_enabled is off.",
                source="inventory",
            )
            return
        if self._inventory_mode() == "disabled" and not manual:
            log.log_event(
                "info",
                "Scheduled inventory push skipped: inventory_mode is "
                "'disabled' (manual push via Settings still runs).",
                source="inventory",
            )
            return

        domain = [("is_storable", "=", True), ("default_code", "!=", False)]
        # 'Sync from' cutover: untouched historical products are skipped
        # unless they are already linked to Shopify.
        sync_from = self.env["shopify.api.client"].sync_from()
        if sync_from:
            domain += [
                "|",
                ("shopify_variant_id", "!=", False),
                ("write_date", ">=", sync_from),
            ]
        products = self.env["shopify.api.client"].rotating_batch(
            "product.product",
            domain,
            limit,
            "inventory_sync_cursor",
        )
        self._push_products(products)

    def cron_push_inventory_dirty(self, limit=200):
        """Near-real-time push: only products flagged shopify_stock_dirty.

        Runs every few minutes via cron. Only active when
        `inventory_mode` == 'near_realtime'; any other mode logs an info
        line and returns. The dirty flag is cleared after a successful
        push (or a deliberate skip — unchanged/filtered); it is kept on
        failure so the next run retries. Silent when nothing is dirty.
        """
        log = self.env["shopify.sync.log"]

        # Return silently when this mode is off — logging a skip line every
        # 5 minutes buries real events in the dashboard feed.
        if not self._inventory_sync_enabled():
            return
        if self._inventory_mode() != "near_realtime":
            return

        products = self.env["product.product"].search(
            [
                ("shopify_stock_dirty", "=", True),
                ("is_storable", "=", True),
                ("default_code", "!=", False),
            ],
            order="id",
            limit=limit,
        )
        if not products:
            return

        results = self._push_products(products)
        done_ids = [
            pid for pid, status in results.items() if status in ("pushed", "skipped")
        ]
        if done_ids:
            self.env["product.product"].browse(done_ids).sudo().write(
                {"shopify_stock_dirty": False}
            )
        pushed = sum(1 for s in results.values() if s == "pushed")
        failed = sum(1 for s in results.values() if s == "failed")
        log.log_event(
            "info",
            f"Near-real-time inventory push: {pushed} pushed, "
            f"{len(results) - pushed - failed} skipped (unchanged/filtered), "
            f"{failed} failed (kept dirty for retry).",
            source="inventory",
        )


class StockMove(models.Model):
    _inherit = "stock.move"

    def _action_done(self, cancel_backorder=False):
        moves = super()._action_done(cancel_backorder=cancel_backorder)
        # Near-real-time mode: flag affected products so the dirty-push
        # cron picks them up. Must never break stock operations.
        try:
            if self.env["shopify.api.client"]._param("inventory_mode") == "near_realtime":
                products = moves.mapped("product_id")
                if products:
                    products.sudo().write({"shopify_stock_dirty": True})
        except Exception:
            _logger.exception(
                "Shopify Ops: could not flag products for near-real-time "
                "inventory push; stock operation completed normally."
            )
        return moves
