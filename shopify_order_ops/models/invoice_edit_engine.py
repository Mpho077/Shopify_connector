from odoo import models

from .shopify_api import normalize_shopify_variant_id
from .shopify_discount import (
    as_float,
    aggregate_by_sku,
    is_charge_line,
    is_charge_sku,
)

PARAM_PREFIX = "shopify_order_ops."
RESIDUAL_TOLERANCE = 0.01


class ShopifyOrderEditEngine(models.AbstractModel):
    """Core business flow: a Shopify order got extra line items after the
    invoice in Odoo was already posted (and usually already marked paid).

    Flow: fetch current Shopify order -> find Odoo SO -> diff lines by SKU ->
    for added lines / qty increases, unreconcile + draft the posted invoice,
    add the lines to SO and invoice, re-post, re-apply payments.
    """

    _name = "shopify.order.edit.engine"
    _description = "Shopify Order Edit Engine"

    # ------------------------------------------------------------------
    # public entry point
    # ------------------------------------------------------------------
    def process_order_edit(self, job):
        """Main entry point called by the sync.job dispatcher.

        Args:
            job: shopify.sync.job record of type `order_edit`. Its payload is
                 {"order_id": <shopify numeric id>, "topic": ..., "raw": {...}}.

        Raises on failure; the caller wraps everything in a savepoint and the
        job queue handles retries.
        """
        log = self.env["shopify.sync.log"]
        api = self.env["shopify.api.client"]

        payload = job.payload_dict()
        shopify_order_id = payload.get("order_id")

        # Off switch (Settings -> Shopify Ops). Default-on: unset means ON,
        # only an explicit falsy string disables. When off, return WITHOUT
        # raising so the job marks done instead of retrying.
        raw_enabled = self._param("order_edit_enabled")
        if raw_enabled is not None and str(raw_enabled).strip().lower() in ("false", "0"):
            log.log_event(
                "info",
                "Order edit job %s skipped: order_edit_enabled is off "
                "(Settings -> Shopify Ops)." % job.name,
                source="order_edit",
                job=job,
                shopify_order_ref=str(shopify_order_id) if shopify_order_id else None,
            )
            return

        if not shopify_order_id:
            raise RuntimeError(
                f"Job {job.name}: payload has no 'order_id' — cannot process order edit."
            )

        # 1. Always fetch the CURRENT order; never trust the webhook line list.
        order = api.get_order(shopify_order_id)
        if not order:
            raise RuntimeError(
                f"Job {job.name}: Shopify order {shopify_order_id} not found via API."
            )
        order_name = order.get("name") or str(shopify_order_id)
        financial_status = (order.get("financial_status") or "").lower()

        def _log(level, message):
            log.log_event(
                level, message, source="order_edit", job=job,
                shopify_order_ref=order_name,
            )

        _log("info", f"Processing order edit for Shopify order {order_name} "
                     f"(id {shopify_order_id}, financial_status={financial_status}).")

        # Manual "Sync lines from Shopify" on an existing SO must still run
        # even if the order is older than Only pull / update after.
        if payload.get("topic") != "manual" and self.env[
            "shopify.order.pull.engine"
        ].skip_order_before_cutoff(order, _log):
            return

        # Refunds are applied by order_refund jobs (credit notes). This
        # engine keeps sale-order quantities in line with Shopify.
        if order.get("refunds"):
            _log(
                "info",
                f"Order {order_name} has refund objects in Shopify — credit "
                f"notes are handled by refund sync; quantities follow "
                f"current_quantity here.",
            )

        # 2. Find the Odoo sale order.
        sale_order = self._find_sale_order(order, shopify_order_id, job)
        self._backfill_order_refs(sale_order, order, shopify_order_id)
        self.env["shopify.order.pull.engine"]._apply_shopify_order_tags(
            sale_order, order, _log
        )

        # Currency mismatch: warn, proceed.
        order_currency = order.get("currency")
        if order_currency and sale_order.currency_id.name \
                and order_currency != sale_order.currency_id.name:
            _log("warning", f"Currency mismatch on {order_name}: Shopify="
                            f"{order_currency}, Odoo SO={sale_order.currency_id.name}. "
                            f"Proceeding with Shopify prices as-is.")

        # 3. Build line maps by SKU (Odoo: every product line, not last-wins).
        shopify_lines = self._shopify_line_map(order, _log)
        odoo_by_sku = self._odoo_product_lines_by_sku(sale_order)
        line_items_by_sku = self._line_items_by_sku(order)

        # 4. Diff: additions/increases vs quantity decreases.
        additions = []   # list of dicts: {'product', 'sku', 'qty', 'price', 'name', 'so_line'|None}
        for sku, sline in shopify_lines.items():
            so_lines = odoo_by_sku.get(sku, self.env["sale.order.line"])
            so_line = so_lines[:1]
            shopify_qty = sline["qty"]
            odoo_qty = sum(so_lines.mapped("product_uom_qty"))
            if so_lines:
                # Already on this SO: keep that product. Catalog duplicates
                # (e.g. PEM900A on two variants) must not block adding other lines.
                if shopify_qty > odoo_qty + 0.0001:
                    additions.append({
                        "product": so_line.product_id, "sku": sku,
                        "qty": shopify_qty - odoo_qty,
                        "price": sline["price"],
                        "name": sline.get("name") or sku,
                        "discount": sline.get("discount"),
                        "so_line": so_line,
                    })
                continue
            if shopify_qty > 0:
                line_item = line_items_by_sku.get(sku)
                product = self._match_product(
                    sku,
                    line_item=line_item,
                    name=sline.get("line_name") or sline.get("name"),
                    title=sline.get("title") or sline.get("name"),
                    _log=_log,
                )
                additions.append({
                    "product": product, "sku": sku, "qty": shopify_qty,
                    "price": sline["price"],
                    "name": sline.get("name") or sku,
                    "discount": sline.get("discount"),
                    "so_line": None,
                })

        decreased = self._sync_quantities_from_shopify(sale_order, order, _log)

        if not additions:
            self.env["shopify.order.pull.engine"]._sync_line_discounts(
                sale_order, order, _log
            )
            if decreased:
                _log("info", f"Order {order_name}: sale order quantities reduced "
                             f"to match Shopify; no line additions.")
            else:
                _log("info", f"Order {order_name}: no line additions or quantity "
                             f"increases; order discount synced.")
            return

        _log("info", f"Order {order_name}: applying {len(additions)} line "
                     f"addition(s)/increase(s): "
                     + ", ".join(f"{a['sku']} +{a['qty']}" for a in additions))

        # 5. Apply the changes.
        move = self._target_invoice(sale_order)
        captured_credit_ids = []
        invoice_was_posted = False
        if move and move.state == "posted":
            invoice_was_posted = True
            captured_credit_ids = self._capture_payments(move, _log, order_name)
            self._reset_invoice(move, _log, order_name)
        elif move:
            _log("info", f"Order {order_name}: target invoice {move.name} is draft — "
                         f"adding lines without payment handling.")
        else:
            _log("warning", f"Order {order_name}: no invoice found for SO "
                            f"{sale_order.name}; updating the sale order only.")

        new_so_lines = self._add_lines_to_sale_order(sale_order, additions, _log, order_name)
        if move:
            self._add_lines_to_invoice(move, additions, new_so_lines, _log, order_name)
            move.action_post()
            _log("info", f"Order {order_name}: invoice {move.name} re-posted.")
            self._backfill_move_ref(move, shopify_order_id)
            if invoice_was_posted:
                self._repay_invoice(
                    move, captured_credit_ids, financial_status,
                    api, _log, order_name,
                )

        self.env["shopify.order.pull.engine"]._sync_line_discounts(
            sale_order, order, _log
        )

        _log("info", f"Order {order_name}: order edit processed successfully.")

    # ------------------------------------------------------------------
    # private helpers
    # ------------------------------------------------------------------
    def _param(self, key, default=None):
        return (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param(PARAM_PREFIX + key, default)
        )

    def _find_sale_order(self, order, shopify_order_id, job):
        """Locate the existing Odoo sale.order. Never creates a new one."""
        shopify_name = order.get("name") or ""
        order_rec = self.env["shopify.order.pull.engine"]._find_existing_order(
            shopify_order_id, shopify_name
        )
        if not order_rec:
            match_field = (
                self._param("order_match_field", "client_order_ref")
                or "client_order_ref"
            )
            raise RuntimeError(
                f"Job {job.name}: no Odoo sale order found for Shopify order "
                f"{shopify_name or shopify_order_id} (match field: {match_field}). "
                f"The job will retry, then fail visibly."
            )
        return order_rec

    def _backfill_order_refs(self, sale_order, order, shopify_order_id):
        vals = {}
        if not sale_order.shopify_order_id:
            vals["shopify_order_id"] = str(shopify_order_id)
        if not sale_order.shopify_order_name and order.get("name"):
            vals["shopify_order_name"] = order["name"]
        if vals:
            sale_order.write(vals)

    def _backfill_move_ref(self, move, shopify_order_id):
        if not move.shopify_order_id:
            move.shopify_order_id = str(shopify_order_id)

    def _shopify_line_map(self, order, _log):
        """{sku: {'qty', 'price', 'discount', 'name'}} from line_items."""
        for item in order.get("line_items") or []:
            sku = (item.get("sku") or "").strip()
            if not sku:
                _log("warning", f"Order {order.get('name')}: Shopify line "
                                f"'{item.get('title')}' (id {item.get('id')}) has no SKU — "
                                f"skipped.")
        return aggregate_by_sku(order.get("line_items"))

    def _line_items_by_sku(self, order):
        """First Shopify line_item per SKU (for title/variant matching)."""
        by_sku = {}
        for item in order.get("line_items") or []:
            sku = (item.get("sku") or "").strip()
            if sku and sku not in by_sku:
                by_sku[sku] = item
        return by_sku

    def _odoo_product_lines_by_sku(self, sale_order):
        """SKU → all non-charge product sale lines (duplicate SKUs kept)."""
        by_sku = {}
        empty = self.env["sale.order.line"]
        for line in sale_order.order_line:
            if (
                line.display_type
                or is_charge_line(line)
                or not line.product_id
            ):
                continue
            sku = (line.product_id.default_code or "").strip()
            if not sku or is_charge_sku(sku):
                continue
            by_sku[sku] = by_sku.get(sku, empty) | line
        return by_sku

    def _sync_quantities_from_shopify(self, sale_order, order, _log):
        """Set Odoo product-line qty to Shopify remaining qty (current_quantity).

        Does not touch invoices — refunds create credit notes. Duplicate Odoo
        lines for one SKU are reduced newest-first until the total matches.
        Returns True when any sale line quantity changed.
        """
        if not sale_order or sale_order.state == "cancel":
            return False
        shopify_lines = self._shopify_line_map(order, _log)
        odoo_by_sku = self._odoo_product_lines_by_sku(sale_order)
        order_name = order.get("name") or sale_order.name
        changed = False
        unlocked = False
        for sku, lines in odoo_by_sku.items():
            target = as_float((shopify_lines.get(sku) or {}).get("qty"))
            current = sum(lines.mapped("product_uom_qty"))
            if target + 0.0001 >= current:
                continue
            if sale_order.state == "done" and not unlocked:
                sale_order.action_unlock()
                unlocked = True
                _log(
                    "info",
                    "Order %s: unlocked locked sale order %s to reduce quantities."
                    % (order_name, sale_order.name),
                )
            extra = current - target
            for line in lines.sorted("id", reverse=True):
                if extra <= 0.0001:
                    break
                take = min(line.product_uom_qty, extra)
                new_qty = line.product_uom_qty - take
                line.with_context(shopify_sync_origin="shopify").write(
                    {"product_uom_qty": new_qty}
                )
                extra -= take
                changed = True
                _log(
                    "info",
                    "Order %s: SO line %s quantity decreased to %s to match "
                    "Shopify." % (order_name, sku, new_qty),
                )
        return changed

    def _match_product(self, sku, line_item=None, name=None, title=None, _log=None):
        """Match by Shopify variant id, import from Shopify if missing."""
        api = self.env["shopify.api.client"]
        if line_item and not name and not title:
            name = api.shopify_line_item_name(line_item)
            title = (line_item.get("title") or "").strip() or None
        product = self.env["shopify.product.sync"].match_or_import_for_order_line(
            sku,
            line_item=line_item,
            name=name,
            title=title,
            _log=_log,
        )
        if not product:
            variant_id = (
                line_item.get("variant_id")
                if isinstance(line_item, dict)
                else None
            )
            raise RuntimeError(
                f"No Odoo product found for Shopify SKU '{sku}' (variant id "
                f"{normalize_shopify_variant_id(variant_id) or variant_id or ''}; "
                f"line {(name or title or '').strip()!r}). Searched "
                f"shopify_variant_id, SKU, and name; automatic import runs "
                f"when product sync is on and create mode is not update only."
            )
        return product

    def _target_invoice(self, sale_order):
        """Latest posted out_invoice; else latest draft out_invoice; else empty."""
        invoices = sale_order.invoice_ids.filtered(
            lambda m: m.move_type == "out_invoice"
        )
        posted = invoices.filtered(lambda m: m.state == "posted")
        if posted:
            return posted.sorted("id")[-1]
        drafts = invoices.filtered(lambda m: m.state == "draft")
        if drafts:
            return drafts.sorted("id")[-1]
        return self.env["account.move"].browse()

    def _capture_payments(self, move, _log, order_name):
        """If the invoice is paid/in_payment or reconciled, capture the credit
        payment lines reconciled against its receivable lines and unreconcile.

        Returns a list of account.move.line ids (the credit side) to re-apply.
        """
        receivable_lines = move.line_ids.filtered(
            lambda l: l.account_id.account_type == "asset_receivable"
        )
        reconciled = receivable_lines.filtered(
            lambda l: l.matched_credit_ids or l.matched_debit_ids
        )
        paid_like = move.payment_state in ("paid", "in_payment") or reconciled
        if not paid_like:
            return []

        credit_ids = []
        for line in receivable_lines.filtered(lambda l: l.debit > 0):
            for match in line.matched_credit_ids:
                credit_move_line = match.credit_move_id
                if credit_move_line.id not in credit_ids:
                    credit_ids.append(credit_move_line.id)
        _log("info", f"Order {order_name}: captured {len(credit_ids)} reconciled "
                     f"payment line(s) on invoice {move.name}; unreconciling.")
        receivable_lines.remove_move_reconcile()
        return credit_ids

    def _reset_invoice(self, move, _log, order_name):
        """Reset a posted invoice to draft. Locked periods raise — let them."""
        move.button_draft()
        _log("info", f"Order {order_name}: invoice {move.name} reset to draft.")

    def _add_lines_to_sale_order(self, sale_order, additions, _log, order_name):
        """Apply additions to the SO. Returns {sku: sale.order.line}."""
        if sale_order.state == "done":
            sale_order.action_unlock()
            _log("info", f"Order {order_name}: unlocked locked sale order "
                         f"{sale_order.name}.")

        result = {}
        for add in additions:
            if add["so_line"] is not None:
                line = add["so_line"]
                # shopify_sync_origin marks this change as Shopify-originated
                # so the Odoo->Shopify update push ignores it (no echo loop).
                qty_vals = {"product_uom_qty": line.product_uom_qty + add["qty"]}
                if add.get("discount") is not None:
                    qty_vals["discount"] = as_float(add.get("discount"))
                line.with_context(shopify_sync_origin="shopify").write(qty_vals)
                _log("info", f"Order {order_name}: SO line {add['sku']} quantity "
                             f"increased to {line.product_uom_qty}.")
                result[add["sku"]] = line
            else:
                new_vals = {
                    "product_id": add["product"].id,
                    "product_uom_qty": add["qty"],
                    "price_unit": add["price"],
                    "name": add["name"],
                }
                if add.get("discount") is not None:
                    new_vals["discount"] = as_float(add.get("discount"))
                sale_order.with_context(shopify_sync_origin="shopify").write({
                    "order_line": [(0, 0, new_vals)],
                })
                line = sale_order.order_line.filtered(
                    lambda l: l.product_id == add["product"]
                ).sorted("id")[-1]
                _log("info", f"Order {order_name}: added SO line {add['sku']} "
                             f"qty {add['qty']} @ {add['price']}.")
                result[add["sku"]] = line
        return result

    def _add_lines_to_invoice(self, move, additions, new_so_lines, _log, order_name):
        """Mirror additions onto the invoice, linked to their sale lines."""
        for add in additions:
            so_line = new_so_lines[add["sku"]]
            if add["so_line"] is not None:
                # Qty increase: bump the existing linked invoice line.
                inv_line = move.invoice_line_ids.filtered(
                    lambda l: add["so_line"] in l.sale_line_ids
                ).sorted("id")[-1:]
                if inv_line:
                    inv_line.quantity = inv_line.quantity + add["qty"]
                    if add.get("discount") is not None:
                        inv_line.discount = as_float(add.get("discount"))
                    _log("info", f"Order {order_name}: invoice line {add['sku']} "
                                 f"quantity increased to {inv_line.quantity}.")
                    continue
                # No linked invoice line yet (e.g. never invoiced) -> add one.
            inv_vals = {
                "product_id": add["product"].id,
                "quantity": add["qty"],
                "price_unit": add["price"],
                "sale_line_ids": [(4, so_line.id)],
                "tax_ids": [(6, 0, so_line.tax_ids.ids)],
            }
            if add.get("discount") is not None:
                inv_vals["discount"] = as_float(add.get("discount"))
            move.write({
                "invoice_line_ids": [(0, 0, inv_vals)],
            })
            _log("info", f"Order {order_name}: added invoice line {add['sku']} "
                         f"qty {add['qty']} @ {add['price']} to {move.name}.")

    def _repay_invoice(self, move, captured_credit_ids, financial_status,
                       api, _log, order_name):
        """Re-apply captured payments; optionally top up the residual."""
        for line_id in captured_credit_ids:
            line = self.env["account.move.line"].browse(line_id)
            if not line.exists():
                continue
            # Skip silently if the line got reconciled meanwhile.
            if line.matched_debit_ids or line.reconciled:
                continue
            try:
                move.js_assign_outstanding_line(line_id)
            except Exception as exc:  # noqa: BLE001 - already reconciled races
                _log("warning", f"Order {order_name}: could not re-apply payment "
                                f"line {line_id} on {move.name}: {exc}")

        if financial_status != "paid":
            _log("info", f"Order {order_name}: Shopify financial_status is "
                         f"'{financial_status}' — original payments re-applied; "
                         f"a balance of {move.amount_residual} remains open on "
                         f"{move.name} until the customer pays the extra.")
            return

        auto_mark_paid = (api._param("auto_mark_paid") or "").strip().lower()
        if auto_mark_paid not in ("true", "1"):
            _log("info", f"Order {order_name}: auto_mark_paid disabled; invoice "
                         f"{move.name} left with residual {move.amount_residual}.")
            return

        if move.amount_residual > RESIDUAL_TOLERANCE:
            journal_id_raw = (api._param("payment_journal_id") or "").strip()
            if not journal_id_raw:
                _log("warning", f"Order {order_name}: residual {move.amount_residual} "
                                f"on {move.name} but no payment journal configured "
                                f"(shopify_order_ops.payment_journal_id) — leaving "
                                f"the balance open.")
                return
            residual = move.amount_residual
            wizard = (
                self.env["account.payment.register"]
                .with_context(active_model="account.move", active_ids=[move.id])
                .create({
                    "journal_id": int(journal_id_raw),
                    "amount": residual,
                })
            )
            wizard.action_create_payments()
            _log("info", f"Order {order_name}: registered extra payment of "
                         f"{residual} on {move.name} via journal "
                         f"{journal_id_raw}.")
        else:
            _log("info", f"Order {order_name}: invoice {move.name} fully covered "
                         f"by re-applied payments; no extra payment needed.")
