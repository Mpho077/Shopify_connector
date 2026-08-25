/** @odoo-module **/
import { Component, useState, useExternalListener, onWillStart, onWillUnmount, markup } from "@odoo/owl";
import { registry } from "@web/core/registry";

const POLL_MS = 15000;
const DATA_URL = "/shopify_order_ops/dashboard/data";
const JOBS_MAX = 10000;

// Inline line icons (stroke = currentColor), 24x24 viewBox.
// Values are wrapped with markup() so OWL renders them as HTML via t-out.
function svg(inner) {
    return markup(
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ' +
        'stroke-linecap="round" stroke-linejoin="round">' + inner + "</svg>"
    );
}
const ICONS = {
    home: svg('<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/>'),
    list: svg('<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/>'),
    fileText: svg('<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>'),
    layers: svg('<polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/>'),
    bell: svg('<path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/>'),
    heart: svg('<path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/>'),
    link: svg('<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>'),
    settings: svg('<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>'),
    database: svg('<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/>'),
    refresh: svg('<polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>'),
    shield: svg('<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><polyline points="9 12 11 14 15 10"/>'),
    alertTri: svg('<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>'),
    clock: svg('<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>'),
    cart: svg('<circle cx="9" cy="21" r="1"/><circle cx="20" cy="21" r="1"/><path d="M1 1h4l2.68 13.39a2 2 0 0 0 2 1.61h9.72a2 2 0 0 0 2-1.61L23 6H6"/>'),
    edit: svg('<path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/>'),
    box: svg('<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/>'),
    truck: svg('<rect x="1" y="3" width="15" height="13" rx="1"/><polygon points="16 8 20 8 23 11 23 16 16 16 16 8"/><circle cx="5.5" cy="18.5" r="2.5"/><circle cx="18.5" cy="18.5" r="2.5"/>'),
    user: svg('<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>'),
    package: svg('<line x1="16.5" y1="9.4" x2="7.5" y2="4.21"/><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/>'),
    tag: svg('<path d="M20.59 13.41 13.42 20.59a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><line x1="7" y1="7" x2="7.01" y2="7"/>'),
    rotate: svg('<polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/>'),
    copy: svg('<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>'),
    check: svg('<polyline points="20 6 9 17 4 12"/>'),
    more: svg('<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>'),
    search: svg('<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>'),
    trash: svg('<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>'),
    external: svg('<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>'),
    updown: svg('<polyline points="17 1 21 5 17 9"/><line x1="21" y1="5" x2="9" y2="5"/><polyline points="7 23 3 19 7 15"/><line x1="3" y1="19" x2="15" y2="19"/>'),
    diamond: svg('<path d="M6 3h12l4 6-10 13L2 9Z"/><path d="M11 3 8 9l4 13 4-13-3-6"/><path d="M2 9h20"/>'),
};
const ENGINE_ICONS = {
    order_pull: "cart", order_edit: "edit", address: "link", inventory: "box",
    fulfillment: "truck", customer: "user", product: "package",
    price: "diamond", metafield: "database", discount_catalogue: "diamond",
};

export class ShopifyOpsDashboard extends Component {
    static template = "shopify_order_ops.Dashboard";
    // NOTE: no `static props` declaration — declaring `props = {}` makes OWL
    // reject the standard action-manager props (action, actionId, ...) in
    // debug mode. Omitting it accepts whatever the action manager passes.

    setup() {
        this.state = useState({
            data: null,
            loading: true,
            error: null,
            expandedJobId: null,
            filterType: "",
            filterState: "",
            filterSearch: "",
            retryingId: null,
            copiedId: null,
            menuJobId: null,
            jobLimit: 50,
            loadingMore: false,
        });
        this._timer = null;
        this._searchTimer = null;
        onWillStart(async () => {
            await this.load();
            this._timer = setInterval(() => this.load(true), POLL_MS);
        });
        onWillUnmount(() => {
            if (this._timer) clearInterval(this._timer);
            if (this._searchTimer) clearTimeout(this._searchTimer);
        });
        // Close any open row "⋯" menu on outside click.
        useExternalListener(window, "click", () => this.closeMenu());
    }

    async load(background) {
        try {
            const q = encodeURIComponent((this.state.filterSearch || "").trim());
            const res = await fetch(
                DATA_URL +
                    "?jobs_limit=" +
                    this.state.jobLimit +
                    (q ? "&jobs_q=" + q : ""),
                { credentials: "same-origin" }
            );
            if (!res.ok) throw new Error("HTTP " + res.status);
            const json = await res.json();
            if (json && json.error) throw new Error(json.error);
            this.state.data = json;
            this.state.error = null;
        } catch (e) {
            if (!background || !this.state.data) {
                this.state.error = String((e && e.message) || e);
            }
        } finally {
            this.state.loading = false;
        }
    }

    // ---- derived ---------------------------------------------------------
    get icons() { return ICONS; }

    get jobs() {
        const d = this.state.data;
        if (!d) return [];
        const ft = this.state.filterType;
        const fs = this.state.filterState;
        const q = this.state.filterSearch.trim().toLowerCase();
        const qBare = q.startsWith("#") ? q.slice(1) : q;
        return d.jobs.filter((j) => {
            if (ft && j.type !== ft) return false;
            if (fs && j.state !== fs) return false;
            if (!q) return true;
            const hay = [
                j.name,
                j.type,
                j.order_ref,
                j.error,
                j.payload_preview,
            ]
                .join(" ")
                .toLowerCase();
            return hay.includes(q) || (qBare && hay.includes(qBare));
        });
    }

    onFilterSearchInput(ev) {
        this.state.filterSearch = ev.target.value;
        if (this._searchTimer) clearTimeout(this._searchTimer);
        this._searchTimer = setTimeout(() => this.load(true), 350);
    }

    get jobTypes() {
        const d = this.state.data;
        if (!d) return [];
        return [...new Set(d.jobs.map((j) => j.type).filter(Boolean))];
    }

    get totalJobs() {
        const d = this.state.data;
        if (!d) return 0;
        return d.jobs_total || (d.jobs || []).length;
    }

    get canLoadMoreJobs() {
        const d = this.state.data;
        if (!d) return false;
        const loaded = (d.jobs || []).length;
        const total = d.jobs_total || 0;
        return loaded < total && this.state.jobLimit < JOBS_MAX;
    }

    engineIcon(key) { return ICONS[ENGINE_ICONS[key] || "database"]; }

    // ---- interactions ----------------------------------------------------
    toggleJob(id) {
        this.state.expandedJobId = this.state.expandedJobId === id ? null : id;
    }

    async retryJob(job, ev) {
        ev.stopPropagation();
        if (this.state.retryingId) return;
        this.state.retryingId = job.id;
        try {
            await fetch("/shopify_order_ops/job/" + job.id + "/retry", {
                method: "POST",
                credentials: "same-origin",
            });
        } catch (e) { /* surfaced on next refresh */ }
        this.state.retryingId = null;
        await this.load(true);
    }

    // ---- row "⋯" menu ---------------------------------------------------
    toggleMenu(id, ev) {
        ev.stopPropagation();
        this.state.menuJobId = this.state.menuJobId === id ? null : id;
    }

    closeMenu() {
        if (this.state.menuJobId !== null) this.state.menuJobId = null;
    }

    menuRetry(job, ev) {
        this.closeMenu();
        return this.retryJob(job, ev);
    }

    async clearJob(job, ev) {
        ev.stopPropagation();
        this.closeMenu();
        const label = job.name || "#" + job.id;
        if (!window.confirm("Clear job " + label + "? This removes it from the queue.")) return;
        try {
            const res = await fetch("/shopify_order_ops/job/" + job.id + "/clear", {
                method: "POST",
                credentials: "same-origin",
            });
            const json = await res.json().catch(() => null);
            if (!res.ok || !json || !json.ok) {
                window.alert("Could not clear " + label + ": " +
                    ((json && json.error) || ("HTTP " + res.status)));
            }
        } catch (e) {
            window.alert("Could not clear " + label + ": " + ((e && e.message) || e));
        }
        await this.load(true);
    }

    async copyPayload(job, ev) {
        ev.stopPropagation();
        const text = job.state === "failed" && job.error ? job.error : job.payload_preview || "";
        try {
            await navigator.clipboard.writeText(text);
            this.state.copiedId = job.id;
            setTimeout(() => { if (this.state.copiedId === job.id) this.state.copiedId = null; }, 1500);
        } catch (e) { /* clipboard unavailable */ }
    }

    refresh() { return this.load(true); }

    async loadMoreJobs(ev) {
        if (ev) ev.stopPropagation();
        if (this.state.loadingMore || !this.canLoadMoreJobs) return;
        this.state.jobLimit = Math.min(JOBS_MAX, this.state.jobLimit + 100);
        this.state.loadingMore = true;
        try {
            await this.load(true);
        } finally {
            this.state.loadingMore = false;
        }
    }

    async pushProduct(p, ev) {
        ev.stopPropagation();
        if (this.state.pushingProductId) return;
        this.state.pushingProductId = p.id;
        try {
            const res = await fetch("/shopify_order_ops/product/" + p.id + "/push", {
                method: "POST",
                credentials: "same-origin",
            });
            const json = await res.json().catch(() => null);
            if (!res.ok || !json || !json.ok) {
                window.alert("Push failed for " + p.name + ": " +
                    ((json && (json.error || json.message)) || ("HTTP " + res.status)));
            }
        } catch (e) {
            window.alert("Push failed for " + p.name + ": " + ((e && e.message) || e));
        }
        this.state.pushingProductId = null;
        await this.load(true);
    }

    productUrl(id) {
        return "/web#model=product.product&view_type=form&id=" + id;
    }

    kpiUrl(key) {
        const links = (this.state.data && this.state.data.links) || {};
        return links[key] || links.jobs || "/web";
    }

    jobUrl(id) {
        const links = (this.state.data && this.state.data.links) || {};
        return (links.job_form_base || "/web#model=shopify.sync.job&view_type=form&id=") + id;
    }
    logsUrl() {
        const links = (this.state.data && this.state.data.links) || {};
        return links.logs || "/web#model=shopify.sync.log&view_type=list";
    }
    jobsUrl() {
        const links = (this.state.data && this.state.data.links) || {};
        return links.jobs || "/web#model=shopify.sync.job&view_type=list";
    }
    mappingsUrl() {
        const links = (this.state.data && this.state.data.links) || {};
        return links.mappings || "/web#model=shopify.metafield.map&view_type=list";
    }
    discountsUrl() {
        const links = (this.state.data && this.state.data.links) || {};
        return links.discounts || "/web#model=shopify.discount&view_type=list";
    }
    engineUrl(eng) {
        if (eng && eng.key === "discount_catalogue") {
            return this.discountsUrl();
        }
        return null;
    }
    // Sidebar MONITORING / SETTINGS items: use data.links[key] when the
    // backend provides it, otherwise fall back to the closest existing view.
    sideUrl(key, fallback) {
        const links = (this.state.data && this.state.data.links) || {};
        return links[key] || fallback;
    }

    // ---- formatting ------------------------------------------------------
    fmtInt(n) { return (n === null || n === undefined) ? "—" : Number(n).toLocaleString("en-US"); }

    fmtRate(r) { return r === null || r === undefined ? "—" : Number(r).toFixed(2) + "%"; }

    // Delta value only ("↑ 18.4%"); the "vs yesterday" suffix is rendered
    // separately so it can stay gray while the value is colored (per mock).
    deltaTxt(v, unit) {
        if (v === null || v === undefined) return null;
        const sign = v > 0 ? "↑" : v < 0 ? "↓" : "→";
        return sign + " " + Math.abs(v) + (unit || "");
    }

    deltaDir(v, invert) {
        if (v === null || v === undefined || v === 0) return "flat";
        const good = invert ? v < 0 : v > 0;
        return good ? "good" : "bad";
    }

    // Avg-queue-time card: the mock shows an improvement (down) in blue
    // and a regression (up) in red.
    deltaDirTime(v) {
        if (v === null || v === undefined || v === 0) return "flat";
        return v < 0 ? "info" : "bad";
    }

    fmtAvg(seconds) {
        if (seconds === null || seconds === undefined) return "—";
        const s = Math.round(seconds);
        if (s < 60) return s + "s";
        const m = Math.floor(s / 60);
        return m + "m " + String(s % 60).padStart(2, "0") + "s";
    }

    rel(seconds) {
        if (seconds === null || seconds === undefined) return "—";
        if (seconds < 60) return seconds + "s";
        const m = Math.floor(seconds / 60);
        if (m < 60) return m + "m";
        const h = Math.floor(m / 60);
        if (h < 24) return h + "h";
        return Math.floor(h / 24) + "d";
    }

    pillClass(state) {
        return { done: "done", failed: "failed", pending: "pending", processing: "running" }[state] || "pending";
    }

    statusLabel(state) {
        return state === "processing" ? "RUNNING" : (state || "").toUpperCase();
    }
}

registry.category("actions").add("shopify_order_ops.dashboard", ShopifyOpsDashboard);
