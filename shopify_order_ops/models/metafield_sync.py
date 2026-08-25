from datetime import date

from odoo import fields, models

VALUE_TOLERANCE = 0.001

# applies_to -> (Odoo field holding the numeric Shopify id, gid template)
_GID_BY_APPLIES_TO = {
    "product": ("shopify_product_id", "gid://shopify/Product/{}"),
    "customer": ("shopify_customer_id", "gid://shopify/Customer/{}"),
    "order": ("shopify_order_id", "gid://shopify/Order/{}"),
}

_METAFIELD_QUERY = """
query($id: ID!, $ns: String!, $key: String!) {
  node(id: $id) {
    ... on HasMetafields {
      metafield(namespace: $ns, key: $key) { value }
    }
  }
}
"""

_METAFIELDS_SET_MUTATION = """
mutation($metafields: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $metafields) {
    metafields { id }
    userErrors { field message }
  }
}
"""


class ShopifyMetafieldSync(models.AbstractModel):
    _name = "shopify.metafield.sync"
    _description = "Syncs Shopify metafields per the configured mappings"

    def cron_sync_metafields(self, limit=100):
        """Apply the active shopify.metafield.map rules.

        Contract (implemented by agent task F):
        - Respect the `metafield_sync_enabled` flag.
        - Iterate active mappings (each has applies_to, direction,
          shopify_namespace/key/type, odoo_model, odoo_field).
        - Resolve the Shopify owner gid per Odoo record:
          product -> product.product.shopify_product_id (product-level
          metafields), customer -> res.partner.shopify_customer_id, order ->
          sale.order.shopify_order_id. Skip records without a gid.
        - shopify_to_odoo: GraphQL the metafield value:
          query($id: ID!, $ns: String!, $key: String!) {
            node(id: $id) { ... on HasMetafields {
              metafield(namespace: $ns, key: $key) { value } } } }
          with gid like 'gid://shopify/Product/<id>'. Write the value into the
          Odoo field with type coercion (bool/int/float/char/text/date based on
          the Odoo field's ttype). Skip when unchanged.
        - odoo_to_shopify: read the Odoo field; if empty skip; call the
          metafieldsSet mutation with ownerId=gid, namespace, key, type and
          value=str(val); check userErrors in the response.
        - Validate odoo_field exists on the model once per mapping; log one
          error and skip the mapping if invalid.
        - Process at most `limit` records per mapping per run; per-record
          try/except: log and continue.
        """
        log = self.env["shopify.sync.log"]
        api = self.env["shopify.api.client"]
        if not self._flag_enabled(api):
            log.log_event(
                "info", "Metafield sync is disabled; skipping run.", source="metafield"
            )
            return
        mappings = self.env["shopify.metafield.map"].search([("active", "=", True)])
        for mapping in mappings:
            self._sync_mapping(api, log, mapping, limit)

    # --- per mapping ------------------------------------------------------
    def _sync_mapping(self, api, log, mapping, limit):
        model = self.env[mapping.odoo_model]
        if mapping.odoo_field not in model._fields:
            log.log_event(
                "error",
                f"Metafield mapping '{mapping.name}': field '{mapping.odoo_field}' "
                f"does not exist on {mapping.odoo_model}; mapping skipped.",
                source="metafield",
            )
            return
        id_field, gid_template = _GID_BY_APPLIES_TO.get(mapping.applies_to, (None, None))
        if not id_field:
            log.log_event(
                "error",
                f"Metafield mapping '{mapping.name}': unknown applies_to "
                f"'{mapping.applies_to}'; mapping skipped.",
                source="metafield",
            )
            return
        records = api.rotating_batch(
            mapping.odoo_model,
            [(id_field, "!=", False)],
            limit,
            "metafield_sync_cursor_%s" % mapping.id,
        )
        stats = {"processed": 0, "updated": 0, "skipped": 0, "errors": 0}
        for record in records:
            stats["processed"] += 1
            gid = gid_template.format(record[id_field])
            try:
                if mapping.direction == "odoo_to_shopify":
                    outcome = self._push_metafield(api, log, mapping, record, gid)
                else:
                    outcome = self._pull_metafield(api, mapping, record, gid)
                stats[outcome] += 1
            except Exception as exc:  # noqa: BLE001 - per-record isolation
                stats["errors"] += 1
                log.log_event(
                    "error",
                    f"Metafield sync failed for {mapping.odoo_model} {record.id} "
                    f"(mapping '{mapping.name}'): {exc}",
                    source="metafield",
                )
        log.log_event(
            "info",
            f"Metafield mapping '{mapping.name}' ({mapping.direction}) complete: "
            f"processed={stats['processed']}, updated={stats['updated']}, "
            f"skipped={stats['skipped']}, errors={stats['errors']}.",
            source="metafield",
        )

    # --- shopify -> odoo --------------------------------------------------
    def _pull_metafield(self, api, mapping, record, gid):
        data = api.graphql(
            _METAFIELD_QUERY,
            {"id": gid, "ns": mapping.shopify_namespace, "key": mapping.shopify_key},
        )
        node = data.get("node") or {}
        metafield = node.get("metafield") or {}
        value = metafield.get("value")
        if value is None:
            return "skipped"
        field = self.env[mapping.odoo_model]._fields[mapping.odoo_field]
        new_value = self._coerce_value(field, value)
        if not self._value_changed(field, record[mapping.odoo_field], new_value):
            return "skipped"
        record[mapping.odoo_field] = new_value
        return "updated"

    @staticmethod
    def _coerce_value(field, value):
        """Convert the Shopify metafield string value to the Odoo field's type."""
        ttype = field.ttype
        if ttype == "boolean":
            return str(value).strip().lower() in ("true", "1", "yes")
        if ttype == "integer":
            return int(float(value))
        if ttype in ("float", "monetary"):
            return float(value)
        if ttype == "date":
            return date.fromisoformat(str(value)[:10])
        if ttype == "datetime":
            return fields.Datetime.to_datetime(value)
        return str(value)

    @staticmethod
    def _value_changed(field, current, new):
        """True when writing `new` would change the stored value."""
        if field.ttype in ("float", "monetary"):
            return abs((current or 0.0) - (new or 0.0)) > VALUE_TOLERANCE
        if field.ttype == "boolean":
            return bool(current) != bool(new)
        if field.ttype == "integer":
            return (current or 0) != (new or 0)
        # char/text/date/datetime/...: Odoo stores empty values as False
        if not current and not new:
            return False
        return current != new

    # --- odoo -> shopify --------------------------------------------------
    def _push_metafield(self, api, log, mapping, record, gid):
        value = record[mapping.odoo_field]
        if not value:
            return "skipped"
        data = api.graphql(
            _METAFIELDS_SET_MUTATION,
            {
                "metafields": [
                    {
                        "ownerId": gid,
                        "namespace": mapping.shopify_namespace,
                        "key": mapping.shopify_key,
                        "type": mapping.shopify_type,
                        "value": str(value),
                    }
                ]
            },
        )
        result = data.get("metafieldsSet") or {}
        user_errors = result.get("userErrors") or []
        if user_errors:
            details = "; ".join(
                f"{'.'.join(err.get('field') or [])}: {err.get('message')}"
                for err in user_errors
            )
            log.log_event(
                "error",
                f"metafieldsSet userErrors for {mapping.odoo_model} {record.id} "
                f"(mapping '{mapping.name}'): {details}",
                source="metafield",
            )
            return "errors"
        return "updated"

    # --- misc ---------------------------------------------------------------
    @staticmethod
    def _flag_enabled(api):
        """ir.config_parameter stores booleans as strings; accept truthy spellings."""
        return str(api._param("metafield_sync_enabled") or "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
