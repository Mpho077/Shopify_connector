/** @odoo-module **/

import { Component, useState, onWillStart } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

class ShopifyDashboard extends Component {
    static template = "corvex_shopify_connector.ShopifyDashboard";

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");

        this.state = useState({
            instances: [],
            selectedInstanceId: null,
            period: "current_week",
            data: {
                instance_name: "",
                total_sales: 0,
                avg_order_value: 0,
                percentage_change: 0,
                order_count_period: 0,
                product_count: 0,
                customer_count: 0,
                order_count: 0,
                shipped_order_count: 0,
                refund_count: 0,
                unacknowledged_error_count: 0,
                queue_draft: 0,
                queue_progress: 0,
                queue_failed: 0,
            },
            loading: true,
        });

        onWillStart(async () => {
            await this.loadInstances();
        });
    }

    async loadInstances() {
        const instances = await this.orm.searchRead(
            "shopify.instance.ept",
            [["active", "=", true]],
            ["id", "name"],
        );
        this.state.instances = instances;
        if (instances.length > 0) {
            this.state.selectedInstanceId = instances[0].id;
            await this.loadDashboardData();
        }
        this.state.loading = false;
    }

    async loadDashboardData() {
        if (!this.state.selectedInstanceId) return;
        this.state.loading = true;
        const data = await this.orm.call(
            "shopify.instance.ept",
            "get_dashboard_data",
            [this.state.selectedInstanceId, this.state.period],
        );
        if (data) {
            this.state.data = data;
        }
        this.state.loading = false;
    }

    async onInstanceChange(ev) {
        this.state.selectedInstanceId = parseInt(ev.target.value);
        await this.loadDashboardData();
    }

    async onPeriodChange(ev) {
        this.state.period = ev.target.value;
        await this.loadDashboardData();
    }

    get periodLabel() {
        const labels = {
            current_week: "This Week",
            current_month: "This Month",
            current_year: "This Year",
        };
        return labels[this.state.period] || "This Week";
    }

    formatCurrency(value) {
        return new Intl.NumberFormat("en-AU", {
            style: "currency",
            currency: "AUD",
            minimumFractionDigits: 2,
        }).format(value);
    }

    formatNumber(value) {
        return new Intl.NumberFormat().format(value || 0);
    }

    openPerformOperation() {
        this.action.doAction("shopify_ept.action_shopify_instance_ept");
    }

    openReport() {
        this.action.doAction("shopify_ept.action_shopify_sales_order");
    }

    openLogs() {
        this.action.doAction("shopify_ept.action_shopify_common_log_line_ept");
    }

    openQueues() {
        this.action.doAction("shopify_ept.action_shopify_order_data_queue_ept");
    }

    openProducts() {
        this.action.doAction("shopify_ept.action_shopify_product_ept");
    }

    openCustomers() {
        this.action.doAction("shopify_ept.action_shopify_partner_form");
    }

    openOrders() {
        this.action.doAction("shopify_ept.action_shopify_sales_order");
    }

    openShippedOrders() {
        this.action.doAction({
            name: "Shipped Orders",
            type: "ir.actions.act_window",
            res_model: "sale.order",
            views: [[false, "list"], [false, "form"]],
            domain: [
                ["shopify_instance_id", "!=", false],
                ["updated_in_shopify", "=", true],
            ],
        });
    }

    openRefunds() {
        this.action.doAction({
            name: "Refunds",
            type: "ir.actions.act_window",
            res_model: "account.move",
            views: [[false, "list"], [false, "form"]],
            domain: [
                ["shopify_instance_id", "!=", false],
                ["move_type", "=", "out_refund"],
            ],
        });
    }

    openUnacknowledgedErrors() {
        this.action.doAction("shopify_ept.action_shopify_common_log_line_ept");
    }

    openInstances() {
        this.action.doAction("shopify_ept.action_shopify_instance_ept");
    }
}

registry.category("actions").add("corvex_shopify_connector_dashboard", ShopifyDashboard);
