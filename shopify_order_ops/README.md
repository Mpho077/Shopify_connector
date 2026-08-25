# Shopify Order Ops (Odoo 19)

Standalone **Odoo ↔ Shopify** operations module by **Leeno Consult**. Pull and
update orders, keep invoices aligned, sync addresses, fulfillments, inventory,
customers, products, prices, metafields, and discounts — with a modern ops
dashboard, job queue, and audit log.

## Features

- **Order pull** — Shopify orders → confirmed Odoo sale orders (optional
  invoice + payment on pull).
- **Order-edit invoice handling** — extra Shopify lines are added to the sale
  order and invoice (invoice reset/re-post if already posted). Removed or
  zeroed Shopify lines reduce the sale order quantity to the remaining
  `current_quantity`. Refunds become credit notes.
- **Shipping and billing address sync** — Shopify order shipping ↔ Odoo
  Delivery Address (direction configurable). Billing → Invoice Address
  (Shopify → Odoo).
- **Order discount & shipping charge sync** — dedicated sale lines for cart
  discounts and shipping amounts.
- **Inventory push** — Odoo free quantities → Shopify inventory levels.
- **Fulfillment push / pull** — deliveries and tracking between Odoo and Shopify.
- **Customer, product, price & metafield sync** — direction configurable where
  supported.
- **Discount catalogue** — pull automatic sales and codes into Shopify Ops.
- **Job queue + audit log + operations dashboard** — every event is queued,
  retried, and visible under *Shopify Ops*.

## Install

1. Copy `shopify_order_ops` into your Odoo addons path (Odoo.sh / self-hosted —
   Odoo Online does not allow custom modules).
2. Update the app list and install **Shopify Order Ops**.

## Configure (Settings → Shopify Ops)

- **Shop Domain** — e.g. `my-store.myshopify.com`
- **Admin API Access Token** — from a custom app in Shopify admin
- **Webhook Signing Secret** — the secret Shopify signs webhook HMACs with
- **Order match field** — how Shopify orders map to Odoo sale orders
- **Payment journal** / auto-paid options as needed
- **Shopify Location ID** — target location for inventory pushes

Then press **Register Shopify webhooks**.

## Support

**Leeno Consult** — free 24/7 WhatsApp / call: **+27 68 666 1814**  
Free Odoo version upgrades included. 30-day money-back guarantee.

## License

OPL-1 (Odoo Proprietary License v1) — see Odoo Apps listing for terms.
