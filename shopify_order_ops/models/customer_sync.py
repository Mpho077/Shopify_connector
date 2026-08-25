import re
from datetime import timezone

from odoo import fields, models
from odoo.fields import Command

# ir.config_parameter key holding the ISO 8601 watermark of the last sweep.
WATERMARK_PARAM = "shopify_order_ops.last_customer_sync"
# Shopify Admin REST page size for customers.json.
PAGE_SIZE = 250
# shopify.sync.log source tag for this subsystem.
LOG_SOURCE = "customer"
# Values of `customer_sync_enabled` treated as "off" (str or bool).
_DISABLED = ("", "0", "false", "none", "no", "off")
# Values of a boolean-ish config parameter treated as "on" (str or bool).
_ENABLED = ("1", "true", "yes", "on")


class ResPartner(models.Model):
    _inherit = "res.partner"

    shopify_customer_id = fields.Char(
        string="Shopify Customer ID", index=True, copy=False
    )
    # Two-way mode: when this partner's core fields were last checked against
    # Shopify for push-side updates (see _push_linked_updates). NULL = never
    # checked; the sweep runs oldest-checked first so coverage rotates.
    shopify_push_checked_at = fields.Datetime(copy=False)


class ShopifyCustomerSync(models.AbstractModel):
    _name = "shopify.customer.sync"
    _description = "Syncs Shopify customers into Odoo contacts"

    def cron_sync_customers(self, limit=100):
        """Pull Shopify customers into Odoo res.partner records.

        Contract (implemented by agent task D):
        - Respect the `customer_sync_enabled` config flag; no-op when off.
        - Direction (customer_sync_direction): 'shopify_to_odoo' (default)
          runs only the pull below; 'odoo_to_shopify' runs only the push;
          'two_way' runs pull, then push, then linked-partner update checks.
        - Incremental: keep a watermark in ir.config_parameter
          `shopify_order_ops.last_customer_sync` (ISO datetime); fetch customers
          with customers.json (updated_at_min=watermark, limit=250, paginate
          with since_id until a short page). Update the watermark only after a
          successful run.
        - Dedupe: shopify_customer_id first, then normalized email. Update
          existing partners; never create duplicates.
        - Map: first+last name -> name; email, phone; company (default_address
          .company) -> create/find a company partner and set parent_id;
          default_address -> street/street2/city/zip/state_id/country_id
          (resolve state/country by code); additional addresses -> child
          contacts of type 'delivery' (dedupe by address content).
        - Tags -> res.partner.category entries (create if missing), linked m2m.
        - Process at most `limit` NEW partner creates per run; log failures per
          customer and continue.
        - Customer settings pack (pull path only; push direction unaffected):
          * customer_create_as: 'individual' (default) | 'company'. 'company'
            creates NEW partners with is_company=True; the name falls back to
            default_address.company when the customer has no first/last name.
          * customer_b2b_sync: on top of the existing parent-company linking,
            backfill the company partner's shopify_customer_id (only when it
            has none) and fill its commercial_company_name when empty.
          * customer_min_orders_enabled + customer_min_orders: skip customers
            whose Shopify orders_count is below the threshold.
          * customer_b2b_only: skip customers without default_address.company.
          * customer_sync_tags: comma-separated allow-list of Shopify tag
            names; empty = all. Case-insensitive.
          Filters run before any partner lookup (so filtered customers never
          consume the creation cap); skips are counted per reason and
          reported in the run summary.
        - Two-way echo safety: the pull stamps shopify_customer_id on every
          partner it touches and the push only considers partners without
          one, so freshly pulled records are never pushed back; push-side
          updates are PUT only on a real normalized difference, and the
          resulting Shopify change comes back through the next pull as
          "unchanged" (pull updates are diff-based via _changed_vals).
        """
        client = self.env["shopify.api.client"]
        log = self.env["shopify.sync.log"]

        enabled = client._param("customer_sync_enabled")
        if str(enabled).strip().lower() in _DISABLED:
            log.log_event(
                "info",
                "Customer sync skipped: customer_sync_enabled is disabled.",
                source=LOG_SOURCE,
            )
            return

        try:
            limit = max(int(limit), 0)
        except (TypeError, ValueError):
            limit = 100

        # Direction: default Shopify -> Odoo (pull); Odoo -> Shopify pushes
        # Odoo contacts OUT to the storefront.
        direction = (client._param("customer_sync_direction") or "").strip()
        if direction == "odoo_to_shopify":
            return self._push_odoo_to_shopify(client, log, limit)

        # Captured BEFORE the first API call so customers created mid-run are
        # picked up by the next sweep (dedupe makes the overlap harmless).
        run_start = fields.Datetime.now()
        run_start_iso = run_start.replace(tzinfo=timezone.utc).isoformat()
        watermark = (client._param("last_customer_sync") or "").strip() or None
        # 'Sync from' cutover: a first-ever pull starts at the cutover, not
        # at the beginning of Shopify history.
        if not watermark:
            watermark = client.sync_from()

        customers, pages_ok = self._fetch_customers(client, log, watermark)

        filters = self._pull_filters(client)
        self._log_active_filters(log, filters)

        created = updated = unchanged = skipped = errors = 0
        skip_reasons = {}
        cap_notice_logged = False
        for customer in customers:
            try:
                with self.env.cr.savepoint():
                    status = self._process_customer(
                        log, customer, created < limit, filters
                    )
            except Exception as exc:  # per-record isolation: log and continue
                errors += 1
                log.log_event(
                    "error",
                    "Failed to sync Shopify customer %s: %s"
                    % (customer.get("id"), exc),
                    source=LOG_SOURCE,
                )
                continue
            if status == "created":
                created += 1
            elif status == "updated":
                updated += 1
            elif status == "unchanged":
                unchanged += 1
            else:
                skipped += 1
                if status.startswith("skipped_"):
                    reason = status[len("skipped_"):]
                    skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                if status == "capped" and not cap_notice_logged:
                    cap_notice_logged = True
                    log.log_event(
                        "info",
                        "New-partner creation cap (%d) reached; further new "
                        "customers are skipped this run (updates still apply)."
                        % limit,
                        source=LOG_SOURCE,
                    )

        if pages_ok:
            self.env["ir.config_parameter"].sudo().set_param(
                WATERMARK_PARAM, run_start_iso
            )
            watermark_msg = f"watermark advanced to {run_start_iso}"
        else:
            watermark_msg = (
                f"watermark NOT advanced (kept at {watermark or 'unset'}) "
                "due to API errors"
            )

        skip_detail = ""
        if skip_reasons:
            skip_detail = " (filter skips: %s)" % ", ".join(
                "%d %s" % (count, reason.replace("_", "-"))
                for reason, count in sorted(skip_reasons.items())
            )

        log.log_event(
            "info",
            "Customer sync finished: %d created, %d updated, %d unchanged, "
            "%d skipped%s, %d errors; %s."
            % (
                created,
                updated,
                unchanged,
                skipped,
                skip_detail,
                errors,
                watermark_msg,
            ),
            source=LOG_SOURCE,
        )
        pull_summary = {
            "created": created,
            "updated": updated,
            "unchanged": unchanged,
            "skipped": skipped,
            "skipped_by_reason": skip_reasons,
            "errors": errors,
        }
        if direction == "two_way":
            # Push phase runs AFTER the pull: the pull has just stamped
            # shopify_customer_id on every partner it created or updated,
            # and the push only considers partners without one, so freshly
            # pulled records are never pushed back to Shopify (no echo).
            push_summary = self._push_odoo_to_shopify(client, log, limit)
            update_summary = self._push_linked_updates(client, log, limit)
            return {
                "pull": pull_summary,
                "push": push_summary,
                "linked_updates": update_summary,
            }
        return pull_summary

    # ------------------------------------------------------------------
    # fetching / pagination
    # ------------------------------------------------------------------
    def _fetch_customers(self, client, log, watermark):
        """Page through customers.json. Returns (customers, pages_ok)."""
        customers = []
        since_id = 0
        pages_ok = True
        while True:
            params = {"limit": PAGE_SIZE, "since_id": since_id}
            if watermark:
                params["updated_at_min"] = watermark
            try:
                data = client.get("customers.json", params=params)
            except Exception as exc:
                pages_ok = False
                log.log_event(
                    "error",
                    "Failed to fetch Shopify customers page (since_id=%s): %s"
                    % (since_id, exc),
                    source=LOG_SOURCE,
                )
                break
            page = data.get("customers") or []
            if not page:
                break
            customers.extend(page)
            since_id = max(c.get("id") or 0 for c in page)
            if len(page) < PAGE_SIZE:
                break
        return customers, pages_ok

    # ------------------------------------------------------------------
    # settings pack: filters and flags (pull path only)
    # ------------------------------------------------------------------
    def _pull_filters(self, client):
        """Read the Customer Settings pack once for a pull run."""
        create_as = (client._param("customer_create_as") or "").strip().lower()
        if create_as not in ("individual", "company"):
            create_as = "individual"
        try:
            min_orders = max(
                int(str(client._param("customer_min_orders") or "0").strip()), 0
            )
        except (TypeError, ValueError):
            min_orders = 0
        return {
            "create_as": create_as,
            "b2b_sync": self._param_on(client._param("customer_b2b_sync")),
            "min_orders_enabled": self._param_on(
                client._param("customer_min_orders_enabled")
            ),
            "min_orders": min_orders,
            "b2b_only": self._param_on(client._param("customer_b2b_only")),
            # empty set = no tag filtering
            "tags": {
                tag.strip().casefold()
                for tag in (client._param("customer_sync_tags") or "").split(",")
                if tag.strip()
            },
        }

    @staticmethod
    def _param_on(value):
        """True when a boolean-ish config parameter value is enabled."""
        return str(value).strip().lower() in _ENABLED

    def _log_active_filters(self, log, filters):
        """One info line describing the active customer settings."""
        active = []
        if filters["create_as"] == "company":
            active.append("create new partners as companies")
        if filters["b2b_sync"]:
            active.append("B2B company backfill")
        if filters["min_orders_enabled"] and filters["min_orders"] > 0:
            active.append("min %d orders" % filters["min_orders"])
        if filters["b2b_only"]:
            active.append("B2B only (default_address.company required)")
        if filters["tags"]:
            active.append("tag allow-list: %s" % ", ".join(sorted(filters["tags"])))
        if active:
            log.log_event(
                "info",
                "Customer pull settings active: %s." % "; ".join(active),
                source=LOG_SOURCE,
            )

    def _skip_reason(self, customer, filters):
        """First filter excluding this customer, or None.

        Evaluation order: min-orders, B2B-only, tag allow-list. A customer
        failing several filters is counted under the first one only.
        """
        if filters["min_orders_enabled"] and filters["min_orders"] > 0:
            try:
                orders_count = int(customer.get("orders_count") or 0)
            except (TypeError, ValueError):
                orders_count = 0
            if orders_count < filters["min_orders"]:
                return "min_orders"
        default = customer.get("default_address") or {}
        if filters["b2b_only"] and not (default.get("company") or "").strip():
            return "b2b_only"
        if filters["tags"]:
            customer_tags = {
                tag.strip().casefold()
                for tag in (customer.get("tags") or "").split(",")
                if tag.strip()
            }
            if not (customer_tags & filters["tags"]):
                return "tags"
        return None

    # ------------------------------------------------------------------
    # per-customer processing
    # ------------------------------------------------------------------
    def _process_customer(self, log, customer, can_create, filters=None):
        """Sync one Shopify customer dict. Returns a status string."""
        Partner = self.env["res.partner"].sudo()
        if filters is None:
            filters = self._pull_filters(self.env["shopify.api.client"])
        sid = str(customer.get("id") or "").strip()
        if not sid:
            log.log_event(
                "warning",
                "Shopify customer payload without an id skipped.",
                source=LOG_SOURCE,
            )
            return "skipped"

        # Settings-pack filters run before any partner lookup: filtered
        # customers never trigger searches and never consume the creation cap.
        reason = self._skip_reason(customer, filters)
        if reason:
            return "skipped_" + reason

        partner = self._find_partner(Partner, customer, sid)
        if (
            partner
            and partner.shopify_customer_id
            and partner.shopify_customer_id != sid
        ):
            log.log_event(
                "warning",
                "Shopify customer %s conflicts with partner %s, already linked "
                "to Shopify customer %s; skipped."
                % (sid, partner.display_name, partner.shopify_customer_id),
                source=LOG_SOURCE,
            )
            return "skipped"

        as_company = filters["create_as"] == "company"
        vals = self._customer_vals(customer, as_company=as_company)
        company = self._find_or_create_company(
            Partner, customer, b2b_sync=filters["b2b_sync"]
        )
        if company:
            vals["parent_id"] = company.id
        tag_ids = self._tag_ids(customer)
        if tag_ids is not None:
            vals["category_id"] = [Command.set(tag_ids)]

        if partner:
            if not partner.shopify_customer_id:
                vals["shopify_customer_id"] = sid
            if partner.parent_id:
                vals.pop("parent_id", None)  # never overwrite an existing parent
            self._prune_update_vals(customer, vals)
            changed = self._changed_vals(partner, vals)
            if changed:
                partner.write(changed)
                status = "updated"
            else:
                status = "unchanged"
        else:
            if not can_create:
                return "capped"
            vals["shopify_customer_id"] = sid
            if as_company:
                vals["is_company"] = True
            partner = Partner.create(vals)
            status = "created"

        self._sync_delivery_children(Partner, partner, customer)
        return status

    def _find_partner(self, Partner, customer, sid):
        """Locate an existing partner by Shopify id, then normalized email."""
        matches = Partner.search([("shopify_customer_id", "=", sid)], limit=10)
        if matches:
            if len(matches) == 1:
                return matches[0]
            # customer_b2b_sync backfills the parent company partner with the
            # customer's Shopify id, so one sid can match both the company
            # and the contact. Prefer an email match, then the non-company.
            email = (customer.get("email") or "").strip().lower()
            if email:
                for candidate in matches:
                    if (candidate.email or "").strip().lower() == email:
                        return candidate
            individuals = matches.filtered(lambda p: not p.is_company)
            return (individuals or matches)[0]
        email = (customer.get("email") or "").strip().lower()
        if email:
            for candidate in Partner.search([("email", "ilike", email)], limit=10):
                if (candidate.email or "").strip().lower() == email:
                    return candidate
        return Partner.browse()

    def _customer_vals(self, customer, as_company=False):
        """Map a Shopify customer dict to res.partner values.

        With as_company, default_address.company is the name fallback for
        nameless customers (the partner represents the company itself).
        """
        first = (customer.get("first_name") or "").strip()
        last = (customer.get("last_name") or "").strip()
        email = (customer.get("email") or "").strip()
        default = customer.get("default_address") or {}
        company_name = (default.get("company") or "").strip()
        name = (
            " ".join(p for p in (first, last) if p)
            or (company_name if as_company else "")
            or email
            or f"Shopify customer {customer.get('id')}"
        )
        phone = (customer.get("phone") or "").strip() or (
            default.get("phone") or ""
        ).strip()
        vals = {
            "name": name,
            "email": email or False,
            "phone": phone or False,
        }
        if default:
            vals.update(self._address_vals(default))
        return vals

    def _prune_update_vals(self, customer, vals):
        """On update, avoid wiping good Odoo data with absent Shopify data."""
        first = (customer.get("first_name") or "").strip()
        last = (customer.get("last_name") or "").strip()
        if not (first or last):
            vals.pop("name", None)  # keep a real name over a synthetic fallback
        if not vals.get("email"):
            vals.pop("email", None)
        if not vals.get("phone"):
            vals.pop("phone", None)
        if not (customer.get("default_address") or {}):
            for key in ("street", "street2", "city", "zip", "state_id",
                        "country_id"):
                vals.pop(key, None)

    def _changed_vals(self, partner, vals):
        """Keep only values that actually differ from the stored partner."""
        changed = {}
        for key, value in vals.items():
            if key == "category_id":
                if set(partner.category_id.ids) != set(value[0][2]):
                    changed[key] = value
            elif key in ("parent_id", "country_id", "state_id"):
                if partner[key].id != value:
                    changed[key] = value
            elif (partner[key] or False) != value:
                changed[key] = value
        return changed

    # ------------------------------------------------------------------
    # companies, tags, addresses
    # ------------------------------------------------------------------
    def _find_or_create_company(self, Partner, customer, b2b_sync=False):
        """Company partner for default_address.company, or None.

        With b2b_sync on, the company partner is additionally linked back to
        Shopify: its shopify_customer_id is backfilled from this customer
        (only when it has none) and its commercial_company_name is filled
        with the Shopify company name when empty. Pricelists and payment
        terms are deliberately out of scope for now.
        """
        default = customer.get("default_address") or {}
        company_name = (default.get("company") or "").strip()
        if not company_name:
            return None
        company = Partner.search(
            [("is_company", "=", True), ("name", "=", company_name)], limit=1
        )
        if not company:
            company = Partner.create({"name": company_name, "is_company": True})
        if b2b_sync:
            updates = {}
            sid = str(customer.get("id") or "").strip()
            if sid and not company.shopify_customer_id:
                updates["shopify_customer_id"] = sid
            if not company.commercial_company_name:
                updates["commercial_company_name"] = company_name
            if updates:
                company.write(updates)
        return company

    def _tag_ids(self, customer):
        """Find-or-create categories for the tags. None => leave m2m untouched."""
        raw = customer.get("tags") or ""
        names = [t.strip() for t in raw.split(",") if t.strip()]
        if not names:
            return None
        Category = self.env["res.partner.category"].sudo()
        ids = []
        for name in names:
            category = Category.search([("name", "=", name)], limit=1)
            if not category:
                category = Category.create({"name": name})
            ids.append(category.id)
        return ids

    def _address_vals(self, address):
        """Map a Shopify address dict to res.partner address values."""
        vals = {
            "street": (address.get("address1") or "").strip() or False,
            "street2": (address.get("address2") or "").strip() or False,
            "city": (address.get("city") or "").strip() or False,
            "zip": (address.get("zip") or "").strip() or False,
        }
        country_code = (address.get("country_code") or "").strip().upper()
        if country_code:
            country = (
                self.env["res.country"]
                .sudo()
                .search([("code", "=", country_code)], limit=1)
            )
            if country:
                vals["country_id"] = country.id
                province = (address.get("province_code") or "").strip().upper()
                if province:
                    state = (
                        self.env["res.country.state"]
                        .sudo()
                        .search(
                            [
                                ("country_id", "=", country.id),
                                ("code", "=", province),
                            ],
                            limit=1,
                        )
                    )
                    if state:
                        vals["state_id"] = state.id
        return vals

    def _sync_delivery_children(self, Partner, partner, customer):
        """Create missing delivery child contacts for non-default addresses."""
        addresses = customer.get("addresses") or []
        if not addresses:
            return
        default = customer.get("default_address") or {}
        default_id = default.get("id")
        default_key = self._address_key(
            default.get("address1"), default.get("zip"), default.get("city")
        )
        seen = {
            self._address_key(child.street, child.zip, child.city)
            for child in partner.child_ids
        }
        for address in addresses:
            if default_id and address.get("id") == default_id:
                continue
            key = self._address_key(
                address.get("address1"), address.get("zip"), address.get("city")
            )
            if not any(key):
                continue  # empty address record
            if any(default_key) and key == default_key:
                continue  # duplicates the default address
            if key in seen:
                continue  # already on the partner (or created earlier this run)
            first = (address.get("first_name") or "").strip()
            last = (address.get("last_name") or "").strip()
            name = (
                (address.get("name") or "").strip()
                or " ".join(p for p in (first, last) if p)
                or partner.name
            )
            vals = {
                "name": name,
                "parent_id": partner.id,
                "type": "delivery",
            }
            phone = (address.get("phone") or "").strip()
            if phone:
                vals["phone"] = phone
            vals.update(self._address_vals(address))
            Partner.create(vals)
            seen.add(key)

    @staticmethod
    def _address_key(street, zip_code, city):
        """Normalized (street, zip, city) tuple used to dedupe addresses."""
        return tuple(
            (part or "").strip().casefold()
            for part in (street, zip_code, city)
        )

    # ------------------------------------------------------------------
    # Odoo -> Shopify direction
    # ------------------------------------------------------------------

    def _push_odoo_to_shopify(self, client, log, limit):
        """Push Odoo contacts out to Shopify as customers.

        Candidates: individual (non-company, top-level) active partners with an
        email and no shopify_customer_id yet. Dedupe by email against Shopify
        first; on match, link instead of creating (never duplicates).
        """
        Partner = self.env["res.partner"].sudo()
        domain = [
            ("shopify_customer_id", "=", False),
            ("email", "!=", False),
            ("is_company", "=", False),
            ("parent_id", "=", False),
            ("active", "=", True),
        ]
        # 'Sync from' cutover: only partners touched after the cutover are
        # pushed; the existing contact book is left alone.
        sync_from = self.env["shopify.api.client"].sync_from()
        if sync_from:
            domain.append(("write_date", ">=", sync_from))
        candidates = Partner.search(
            domain,
            order="id",
            limit=limit,
        )
        created = linked = skipped = errors = 0
        for partner in candidates:
            email = (partner.email or "").strip().lower()
            if not email:
                skipped += 1
                continue
            try:
                existing = (
                    client.get(
                        "customers/search.json",
                        params={"query": "email:%s" % email},
                    ).get("customers")
                    or []
                )
                if existing:
                    partner.shopify_customer_id = str(existing[0]["id"])
                    linked += 1
                    log.log_event(
                        "info",
                        "Linked partner %s to existing Shopify customer %s."
                        % (partner.name, existing[0]["id"]),
                        source=LOG_SOURCE,
                    )
                    continue
                resp = client.post(
                    "customers.json",
                    {"customer": self._shopify_customer_payload(partner)},
                )
                new_id = ((resp or {}).get("customer") or {}).get("id")
                if not new_id:
                    raise RuntimeError(
                        "Shopify returned no customer id: %r" % (resp,)
                    )
                partner.shopify_customer_id = str(new_id)
                created += 1
                log.log_event(
                    "info",
                    "Pushed partner %s to Shopify customer %s."
                    % (partner.name, new_id),
                    source=LOG_SOURCE,
                )
            except Exception as exc:  # noqa: BLE001 - per-record isolation
                errors += 1
                log.log_event(
                    "error",
                    "Customer push failed for partner %s (id %s): %s"
                    % (partner.name, partner.id, exc),
                    source=LOG_SOURCE,
                )
        log.log_event(
            "info",
            "Customer push run: %d created, %d linked, %d skipped, %d errors."
            % (created, linked, skipped, errors),
            source=LOG_SOURCE,
        )
        return {
            "created": created,
            "linked": linked,
            "skipped": skipped,
            "errors": errors,
        }

    def _push_linked_updates(self, client, log, limit):
        """Two-way mode only: push core-field updates for linked partners.

        Takes up to `limit` linked partners per run, oldest-checked first
        (never-checked records sort first), fetches each Shopify customer,
        and PUTs customers/{id}.json only when a normalized core field
        (name split via _split_name, email lowercased, phone digits-only)
        actually differs from the Odoo value. Every checked partner is
        stamped with shopify_push_checked_at (match or not) so coverage
        rotates across runs and no record starves or is checked twice.

        Scope mirrors the push candidates: top-level, non-company, active
        partners with an email. Company partners must never be update-
        checked: customer_b2b_sync backfills them with the customer's
        Shopify id, and splitting a company name into first/last would
        corrupt the storefront record.
        """
        Partner = self.env["res.partner"].sudo()
        candidates = Partner.search(
            [
                ("shopify_customer_id", "!=", False),
                ("email", "!=", False),
                ("is_company", "=", False),
                ("parent_id", "=", False),
                ("active", "=", True),
            ],
            order="shopify_push_checked_at asc nulls first, id asc",
            limit=limit,
        )
        checked = updated = unchanged = missing = errors = 0
        checked_at = fields.Datetime.now()
        for partner in candidates:
            sid = partner.shopify_customer_id
            try:
                data = client.get("customers/%s.json" % sid)
                shopify_customer = (data or {}).get("customer") or {}
                if not shopify_customer:
                    missing += 1
                    log.log_event(
                        "warning",
                        "Linked Shopify customer %s (partner %s) not found; "
                        "link left in place." % (sid, partner.name),
                        source=LOG_SOURCE,
                    )
                elif self._linked_customer_differs(partner, shopify_customer):
                    client.put(
                        "customers/%s.json" % sid,
                        {
                            "customer": self._shopify_customer_update_payload(
                                partner
                            )
                        },
                    )
                    updated += 1
                    log.log_event(
                        "info",
                        "Pushed core-field update for partner %s to Shopify "
                        "customer %s." % (partner.name, sid),
                        source=LOG_SOURCE,
                    )
                else:
                    unchanged += 1
            except Exception as exc:  # noqa: BLE001 - per-record isolation
                errors += 1
                log.log_event(
                    "error",
                    "Linked-customer update check failed for partner %s "
                    "(Shopify customer %s): %s" % (partner.name, sid, exc),
                    source=LOG_SOURCE,
                )
            # Stamp after every check (match or not, even on error) so the
            # oldest-first sweep keeps rotating across the whole linked set.
            partner.shopify_push_checked_at = checked_at
            checked += 1
        log.log_event(
            "info",
            "Linked-customer update checks: %d checked, %d updated, "
            "%d unchanged, %d missing, %d errors."
            % (checked, updated, unchanged, missing, errors),
            source=LOG_SOURCE,
        )
        return {
            "checked": checked,
            "updated": updated,
            "unchanged": unchanged,
            "missing": missing,
            "errors": errors,
        }

    def _linked_customer_differs(self, partner, shopify_customer):
        """True when a normalized core field differs between Odoo and Shopify.

        Normalization: name via _split_name vs first_name/last_name, email
        lowercased, phone digits-only. An absent Odoo phone never counts as
        a difference: the PUT payload omits phone, Shopify would keep its
        own value, and the pair would mismatch again on every run.
        """
        first, last = self._split_name(partner.name)
        if first != (shopify_customer.get("first_name") or "").strip():
            return True
        if last != (shopify_customer.get("last_name") or "").strip():
            return True
        odoo_email = (partner.email or "").strip().lower()
        if odoo_email != (shopify_customer.get("email") or "").strip().lower():
            return True
        odoo_phone = self._digits_only(partner.phone)
        if odoo_phone:
            return odoo_phone != self._digits_only(
                shopify_customer.get("phone")
            )
        return False

    def _shopify_customer_update_payload(self, partner):
        """Minimal PUT payload for a linked customer: core identity fields.

        Deliberately excludes addresses and tags: the pull owns those
        (Shopify is their source), and re-sending addresses on every PUT
        would append duplicate address records on the Shopify side.
        """
        first, last = self._split_name(partner.name)
        payload = {
            "first_name": first or (partner.email or "").split("@")[0],
            "last_name": last,
            "email": (partner.email or "").strip().lower(),
        }
        phone = partner.phone
        if phone:
            payload["phone"] = phone
        return payload

    @staticmethod
    def _split_name(name):
        parts = (name or "").strip().split(None, 1)
        first = parts[0] if parts else ""
        last = parts[1] if len(parts) > 1 else ""
        return first, last

    @staticmethod
    def _digits_only(phone):
        return re.sub(r"\D", "", phone or "")

    def _shopify_customer_payload(self, partner):
        first, last = self._split_name(partner.name)
        customer = {
            "first_name": first or (partner.email or "").split("@")[0],
            "last_name": last,
            "email": (partner.email or "").strip().lower(),
            "verified_email": True,
            "send_email_invite": False,
        }
        phone = partner.phone
        if phone:
            customer["phone"] = phone
        tags = [c.name for c in partner.category_id]
        if tags:
            customer["tags"] = ", ".join(tags)

        address = {}
        if partner.street:
            address["address1"] = partner.street
        if partner.street2:
            address["address2"] = partner.street2
        if partner.city:
            address["city"] = partner.city
        if partner.zip:
            address["zip"] = partner.zip
        if partner.country_id and partner.country_id.code:
            address["country_code"] = partner.country_id.code
        if partner.state_id and partner.state_id.code:
            address["province_code"] = partner.state_id.code
        company = partner.commercial_company_name
        if company:
            address["company"] = company
        if address:
            address["name"] = (partner.name or "").strip()
            if phone:
                address["phone"] = phone
            customer["addresses"] = [address]
        return customer

    # ------------------------------------------------------------------
    # webhook-driven real-time sync (Shopify -> Odoo)
    # ------------------------------------------------------------------
    def process_customer_webhook(self, job):
        """Handle a customers/create or customers/update webhook: sync that
        single customer into Odoo immediately (the hourly cron stays as the
        backstop). The webhook payload carries the full customer object, so
        no API fetch is needed. Ignored in pure push mode (odoo_to_shopify):
        that direction does not accept Shopify-side writes."""
        log = self.env["shopify.sync.log"]
        client = self.env["shopify.api.client"]

        enabled = client._param("customer_sync_enabled")
        if str(enabled).strip().lower() in _DISABLED:
            log.log_event(
                "info",
                "Customer webhook ignored: customer_sync_enabled is off.",
                source=LOG_SOURCE,
                job=job,
            )
            return
        direction = (client._param("customer_sync_direction") or "shopify_to_odoo").strip()
        if direction == "odoo_to_shopify":
            log.log_event(
                "info",
                "Customer webhook ignored: direction is Odoo -> Shopify (push-only).",
                source=LOG_SOURCE,
                job=job,
            )
            return

        payload = job.payload_dict()
        customer = payload.get("raw") or {}
        if not customer.get("id"):
            log.log_event(
                "warning",
                "Customer webhook carried no customer id; nothing to do.",
                source=LOG_SOURCE,
                job=job,
            )
            return

        status = self._process_customer(log, customer, can_create=True)
        log.log_event(
            "info",
            "Customer webhook %s processed: Shopify customer %s -> %s."
            % (payload.get("topic") or "?", customer.get("id"), status),
            source=LOG_SOURCE,
            job=job,
            shopify_order_ref=str(customer.get("email") or customer.get("id")),
        )
