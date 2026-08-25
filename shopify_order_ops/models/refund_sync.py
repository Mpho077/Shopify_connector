import logging

from odoo import fields, models

from .shopify_discount import DISCOUNT_PRODUCT_SKU, as_float, is_charge_line, is_charge_sku
from .shopify_refund import (
    allocate_refund_to_lines,
    discount_share_for_credit,
    is_full_product_refund,
    refund_line_sku,
    refund_prices_by_sku,
    refund_qty_by_sku,
    refund_transaction_amount,
)

_logger = logging.getLogger(__name__)

LOG_SOURCE = "order_refund"


class AccountMove(models.Model):
    _inherit = "account.move"

    shopify_refund_id = fields.Char(
        string="Shopify Refund ID", copy=False, index=True
    )


class ShopifyRefundSync(models.AbstractModel):
    """Syncs Shopify refunds into Odoo as credit notes (with restock)."""

    _name = "shopify.refund.sync"
    _description = "Shopify Refund Sync"

    # ------------------------------------------------------------------
    # Shopify -> Odoo
    # ------------------------------------------------------------------
    def process_order_refund(self, job):
        """Create an Odoo credit note for a Shopify refund.

        Job payload: {"order_id": <shopify id>, "topic": "refunds/create",
        "raw": {...}}. The raw payload is the Refund object (has order_id and
        refund_line_items); treat it as informational only.

        Behaviour:
        - Respects `refund_sync_enabled` (default OFF when unset); when off,
          logs info and returns.
        - Fetches the order via api.get_order(payload['order_id']); finds the
          newest refund on it (order['refunds'][-1]), or the refund matching
          raw['id'] when the webhook payload carries one.
        - Finds the Odoo sale.order and its latest posted out_invoice (pull-
          engine match rules). Not found -> raise RuntimeError (retry; the
          order may arrive after the refund webhook).
        - Idempotent on shopify_refund_id; also adopts a leftover unposted
          draft credit note from a crashed previous attempt (discards that
          draft when the refund is partial — leftover drafts are full copies).
        - Full product refund (every invoiced SKU refunded and no product
          qty left on the sale order) -> account.move.reversal wizard.
          Partial -> reverse-copy trimmed to the refunded SKUs only, plus
          that line's share of the order discount (so a 10% code does not
          refund the pre-discount price). Shipping stays off the credit
          unless it was refunded. Posts.
        - Restocks storable products flagged for restock via a validated
          return picking, only when a done delivery exists; restock problems
          never fail the job.
        - Raises on unexpected failure so the queue retries.
        """
        log = self.env["shopify.sync.log"]
        api = self.env["shopify.api.client"]

        payload = job.payload_dict()
        shopify_order_id = payload.get("order_id")
        raw = payload.get("raw") or {}
        ref = str(shopify_order_id) if shopify_order_id else None

        # Explicit-on gate: financial action must be opt-in.
        if not self._truthy(api._param("refund_sync_enabled")):
            log.log_event(
                "info",
                "Order refund job %s skipped: refund_sync_enabled is off "
                "(Settings -> Shopify Ops)." % job.name,
                source=LOG_SOURCE,
                job=job,
                shopify_order_ref=ref,
            )
            return

        if not shopify_order_id:
            raise RuntimeError(
                "Job %s: payload has no 'order_id' — cannot process refund."
                % job.name
            )

        # Always fetch the current order; never trust the webhook payload.
        order = api.get_order(shopify_order_id)
        if not order:
            raise RuntimeError(
                "Job %s: Shopify order %s not found via API."
                % (job.name, shopify_order_id)
            )
        order_name = order.get("name") or str(shopify_order_id)

        def _log(level, message):
            log.log_event(
                level, message, source=LOG_SOURCE, job=job,
                shopify_order_ref=order_name,
            )

        refund = self._select_refund(order, raw)
        if not refund:
            raise RuntimeError(
                "Job %s: no matching refund visible on Shopify order %s "
                "(id %s) yet — retrying." % (job.name, order_name, shopify_order_id)
            )
        refund_id = str(refund.get("id") or "").strip()
        if not refund_id:
            raise RuntimeError(
                "Job %s: refund on Shopify order %s has no id."
                % (job.name, order_name)
            )

        sale_order = self._find_sale_order(shopify_order_id, order.get("name"))
        if not sale_order:
            raise RuntimeError(
                "Job %s: no Odoo sale order for Shopify order %s (id %s) "
                "yet — retrying (the order may arrive after the refund "
                "webhook)." % (job.name, order_name, shopify_order_id)
            )

        # Reduce SO qty first so duplicate-SKU credits can target the
        # canceled line (invoiced qty still on a zeroed sale line).
        self.env["shopify.order.edit.engine"]._sync_quantities_from_shopify(
            sale_order, order, _log
        )

        invoice = self._target_invoice(sale_order)
        if not invoice:
            raise RuntimeError(
                "Job %s: sale order %s for Shopify order %s has no posted "
                "customer invoice yet — retrying."
                % (job.name, sale_order.name, order_name)
            )

        Move = self.env["account.move"]

        # Idempotency: a credit note already tagged with this refund id wins.
        existing = Move.search(
            [
                ("reversed_entry_id", "=", invoice.id),
                ("shopify_refund_id", "=", refund_id),
            ],
            limit=1,
        )
        if existing:
            _log(
                "info",
                "Order %s: credit note %s already exists for Shopify refund "
                "%s — idempotent no-op." % (order_name, existing.name, refund_id),
            )
            self.env["shopify.order.edit.engine"]._sync_quantities_from_shopify(
                sale_order, order, _log
            )
            return

        if self._invoice_already_credited(invoice):
            remaining = self._remaining_product_qty(sale_order)
            if remaining > 0.0001:
                _log(
                    "warning",
                    "Order %s: invoice %s already has credit notes covering "
                    "its total, but the sale order still has product qty. "
                    "The existing credit is likely a full reversal of a "
                    "one-line cancel — reset/delete that credit note and "
                    "retry this refund to credit only the canceled SKU."
                    % (order_name, invoice.name),
                )
            else:
                _log(
                    "warning",
                    "Order %s: invoice %s is already fully credited — not "
                    "creating another credit note for Shopify refund %s."
                    % (order_name, invoice.name, refund_id),
                )
            self.env["shopify.order.edit.engine"]._sync_quantities_from_shopify(
                sale_order, order, _log
            )
            return

        refund_qty = self._resolve_refund_qty(refund, sale_order, order, _log)
        remaining_ordered = self._remaining_product_qty(sale_order)
        invoiced_by_sku = self._invoice_product_qty_by_sku(invoice)
        full_refund = is_full_product_refund(
            invoiced_by_sku, refund_qty, remaining_ordered
        )

        # Crash recovery: a previous attempt may have created the draft
        # credit note but died before tagging/posting it. Adopt and finish.
        orphan = Move.search(
            [
                ("reversed_entry_id", "=", invoice.id),
                ("move_type", "=", "out_refund"),
                ("state", "=", "draft"),
                ("shopify_refund_id", "=", False),
                ("ref", "ilike", refund_id),
            ],
            order="id desc",
            limit=1,
        )
        if orphan:
            if not full_refund:
                _log(
                    "warning",
                    "Order %s: leftover draft credit note %s is a full "
                    "invoice copy, but Shopify refund %s is partial — "
                    "discarding the draft so a SKU-trimmed credit can "
                    "be created."
                    % (order_name, orphan.display_name, refund_id),
                )
                orphan.unlink()
            else:
                orphan.write({"shopify_refund_id": refund_id})
                orphan.action_post()
                _log(
                    "info",
                    "Order %s: adopted leftover draft credit note %s for Shopify "
                    "refund %s and posted it." % (order_name, orphan.name, refund_id),
                )
                self._restock(sale_order, refund, _log, order_name, order=order)
                self.env["shopify.order.edit.engine"]._sync_quantities_from_shopify(
                    sale_order, order, _log
                )
                return

        _log(
            "info",
            "Order %s: creating credit note for Shopify refund %s on invoice "
            "%s (%s, %d refunded SKU(s))."
            % (
                order_name,
                refund_id,
                invoice.name,
                "full reversal" if full_refund else "partial",
                len(refund_qty),
            ),
        )

        journal = self._credit_note_journal(invoice, _log, order_name)
        if not full_refund and not refund_qty:
            _log(
                "warning",
                "Order %s: Shopify refund %s has no product SKUs to credit "
                "and the sale order still has items — not reversing the "
                "whole invoice. Credit it manually if needed."
                % (order_name, refund_id),
            )
            self.env["shopify.order.edit.engine"]._sync_quantities_from_shopify(
                sale_order, order, _log
            )
            return
        if full_refund:
            credit_note = self._full_credit_note(
                invoice, journal, refund_id, order_name
            )
        else:
            credit_note = self._partial_credit_note(
                invoice, journal, refund_id, order_name, refund_qty, _log,
                refund=refund, order=order,
            )
            if not credit_note:
                self._restock(sale_order, refund, _log, order_name, order=order)
                self.env["shopify.order.edit.engine"]._sync_quantities_from_shopify(
                    sale_order, order, _log
                )
                return

        credit_note.write({"shopify_refund_id": refund_id})
        credit_note.action_post()
        self._register_refund_payment(credit_note, refund, _log, order_name)
        _log(
            "info",
            "Order %s: credit note %s posted for Shopify refund %s (journal "
            "%s)."
            % (order_name, credit_note.name, refund_id,
               credit_note.journal_id.display_name),
        )

        self._restock(sale_order, refund, _log, order_name, order=order)
        self.env["shopify.order.edit.engine"]._sync_quantities_from_shopify(
            sale_order, order, _log
        )

    def _register_refund_payment(self, credit_note, refund, _log, order_name):
        """Book the money-out side of the refund: register a payment on the
        credit note via the configured payment journal (bank/cash), mirroring
        the order pull's auto-paid behavior. Only runs when the Shopify refund
        has transactions (money actually moved); warns and leaves the credit
        note open otherwise."""
        if credit_note.amount_residual <= 0.01:
            return
        if not (refund.get("transactions") or []):
            _log(
                "warning",
                "Order %s: credit note %s posted but the refund shows no "
                "transactions — payment NOT registered; reconcile manually if "
                "money went out." % (order_name, credit_note.name),
            )
            return
        raw = (self.env["shopify.api.client"]._param("payment_journal_id") or "").strip()
        journal = self.env["account.journal"].browse()
        if raw:
            try:
                candidate = self.env["account.journal"].browse(int(raw))
                if candidate.exists() and candidate.type in ("bank", "cash"):
                    journal = candidate
            except (TypeError, ValueError):
                pass
        if not journal:
            _log(
                "warning",
                "Order %s: no bank/cash payment journal configured — refund "
                "payment for %s left unregistered."
                % (order_name, credit_note.name),
            )
            return
        wizard = (
            self.env["account.payment.register"]
            .with_context(active_model="account.move", active_ids=credit_note.ids)
            .create({"journal_id": journal.id})
        )
        wizard.action_create_payments()
        _log(
            "info",
            "Order %s: refund payment of %.2f registered on credit note %s "
            "via %s."
            % (order_name, credit_note.amount_total, credit_note.name,
               journal.display_name),
        )

    # ------------------------------------------------------------------
    # config / matching helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _truthy(value):
        """Explicit-on semantics: only 'True'/'true'/'1' count as enabled
        (unset means OFF)."""
        return str(value).strip().lower() in ("true", "1")

    def _find_sale_order(self, shopify_order_id, order_name):
        """sale.order by shopify_order_id, else client_order_ref/name with
        and without '#'. Empty recordset when none."""
        SaleOrder = self.env["sale.order"]
        sid = str(shopify_order_id)
        name = (order_name or "").strip()
        if not name:
            return SaleOrder.search([("shopify_order_id", "=", sid)], limit=1)
        clean = name.lstrip("#")
        candidates = [name, clean, "#" + clean]
        return SaleOrder.search(
            [
                "|",
                ("shopify_order_id", "=", sid),
                "|",
                ("client_order_ref", "in", candidates),
                ("name", "in", candidates),
            ],
            limit=1,
        )

    def _target_invoice(self, sale_order):
        """Latest posted out_invoice on the sale order; empty when none."""
        posted = sale_order.invoice_ids.filtered(
            lambda m: m.move_type == "out_invoice" and m.state == "posted"
        )
        return posted.sorted("id")[-1] if posted else self.env["account.move"].browse()

    def _select_refund(self, order, raw):
        """The refund to process: the one matching raw['id'] when given,
        else the newest refund on the order. None when not visible yet."""
        refunds = order.get("refunds") or []
        raw_id = raw.get("id")
        if raw_id is not None:
            for refund in refunds:
                if str(refund.get("id")) == str(raw_id):
                    return refund
            return None  # specific refund not visible yet -> caller retries
        return refunds[-1] if refunds else None

    def _credit_note_journal(self, invoice, _log, order_name):
        """Journal for the credit note. A credit note must live in a SALE
        journal (same type as the reversed entry) — the configured payment
        journal (bank/cash) is only used when it is type 'sale'; otherwise
        fall back to the invoice's own journal."""
        raw = (self.env["shopify.api.client"]._param("payment_journal_id") or "").strip()
        if raw:
            try:
                journal = self.env["account.journal"].browse(int(raw))
                if journal.exists() and journal.type == "sale":
                    return journal
                if journal.exists():
                    _log(
                        "warning",
                        "Order %s: configured journal %s is type '%s', not "
                        "'sale' — credit note goes to the invoice's journal %s."
                        % (order_name, journal.display_name, journal.type,
                           invoice.journal_id.display_name),
                    )
                    return invoice.journal_id
            except (TypeError, ValueError):
                pass
            _log(
                "warning",
                "Order %s: configured payment journal '%s' is not a valid "
                "journal id — falling back to the invoice's journal %s."
                % (order_name, raw, invoice.journal_id.display_name),
            )
        return invoice.journal_id

    # ------------------------------------------------------------------
    # refund interpretation
    # ------------------------------------------------------------------
    def _refund_qty_by_sku(self, refund):
        return refund_qty_by_sku(refund)

    def _resolve_refund_qty(self, refund, sale_order, order, _log):
        """SKU qty to credit: Shopify refund lines, else invoiced-but-unordered."""
        qty = refund_qty_by_sku(refund, order)
        if qty:
            return qty
        inferred = self._invoiced_beyond_ordered(sale_order)
        if inferred:
            _log(
                "info",
                "Shopify refund has no product SKUs — crediting invoiced qty "
                "that exceeds the sale order: %s."
                % ", ".join("%s x %s" % (sku, qty) for sku, qty in inferred.items()),
            )
        return inferred

    def _invoiced_beyond_ordered(self, sale_order):
        """{sku: qty} still invoiced after the sale line was reduced."""
        result = {}
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
            extra = line.qty_invoiced - line.product_uom_qty
            if extra > 0.0001:
                result[sku] = result.get(sku, 0.0) + extra
        return result

    def _remaining_product_qty(self, sale_order):
        total = 0.0
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
            total += line.product_uom_qty
        return total

    def _invoice_product_qty_by_sku(self, invoice):
        result = {}
        for line in invoice.invoice_line_ids:
            if line.display_type or not line.product_id:
                continue
            sku = (line.product_id.default_code or "").strip()
            if not sku or is_charge_sku(sku) or is_charge_line(line):
                continue
            if any(is_charge_line(sl) for sl in line.sale_line_ids):
                continue
            result[sku] = result.get(sku, 0.0) + line.quantity
        return result

    def _refund_transaction_amount(self, refund):
        return refund_transaction_amount(refund)

    def _invoice_already_credited(self, invoice):
        """True when posted credit notes already cover the invoice total."""
        credits = self.env["account.move"].search(
            [
                ("reversed_entry_id", "=", invoice.id),
                ("move_type", "=", "out_refund"),
                ("state", "=", "posted"),
            ]
        )
        if not credits:
            return False
        credited = sum(abs(c.amount_total) for c in credits)
        return credited + 0.02 >= abs(invoice.amount_total)

    def _is_full_refund(self, invoice, refund_qty, refund=None, sale_order=None):
        remaining = (
            self._remaining_product_qty(sale_order) if sale_order else 0.0
        )
        return is_full_product_refund(
            self._invoice_product_qty_by_sku(invoice),
            refund_qty,
            remaining,
        )

    # ------------------------------------------------------------------
    # credit note creation
    # ------------------------------------------------------------------
    def _full_credit_note(self, invoice, journal, refund_id, order_name):
        """Full reversal via the account.move.reversal wizard.

        Odoo 19 API: no refund_method field; refund_moves()/reverse_moves()
        fills wizard.new_move_ids with the draft credit note."""
        Reversal = self.env["account.move.reversal"].with_context(
            active_model="account.move",
            active_ids=invoice.ids,
        )
        wizard = Reversal.create(
            {
                "move_ids": [(6, 0, invoice.ids)],
                "journal_id": journal.id,
                "reason": "Shopify refund %s (%s)" % (refund_id, order_name),
            }
        )
        wizard.refund_moves()
        credit_note = wizard.new_move_ids.filtered(
            lambda m: m.move_type == "out_refund"
        )[:1]
        if not credit_note:
            raise RuntimeError(
                "Order %s: reversal wizard produced no credit note for "
                "invoice %s." % (order_name, invoice.name)
            )
        return credit_note

    def _product_invoice_lines(self, move):
        """Invoice tab product lines (Odoo 19 uses display_type='product')."""
        lines = move.invoice_line_ids.filtered(
            lambda l: l.product_id
            and l.display_type in (False, "product")
        )
        if lines:
            return lines
        return move.line_ids.filtered(
            lambda l: l.product_id and l.display_type in (False, "product")
        )

    def _refund_match_aliases(self, refund_qty):
        """Map Odoo default_code/barcode/product id back to Shopify refund SKUs."""
        aliases = {}
        product_ids = {}
        api = self.env["shopify.api.client"]
        for sku in refund_qty or {}:
            try:
                product = api.match_product_by_sku(sku)
            except RuntimeError:
                product = self.env["product.product"].browse()
            if not product:
                continue
            if product.default_code:
                aliases[product.default_code] = sku
            if product.barcode:
                aliases[product.barcode] = sku
            product_ids[product.id] = sku
        return aliases, product_ids

    def _is_discount_invoice_line(self, line):
        sku = (
            (line.product_id.default_code or "").strip() if line.product_id else ""
        )
        if sku == DISCOUNT_PRODUCT_SKU:
            return True
        return any(
            getattr(sl, "shopify_discount_line", False) for sl in line.sale_line_ids
        )

    def _sale_line_invoiced_extra(self, invoice_line):
        """Qty still invoiced after the linked sale line was reduced."""
        extra = 0.0
        sale_lines = invoice_line.sale_line_ids
        for sl in sale_lines:
            extra += max(0.0, (sl.qty_invoiced or 0.0) - (sl.product_uom_qty or 0.0))
        if extra <= 0.0001 and sale_lines and all(
            (sl.product_uom_qty or 0.0) <= 0.0001 for sl in sale_lines
        ):
            extra = abs(invoice_line.quantity or 0.0)
        return extra

    def _partial_credit_note(self, invoice, journal, refund_id, order_name,
                             refund_qty, _log, refund=None, order=None):
        """Partial refund: reverse-copy the invoice (correct debit/credit
        signs via _reverse_moves), then trim the draft to the refunded
        lines/quantities mapped by SKU.

        Returns an empty recordset when the refunded SKUs are not on this
        invoice (nothing to credit). Caller should not fail the job.
        """
        reversed_moves = invoice._reverse_moves(
            default_values_list=[
                {
                    "journal_id": journal.id,
                    "ref": "Shopify refund %s (%s)" % (refund_id, order_name),
                    "invoice_date": fields.Date.context_today(self),
                    "date": fields.Date.context_today(self),
                }
            ],
            cancel=False,
        )
        credit_note = (
            reversed_moves[0]
            if reversed_moves
            else self.env["account.move"].browse()
        )
        if not credit_note:
            raise RuntimeError(
                "Order %s: failed to build a reversal of invoice %s."
                % (order_name, invoice.name)
            )

        aliases, product_ids = self._refund_match_aliases(refund_qty)
        prices = refund_prices_by_sku(refund, order)
        tab_lines = credit_note.invoice_line_ids or credit_note.line_ids.filtered(
            lambda l: l.display_type in (False, "product", "line_section",
                                         "line_subsection", "line_note")
        )
        line_specs = []
        for line in tab_lines:
            product = line.product_id
            sku = ((product.default_code or "") if product else "").strip()
            barcode = ((product.barcode or "") if product else "").strip()
            line_specs.append(
                {
                    "id": line.id,
                    "sku": sku,
                    "barcode": barcode,
                    "product_id": product.id if product else False,
                    "quantity": line.quantity,
                    "price": abs(line.price_unit or 0.0),
                    "extra": self._sale_line_invoiced_extra(line),
                }
            )
        keep = allocate_refund_to_lines(
            line_specs,
            refund_qty,
            aliases=aliases,
            product_ids=product_ids,
            prices=prices,
        )
        discount_vals = {}
        if keep:
            credited_gross = 0.0
            invoice_product_gross = 0.0
            invoice_discount_abs = 0.0
            discount_line = None
            spec_by_id = {spec["id"]: spec for spec in line_specs}
            for spec in line_specs:
                if is_charge_sku(spec.get("sku")):
                    continue
                invoice_product_gross += abs(
                    as_float(spec.get("price")) * as_float(spec.get("quantity"))
                )
            for line_id, take in keep.items():
                spec = spec_by_id.get(line_id) or {}
                credited_gross += abs(as_float(spec.get("price")) * take)
            for line in tab_lines:
                if self._is_discount_invoice_line(line):
                    invoice_discount_abs += abs(line.price_unit * line.quantity)
                    if discount_line is None:
                        discount_line = line
            share = discount_share_for_credit(
                refund,
                order,
                credited_gross,
                invoice_product_gross,
                invoice_discount_abs,
            )
            if discount_line and share > 0.0001:
                keep[discount_line.id] = abs(discount_line.quantity) or 1.0
                qty = abs(discount_line.quantity) or 1.0
                signed = -abs(share) if (discount_line.price_unit or 0) < 0 else abs(share)
                discount_vals[discount_line.id] = {
                    "quantity": -qty if discount_line.quantity < 0 else qty,
                    "price_unit": signed / qty,
                }
                _log(
                    "info",
                    "Order %s: credit note for Shopify refund %s includes "
                    "%.2f of the order discount so the customer is not "
                    "refunded the pre-discount price."
                    % (order_name, refund_id, share),
                )
        line_commands = []
        for line in tab_lines:
            if line.id not in keep:
                if line.product_id and line.display_type in (False, "product"):
                    sku = (line.product_id.default_code or "").strip()
                    _log(
                        "info",
                        "Order %s: invoice line '%s' (SKU %s) is not part of "
                        "Shopify refund %s — excluded from the credit note."
                        % (order_name, line.name, sku or "none", refund_id),
                    )
                line_commands.append((2, line.id))
            elif line.id in discount_vals:
                line_commands.append((1, line.id, discount_vals[line.id]))
            else:
                new_qty = keep[line.id]
                if line.quantity < 0:
                    new_qty = -abs(new_qty)
                vals = {}
                if abs(abs(new_qty) - abs(line.quantity)) > 0.0001:
                    vals["quantity"] = new_qty
                if vals:
                    line_commands.append((1, line.id, vals))
        if line_commands:
            credit_note.write({"invoice_line_ids": line_commands})

        if not self._product_invoice_lines(credit_note):
            invoice_skus = [
                spec["sku"] or spec.get("barcode") or "none"
                for spec in line_specs
                if spec.get("sku") or spec.get("barcode")
            ]
            _log(
                "warning",
                "Order %s: Shopify refund %s SKUs %s are not on invoice %s "
                "(invoice SKUs: %s) — skipping credit note. Sale order qty "
                "still follows Shopify; credit manually if the invoice still "
                "includes that product."
                % (
                    order_name,
                    refund_id,
                    ", ".join(refund_qty) or "none",
                    invoice.name,
                    ", ".join(invoice_skus) or "none",
                ),
            )
            credit_note.with_context(force_delete=True).unlink()
            return self.env["account.move"].browse()

        # Belt and braces: _reverse_moves links the reversal already.
        credit_note.write({"reversed_entry_id": invoice.id})
        return credit_note

    # ------------------------------------------------------------------
    # restock
    # ------------------------------------------------------------------
    def _restock_qty_by_sku(self, refund, order=None):
        """{sku: qty} for refund lines flagged for restock (restock == True
        or a restock_type that returns goods to stock)."""
        result = {}
        for entry in refund.get("refund_line_items") or []:
            restock = bool(entry.get("restock")) or entry.get("restock_type") in (
                "return",
                "legacy_restock",
            )
            if not restock:
                continue
            sku = refund_line_sku(entry, order)
            if not sku:
                continue
            try:
                qty = float(entry.get("quantity") or 0)
            except (TypeError, ValueError):
                qty = 0.0
            if qty > 0:
                result[sku] = result.get(sku, 0.0) + qty
        return result

    def _restock(self, sale_order, refund, _log, order_name, order=None):
        """Return-to-stock for refunded storable lines. Fully defensive:
        any failure logs an error and the job still succeeds."""
        try:
            restock_qty = self._restock_qty_by_sku(refund, order)
            if not restock_qty:
                return

            deliveries = sale_order.picking_ids.filtered(
                lambda p: p.picking_type_code == "outgoing"
                and p.state == "done"
            ).sorted("id")
            if not deliveries:
                _log(
                    "warning",
                    "Order %s: refund requests restock for %s but no done "
                    "delivery exists on %s — manual restock needed."
                    % (order_name, restock_qty, sale_order.name),
                )
                return
            picking = deliveries[-1]

            ReturnWizard = self.env["stock.return.picking"].with_context(
                active_model="stock.picking",
                active_id=picking.id,
                active_ids=picking.ids,
            )
            wizard = ReturnWizard.create({"picking_id": picking.id})

            kept = 0
            line_commands = []
            for line in wizard.product_return_moves:
                product = line.product_id
                sku = ((product.default_code or "") if product else "").strip()
                qty = restock_qty.get(sku, 0.0)
                # Storable products only, and only refunded quantities.
                if qty <= 0 or not product or product.type != "product":
                    line_commands.append((2, line.id))
                    continue
                delivered = line.move_id.product_uom_qty if line.move_id else qty
                line_commands.append((1, line.id, {"quantity": min(qty, delivered)}))
                kept += 1
            if line_commands:
                wizard.write({"product_return_moves": line_commands})

            if not kept:
                _log(
                    "warning",
                    "Order %s: none of the restock SKUs %s are storable lines "
                    "on delivery %s — manual restock needed."
                    % (order_name, sorted(restock_qty), picking.name),
                )
                return

            action = wizard.action_create_returns()
            return_picking = self.env["stock.picking"].browse()
            if isinstance(action, dict) and action.get("res_id"):
                return_picking = self.env["stock.picking"].browse(action["res_id"])
            if not return_picking.exists():
                return_picking = self.env["stock.picking"].search(
                    [
                        ("sale_id", "=", sale_order.id),
                        ("picking_type_code", "=", "incoming"),
                        ("state", "not in", ("done", "cancel")),
                    ],
                    order="id desc",
                    limit=1,
                )
            if not return_picking:
                _log(
                    "warning",
                    "Order %s: return picking not found after the return "
                    "wizard — verify stock for %s manually."
                    % (order_name, restock_qty),
                )
                return

            for move in return_picking.move_ids:
                move.quantity = move.product_uom_qty
            return_picking.button_validate()
            _log(
                "info",
                "Order %s: restocked via return picking %s (%s)."
                % (
                    order_name,
                    return_picking.name,
                    ", ".join(
                        "%gx %s" % (q, s) for s, q in sorted(restock_qty.items())
                    ),
                ),
            )
        except Exception as exc:
            # Restock issues must never fail the job — the credit note is
            # already posted and correct.
            _log(
                "error",
                "Order %s: automatic restock failed (%s) — manual restock "
                "needed; the credit note itself is fine." % (order_name, exc),
            )
