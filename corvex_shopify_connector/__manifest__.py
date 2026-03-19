# -*- coding: utf-8 -*-
{
    'name': 'Corvex Shopify Connector',
    'version': '19.0.1.0.0',
    'category': 'Sales',
    'summary': 'Enhanced Shopify integration — Dashboard, Price Sync, Auto-Invoice',
    'description': """
Corvex Shopify Connector
========================

Extends the Shopify Odoo Connector (shopify_ept) with powerful enhancements:

- **Dashboard**: Real-time sales stats, KPIs, queue monitoring, quick actions
- **Price Sync**: Standalone Shopify-to-Odoo price synchronization by SKU
- **Auto-Invoice**: Automatically create and pay invoices for Shopify orders
- **Pickup Orders**: Track pickup / click-and-collect orders
- **Error Notifications**: Configurable error alerting per instance
    """,
    'author': 'Corvex Consult',
    'website': 'https://www.corvexconsult.com/',
    'license': 'Other proprietary',
    'depends': ['shopify_ept'],
    'data': [
        'security/shopify_connector_security.xml',
        'security/ir.model.access.csv',
        'views/shopify_error_notification_views.xml',
        'views/shopify_price_sync_views.xml',
        'views/shopify_order_views.xml',
        'views/res_config_settings_views.xml',
        'views/shopify_connector_views.xml',
        'data/shopify_cron.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'corvex_shopify_connector/static/src/css/shopify_connector.css',
            'corvex_shopify_connector/static/src/js/shopify_connector.js',
            'corvex_shopify_connector/static/src/xml/shopify_connector.xml',
        ],
    },
    'images': ['static/description/banner.png'],
    'installable': True,
    'application': True,
    'auto_install': False,
}
