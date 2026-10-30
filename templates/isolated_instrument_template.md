{# templates/isolated_instrument.html #}
{% extends "base.html" %}

{% block title %}Isolated Instrument · {{ app_name }}{% endblock %}

{% block head %}
<link
    href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css"
    rel="stylesheet"
/>
<link
    rel="stylesheet"
    href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css"
/>

<style>
    :root {
        --iso-bg: #f8fafc;
        --iso-card-bg: #ffffff;
        --iso-border: #e2e8f0;
        --iso-text: #0f172a;
        --iso-muted: #64748b;
        --iso-primary: #2563eb;
        --iso-green: #16a34a;
        --iso-green-bg: #dcfce7;
        --iso-red: #dc2626;
        --iso-red-bg: #fee2e2;
        --iso-amber: #b45309;
        --iso-amber-bg: #fef3c7;
        --iso-amber-border: #fde68a;
        --iso-blue-bg: #dbeafe;
        --iso-blue-border: #bfdbfe;
        --iso-purple: #7c3aed;
        --iso-purple-bg: #ede9fe;
        --iso-purple-border: #ddd6fe;
        --iso-simulate: #ea580c;
        --iso-simulate-bg: #ffedd5;
        --iso-simulate-border: #fed7aa;
        --iso-shadow: 0 1px 3px rgba(15, 23, 42, 0.06);
        --iso-shadow-md: 0 8px 24px rgba(15, 23, 42, 0.08);
    }

    body {
        background-color: var(--iso-bg) !important;
        color: var(--iso-text) !important;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
            "Helvetica Neue", Arial, sans-serif;
    }

    .iso-page {
        max-width: 1400px;
        margin: 0 auto;
        padding: 24px;
    }

    /* ---- Top bar ---- */
    .iso-topbar {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 16px;
        flex-wrap: wrap;
        margin-bottom: 20px;
    }

    .iso-topbar-left {
        display: flex;
        align-items: center;
        gap: 16px;
        flex-wrap: wrap;
    }

    .iso-topbar-title {
        display: flex;
        align-items: center;
        gap: 10px;
        font-size: 1.4rem;
        font-weight: 800;
        margin: 0;
    }

    .iso-topbar-title i {
        color: var(--iso-primary);
    }

    .iso-status-pill {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        padding: 6px 14px;
        border-radius: 999px;
        font-size: 0.8rem;
        font-weight: 700;
        border: 1px solid transparent;
        text-transform: uppercase;
        letter-spacing: 0.3px;
    }

    .iso-status-pill::before {
        content: "";
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background-color: currentColor;
    }

    .iso-status-pill.isolated {
        background: var(--iso-green-bg);
        color: var(--iso-green);
        border-color: #bbf7d0;
    }

    .iso-status-pill.simulate {
        background: var(--iso-simulate-bg);
        color: var(--iso-simulate);
        border-color: var(--iso-simulate-border);
    }

    .iso-status-pill.watching {
        background: var(--iso-blue-bg);
        color: var(--iso-primary);
        border-color: var(--iso-blue-border);
    }

    .iso-status-pill.waiting {
        background: var(--iso-amber-bg);
        color: var(--iso-amber);
        border-color: var(--iso-amber-border);
    }

    .iso-status-pill.off {
        background: #f1f5f9;
        color: var(--iso-muted);
        border-color: var(--iso-border);
    }

    .iso-topbar-right {
        display: flex;
        align-items: center;
        gap: 10px;
        flex-wrap: wrap;
    }

    .iso-day-label {
        font-size: 0.85rem;
        color: var(--iso-muted);
        font-weight: 600;
    }

    .iso-btn {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 8px 14px;
        background: #ffffff;
        border: 1px solid var(--iso-border);
        border-radius: 10px;
        color: var(--iso-text);
        font-size: 0.82rem;
        font-weight: 700;
        cursor: pointer;
        transition: all 0.15s ease;
        text-decoration: none;
    }

    .iso-btn:hover {
        background: var(--iso-primary);
        color: #ffffff;
        border-color: var(--iso-primary);
    }

    .iso-btn:disabled {
        opacity: 0.5;
        cursor: not-allowed;
    }

    /* ---- Banner ---- */
    .iso-banner {
        display: flex;
        align-items: flex-start;
        gap: 18px;
        padding: 22px 26px;
        margin-bottom: 22px;
        background: var(--iso-amber-bg);
        border: 1px solid var(--iso-amber-border);
        border-left: 5px solid var(--iso-amber);
        border-radius: 16px;
        box-shadow: var(--iso-shadow);
    }

    .iso-banner.is-watching {
        background: var(--iso-blue-bg);
        border-color: var(--iso-blue-border);
        border-left-color: var(--iso-primary);
    }

    .iso-banner.is-isolated {
        background: var(--iso-green-bg);
        border-color: #bbf7d0;
        border-left-color: var(--iso-green);
    }

    .iso-banner.is-simulate {
        background: var(--iso-simulate-bg);
        border-color: var(--iso-simulate-border);
        border-left-color: var(--iso-simulate);
    }

    .banner-icon {
        flex: 0 0 auto;
        width: 52px;
        height: 52px;
        display: flex;
        align-items: center;
        justify-content: center;
        background: rgba(255, 255, 255, 0.7);
        border-radius: 12px;
        font-size: 1.5rem;
    }

    .iso-banner .banner-title {
        font-size: 1.05rem;
        font-weight: 800;
        margin-bottom: 6px;
        color: var(--iso-amber);
    }

    .iso-banner.is-watching .banner-title {
        color: var(--iso-primary);
    }

    .iso-banner.is-isolated .banner-title {
        color: var(--iso-green);
    }

    .iso-banner.is-simulate .banner-title {
        color: var(--iso-simulate);
    }

    .iso-banner .banner-message {
        font-size: 0.95rem;
        color: #78350f;
        margin-bottom: 6px;
        line-height: 1.5;
    }

    .iso-banner.is-watching .banner-message,
    .iso-banner.is-isolated .banner-message,
    .iso-banner.is-simulate .banner-message {
        color: var(--iso-text);
    }

    .iso-banner .banner-subtext {
        font-size: 0.82rem;
        color: var(--iso-amber);
        opacity: 0.85;
    }

    /* ---- Hero card ---- */
    .iso-hero {
        background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
        color: #f8fafc;
        border-radius: 20px;
        padding: 30px 34px;
        margin-bottom: 22px;
        box-shadow: 0 16px 44px rgba(15, 23, 42, 0.28);
        display: flex;
        justify-content: space-between;
        gap: 26px;
        flex-wrap: wrap;
        border: 1px solid transparent;
    }

    .iso-hero.is-simulate {
        border-color: var(--iso-simulate);
        box-shadow: 0 16px 44px rgba(234, 88, 12, 0.32);
    }

    .iso-hero-left {
        flex: 1 1 380px;
        min-width: 0;
    }

    .iso-hero-label {
        display: inline-block;
        font-size: 0.72rem;
        font-weight: 800;
        color: #4ade80;
        background: rgba(74, 222, 128, 0.12);
        padding: 4px 12px;
        border-radius: 999px;
        letter-spacing: 1px;
        margin-bottom: 12px;
    }

    .iso-hero.is-simulate .iso-hero-label {
        color: #fdba74;
        background: rgba(253, 186, 116, 0.16);
    }

    .iso-hero-symbol {
        font-size: 2rem;
        font-weight: 800;
        color: #ffffff;
        line-height: 1.15;
        letter-spacing: -0.5px;
        margin-bottom: 6px;
        word-break: break-word;
    }

    .iso-hero-key {
        font-size: 0.85rem;
        color: #94a3b8;
        margin-bottom: 14px;
        font-family: "JetBrains Mono", "Fira Code", "Courier New", monospace;
    }

    .iso-hero-tags {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        margin-bottom: 14px;
    }

    .iso-hero-tags .tag {
        display: inline-block;
        padding: 4px 12px;
        border-radius: 999px;
        font-size: 0.75rem;
        font-weight: 700;
    }

    .iso-hero-tags .tag.r {
        background: rgba(239, 68, 68, 0.16);
        color: #fca5a5;
        border: 1px solid rgba(239, 68, 68, 0.3);
    }

    .iso-hero-tags .tag.s {
        background: rgba(34, 197, 94, 0.16);
        color: #86efac;
        border: 1px solid rgba(34, 197, 94, 0.3);
    }

    .iso-hero-tags .tag.th {
        background: rgba(148, 163, 184, 0.16);
        color: #cbd5e1;
        border: 1px solid rgba(148, 163, 184, 0.3);
    }

    .iso-hero-tags .tag.promo {
        background: rgba(124, 58, 237, 0.20);
        color: #c4b5fd;
        border: 1px solid rgba(124, 58, 237, 0.45);
        letter-spacing: 0.6px;
    }

    .iso-hero-tags .tag.sim {
        background: rgba(234, 88, 12, 0.22);
        color: #fdba74;
        border: 1px solid rgba(234, 88, 12, 0.5);
        letter-spacing: 0.6px;
    }

    .iso-hero-meta {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
        gap: 10px 20px;
        font-size: 0.82rem;
        color: #cbd5e1;
    }

    .iso-hero-meta .meta-label {
        color: #94a3b8;
        font-weight: 600;
        display: block;
        font-size: 0.7rem;
        letter-spacing: 0.5px;
        text-transform: uppercase;
        margin-bottom: 2px;
    }

    .iso-hero-meta .meta-value {
        color: #ffffff;
        font-weight: 700;
        font-size: 0.95rem;
    }

    .iso-hero-meta .meta-value.highlight {
        color: #4ade80;
    }

    .iso-hero-meta .meta-value.sim {
        color: #fdba74;
    }

    .iso-hero-right {
        flex: 0 0 auto;
        display: flex;
        flex-direction: column;
        align-items: flex-end;
        justify-content: space-between;
        gap: 12px;
        min-width: 220px;
    }

    .iso-hero-price {
        text-align: right;
    }

    .iso-hero-price .price-label {
        font-size: 0.72rem;
        color: #94a3b8;
        letter-spacing: 0.5px;
        text-transform: uppercase;
        margin-bottom: 2px;
    }

    .iso-hero-price .price-value {
        font-size: 1.6rem;
        font-weight: 800;
        color: #ffffff;
    }

    .iso-hero-price .price-value.instrument {
        color: #4ade80;
    }

    .iso-hero-price .price-sub {
        font-size: 0.8rem;
        color: #94a3b8;
        margin-top: 4px;
    }

    .iso-hero-chart-btn {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        padding: 12px 22px;
        background: #16a34a;
        color: #ffffff;
        border-radius: 12px;
        font-weight: 800;
        font-size: 0.9rem;
        text-decoration: none;
        transition: all 0.15s ease;
        box-shadow: 0 6px 20px rgba(22, 163, 74, 0.35);
    }

    .iso-hero-chart-btn:hover {
        background: #15803d;
        color: #ffffff;
        transform: translateY(-1px);
    }

    .iso-hero-layout-btn {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        padding: 12px 22px;
        background: var(--iso-primary);
        color: #ffffff;
        border-radius: 12px;
        font-weight: 800;
        font-size: 0.9rem;
        text-decoration: none;
        transition: all 0.15s ease;
        box-shadow: 0 6px 20px rgba(37, 99, 235, 0.35);
    }

    .iso-hero-layout-btn:hover {
        background: #1d4ed8;
        color: #ffffff;
        transform: translateY(-1px);
    }

    .iso-hero-layout-btn.is-hidden {
        display: none;
    }

    /* ---- Live Feed card ---- */
    .live-feed-tiles {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
        gap: 14px;
    }

    .live-tile {
        padding: 16px 20px;
        background: #f8fafc;
        border: 1px solid var(--iso-border);
        border-radius: 14px;
        transition: border-color 0.15s ease, box-shadow 0.15s ease;
    }

    .live-tile:hover {
        border-color: var(--iso-primary);
        box-shadow: 0 4px 12px rgba(37, 99, 235, 0.08);
    }

    .live-tile.live-tile-primary {
        background: linear-gradient(135deg, #f0f7ff 0%, #ffffff 100%);
        border-color: var(--iso-primary);
        grid-column: span 2;
    }

    .live-tile.live-tile-trigger {
        border-color: var(--iso-simulate);
        background: linear-gradient(135deg, #fff7ed 0%, #ffffff 100%);
    }

    @media (max-width: 768px) {
        .live-tile.live-tile-primary {
            grid-column: span 1;
        }
    }

    .live-tile-label {
        font-size: 0.72rem;
        font-weight: 800;
        color: var(--iso-muted);
        text-transform: uppercase;
        letter-spacing: 0.5px;
        margin-bottom: 8px;
        display: flex;
        align-items: center;
        gap: 6px;
    }

    .live-tile-value {
        font-size: 1.5rem;
        font-weight: 800;
        color: var(--iso-text);
        font-variant-numeric: tabular-nums;
        line-height: 1.2;
        transition: color 0.2s ease;
    }

    .live-tile.live-tile-primary .live-tile-value {
        font-size: 2rem;
    }

    .live-tile-value.up {
        color: var(--iso-green);
    }

    .live-tile-value.down {
        color: var(--iso-red);
    }

    .live-tile-sub {
        font-size: 0.82rem;
        color: var(--iso-muted);
        font-weight: 700;
        margin-top: 6px;
        font-variant-numeric: tabular-nums;
    }

    .live-tile-sub.up {
        color: var(--iso-green);
    }

    .live-tile-sub.down {
        color: var(--iso-red);
    }

    .live-feed-meta {
        font-size: 0.78rem;
        color: var(--iso-muted);
        font-weight: 600;
        margin-top: 14px;
        padding-top: 12px;
        border-top: 1px solid var(--iso-border);
        font-variant-numeric: tabular-nums;
    }

    /* ---- Generic cards ---- */
    .iso-card {
        background: var(--iso-card-bg);
        border: 1px solid var(--iso-border);
        border-radius: 16px;
        margin-bottom: 20px;
        box-shadow: var(--iso-shadow);
        overflow: hidden;
    }

    .iso-card-header {
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 16px 22px;
        border-bottom: 1px solid var(--iso-border);
        background: #f8fafc;
        font-weight: 800;
        font-size: 0.95rem;
    }

    .iso-card-header i {
        font-size: 1.05rem;
    }

    .iso-card-body {
        padding: 20px 22px;
    }

    .iso-empty {
        text-align: center;
        color: var(--iso-muted);
        padding: 24px;
        font-size: 0.9rem;
    }

    /* ---- Two-column layout ---- */
    .iso-row {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 20px;
    }

    @media (max-width: 1024px) {
        .iso-row {
            grid-template-columns: 1fr;
        }
    }

    /* ---- Tables ---- */
    .iso-table-wrap {
        overflow-x: auto;
    }

    .iso-table {
        width: 100%;
        border-collapse: collapse;
        font-size: 0.85rem;
    }

    .iso-table thead {
        background: #f8fafc;
        border-bottom: 1px solid var(--iso-border);
    }

    .iso-table th {
        padding: 10px 14px;
        text-align: left;
        font-weight: 700;
        color: var(--iso-muted);
        font-size: 0.72rem;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        white-space: nowrap;
    }

    .iso-table th.num,
    .iso-table td.num {
        text-align: right;
        font-variant-numeric: tabular-nums;
    }

    .iso-table td {
        padding: 11px 14px;
        border-bottom: 1px solid #f1f5f9;
        vertical-align: middle;
    }

    .iso-table tr:last-child td {
        border-bottom: none;
    }

    .iso-table tr.is-picked td {
        background: #ecfdf5;
        font-weight: 700;
    }

    .iso-table tr.is-trigger-row td {
        background: #fff7ed;
    }

    .iso-table tr.is-simulate-row td:first-child {
        border-left: 3px solid var(--iso-simulate);
    }

    .iso-table tr.is-counter-candidate td {
        opacity: 0.82;
    }

    .type-pill {
        display: inline-block;
        padding: 2px 9px;
        border-radius: 6px;
        font-size: 0.68rem;
        font-weight: 800;
    }

    .type-pill.ce {
        background: var(--iso-green-bg);
        color: var(--iso-green);
    }

    .type-pill.pe {
        background: var(--iso-red-bg);
        color: var(--iso-red);
    }

    .level-tag {
        display: inline-block;
        padding: 1px 7px;
        border-radius: 4px;
        font-size: 0.68rem;
        font-weight: 700;
        background: #e0e7ff;
        color: #3730a3;
        margin-right: 3px;
    }

    .level-tag.r {
        background: var(--iso-red-bg);
        color: #991b1b;
    }

    .level-tag.s {
        background: var(--iso-green-bg);
        color: #166534;
    }

    .touch-chip {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 4px 12px;
        border-radius: 999px;
        font-size: 0.75rem;
        font-weight: 700;
    }

    .touch-chip.touched {
        background: var(--iso-green-bg);
        color: var(--iso-green);
    }

    .touch-chip.not-touched {
        background: #f1f5f9;
        color: var(--iso-muted);
    }

    /* ---- Reasoning steps ---- */
    .reasoning-steps {
        display: flex;
        flex-direction: column;
        gap: 14px;
    }

    .reason-step {
        display: flex;
        gap: 14px;
        align-items: flex-start;
        padding: 14px 16px;
        background: #f8fafc;
        border: 1px solid var(--iso-border);
        border-left: 4px solid var(--iso-primary);
        border-radius: 12px;
    }

    .reason-step:last-child {
        border-left-color: var(--iso-green);
        background: #ecfdf5;
    }

    .reason-step-icon {
        flex: 0 0 auto;
        width: 36px;
        height: 36px;
        display: flex;
        align-items: center;
        justify-content: center;
        background: #ffffff;
        border: 1px solid var(--iso-border);
        border-radius: 10px;
        color: var(--iso-primary);
        font-size: 1.05rem;
    }

    .reason-step:last-child .reason-step-icon {
        color: var(--iso-green);
        border-color: #bbf7d0;
    }

    .reason-step-body {
        flex: 1;
    }

    .reason-step-title {
        font-size: 0.85rem;
        font-weight: 800;
        color: var(--iso-text);
        margin-bottom: 4px;
        text-transform: uppercase;
        letter-spacing: 0.4px;
    }

    .reason-step-detail {
        font-size: 0.88rem;
        color: var(--iso-muted);
        line-height: 1.55;
    }

    /* ---- OR levels mini-grid ---- */
    .or-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
        gap: 12px;
    }

    .or-block {
        background: #f8fafc;
        border: 1px solid var(--iso-border);
        border-radius: 12px;
        padding: 14px 16px;
    }

    .or-block-title {
        font-size: 0.78rem;
        font-weight: 800;
        color: var(--iso-primary);
        text-transform: uppercase;
        letter-spacing: 0.5px;
        margin-bottom: 10px;
    }

    .or-cell {
        display: flex;
        justify-content: space-between;
        padding: 3px 0;
        font-size: 0.82rem;
    }

    .or-cell .or-label {
        color: var(--iso-muted);
    }

    .or-cell .or-value {
        font-weight: 700;
        font-variant-numeric: tabular-nums;
    }

    .or-cell.or-hit .or-value {
        color: var(--iso-green);
    }

    /* ---- Backfill button state ---- */
    .iso-btn.primary {
        background: var(--iso-primary);
        color: #ffffff;
        border-color: var(--iso-primary);
    }

    .iso-btn.primary:hover {
        background: #1d4ed8;
        border-color: #1d4ed8;
    }

    .iso-btn.simulate {
        background: var(--iso-simulate);
        color: #ffffff;
        border-color: var(--iso-simulate);
    }

    .iso-btn.simulate:hover {
        background: #c2410c;
        border-color: #c2410c;
    }

    /* ---- Instrument snapshot (own data at touch) ---- */
    .snapshot-tiles {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
        gap: 14px;
        margin-bottom: 20px;
    }

    .snapshot-tile {
        padding: 14px 18px;
        background: #f8fafc;
        border: 1px solid var(--iso-border);
        border-radius: 12px;
        transition: border-color 0.15s ease;
    }

    .snapshot-tile:hover {
        border-color: var(--iso-primary);
    }

    .snapshot-tile .tile-label {
        font-size: 0.72rem;
        font-weight: 800;
        color: var(--iso-muted);
        text-transform: uppercase;
        letter-spacing: 0.4px;
        margin-bottom: 6px;
    }

    .snapshot-tile .tile-value {
        font-size: 1.15rem;
        font-weight: 800;
        color: var(--iso-text);
        font-variant-numeric: tabular-nums;
        line-height: 1.4;
    }

    .snapshot-tile .tile-value .ohlc-grid {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 2px 12px;
        font-size: 0.92rem;
        font-weight: 700;
        margin-top: 4px;
    }

    .snapshot-tile .tile-value .ohlc-grid .ohlc-cell {
        display: flex;
        justify-content: space-between;
        gap: 8px;
    }

    .snapshot-tile .tile-value .ohlc-grid .ohlc-cell .ohlc-key {
        color: var(--iso-muted);
        font-weight: 600;
    }

    .snapshot-tile .tile-sub {
        font-size: 0.72rem;
        color: var(--iso-muted);
        margin-top: 6px;
        font-weight: 600;
    }

    .snapshot-tile.highlight {
        background: #ecfdf5;
        border-color: #bbf7d0;
    }

    .snapshot-tile.highlight .tile-value {
        color: var(--iso-green);
        font-size: 1.5rem;
    }

    .snapshot-tile.highlight .tile-label {
        color: #15803d;
    }

    .snapshot-section-title {
        font-size: 0.8rem;
        font-weight: 800;
        color: var(--iso-text);
        text-transform: uppercase;
        letter-spacing: 0.5px;
        margin: 16px 0 10px 0;
        padding-top: 16px;
        border-top: 1px solid var(--iso-border);
    }

    .snapshot-section-title:first-child {
        margin-top: 0;
        padding-top: 0;
        border-top: none;
    }

    .snapshot-or-block {
        background: #f8fafc;
        border: 1px solid var(--iso-border);
        border-radius: 12px;
        padding: 14px 18px;
    }

    .snapshot-or-block-title {
        font-size: 0.8rem;
        font-weight: 800;
        color: var(--iso-primary);
        text-transform: uppercase;
        letter-spacing: 0.5px;
        margin-bottom: 10px;
        display: flex;
        align-items: center;
        gap: 8px;
        flex-wrap: wrap;
    }

    .snapshot-or-block-title .snapshot-or-type-badge {
        display: inline-block;
        padding: 2px 8px;
        border-radius: 6px;
        font-size: 0.68rem;
        font-weight: 800;
        background: #e0e7ff;
        color: #3730a3;
    }

    .snapshot-or-block-title .snapshot-or-type-badge.ce {
        background: var(--iso-green-bg);
        color: var(--iso-green);
    }

    .snapshot-or-block-title .snapshot-or-type-badge.pe {
        background: var(--iso-red-bg);
        color: var(--iso-red);
    }

    .snapshot-empty-state {
        grid-column: 1 / -1;
        padding: 24px;
        text-align: center;
        color: var(--iso-muted);
        font-size: 0.9rem;
        line-height: 1.6;
    }

    .snapshot-empty-state i {
        font-size: 1.8rem;
        display: block;
        margin-bottom: 10px;
        opacity: 0.4;
    }

    .snapshot-empty-state strong {
        color: var(--iso-text);
    }

    /* ---- SR Tracking — single merged table ---- */
    .sr-header-pills {
        margin-left: auto;
        display: flex;
        gap: 8px;
    }

    .sr-count-pill {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        padding: 3px 11px;
        border-radius: 999px;
        font-size: 0.72rem;
        font-weight: 800;
        background: #f1f5f9;
        color: var(--iso-muted);
        border: 1px solid var(--iso-border);
    }

    .sr-count-pill.has {
        background: #e0e7ff;
        color: #3730a3;
        border-color: #bfdbfe;
    }

    .sr-count-pill.post {
        background: var(--iso-purple-bg);
        color: var(--iso-purple);
        border-color: var(--iso-purple-border);
    }

    .sr-count-pill.chosen {
        background: var(--iso-green-bg);
        color: var(--iso-green);
        border-color: #bbf7d0;
    }

    .sr-count-pill.sim {
        background: var(--iso-simulate-bg);
        color: var(--iso-simulate);
        border-color: var(--iso-simulate-border);
    }

    .sr-count-pill.error {
        background: var(--iso-red-bg);
        color: var(--iso-red);
        border-color: #fecaca;
    }

    .sr-phase-header {
        display: flex;
        align-items: center;
        gap: 10px;
        margin: 0 0 12px 0;
        flex-wrap: wrap;
    }

    .sr-phase-header .phase-title {
        font-size: 0.8rem;
        font-weight: 800;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        color: var(--iso-text);
    }

    .sr-phase-header .phase-hint {
        font-size: 0.7rem;
        color: var(--iso-muted);
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.4px;
    }

    .sr-phase-header .ref-label {
        font-size: 0.72rem;
        color: var(--iso-muted);
        font-weight: 600;
        font-family: "JetBrains Mono", "Fira Code", "Courier New", monospace;
    }

    /* ---- Legend explaining row colors ---- */
    .sr-legend {
        margin-left: auto;
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
    }

    .sr-legend-item {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 3px 10px;
        border-radius: 6px;
        font-size: 0.68rem;
        font-weight: 700;
        letter-spacing: 0.3px;
        text-transform: uppercase;
    }

    .sr-legend-item::before {
        content: "";
        width: 10px;
        height: 10px;
        border-radius: 3px;
    }

    .sr-legend-item.before {
        background: #f8fafc;
        color: var(--iso-muted);
        border: 1px solid var(--iso-border);
    }

    .sr-legend-item.before::before {
        background: #f1f5f9;
        border: 1px solid #cbd5e1;
    }

    .sr-legend-item.after {
        background: #f5f3ff;
        color: var(--iso-purple);
        border: 1px solid var(--iso-purple-border);
    }

    .sr-legend-item.after::before {
        background: var(--iso-purple);
    }

    .sr-type {
        display: inline-block;
        padding: 2px 9px;
        border-radius: 5px;
        font-size: 0.68rem;
        font-weight: 800;
        letter-spacing: 0.4px;
    }

    .sr-type.resistance {
        background: var(--iso-red-bg);
        color: var(--iso-red);
    }

    .sr-type.support {
        background: var(--iso-green-bg);
        color: var(--iso-green);
    }

    .sr-row-after-isolation td {
        background: #f5f3ff !important;
    }

    .sr-row-after-isolation td:first-child {
        border-left: 3px solid var(--iso-purple);
    }

    .sr-new-badge {
        display: inline-block;
        padding: 1px 7px;
        border-radius: 4px;
        font-size: 0.62rem;
        font-weight: 800;
        letter-spacing: 0.3px;
        background: var(--iso-purple);
        color: #ffffff;
        margin-left: 6px;
        vertical-align: middle;
    }

    .sr-empty-state {
        padding: 22px 18px;
        text-align: center;
        color: var(--iso-muted);
        font-size: 0.85rem;
        background: #f8fafc;
        border: 1px dashed var(--iso-border);
        border-radius: 10px;
        line-height: 1.55;
    }

    .sr-empty-state i {
        display: block;
        font-size: 1.6rem;
        margin-bottom: 8px;
        opacity: 0.4;
    }

    /* ---- SR Compare card — search + comparison view ---- */
    .sr-search-row {
        display: flex;
        flex-direction: column;
        gap: 8px;
        margin-bottom: 12px;
    }

    .sr-search-input-wrap {
        display: flex;
        align-items: center;
        gap: 10px;
        background: #ffffff;
        border: 1px solid var(--iso-border);
        border-radius: 12px;
        padding: 10px 14px;
        transition: border-color 0.15s ease, box-shadow 0.15s ease;
    }

    .sr-search-input-wrap:focus-within {
        border-color: var(--iso-primary);
        box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.12);
    }

    .sr-search-input-wrap i {
        color: var(--iso-muted);
        font-size: 0.95rem;
    }

    .sr-search-input-wrap input {
        flex: 1;
        border: none;
        outline: none;
        background: transparent;
        font-size: 0.9rem;
        color: var(--iso-text);
        font-weight: 500;
    }

    .sr-search-input-wrap input::placeholder {
        color: var(--iso-muted);
    }

    .sr-search-hint {
        font-size: 0.72rem;
        color: var(--iso-muted);
        font-weight: 600;
        padding-left: 4px;
    }

    .sr-search-results {
        display: flex;
        flex-direction: column;
        gap: 6px;
        margin-bottom: 12px;
    }

    .sr-search-results:empty {
        display: none;
    }

    .sr-search-result {
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 10px 14px;
        background: #ffffff;
        border: 1px solid var(--iso-border);
        border-radius: 10px;
        cursor: pointer;
        text-align: left;
        transition: all 0.15s ease;
        font-family: inherit;
        font-size: 0.85rem;
    }

    .sr-search-result:hover {
        border-color: var(--iso-primary);
        background: #f0f7ff;
        transform: translateY(-1px);
    }

    .sr-search-result.is-official {
        border-color: var(--iso-green);
        background: #ecfdf5;
    }

    .sr-search-result .sr-result-symbol {
        font-weight: 700;
        color: var(--iso-text);
        flex: 1;
    }

    .sr-search-result .sr-result-meta {
        font-size: 0.75rem;
        color: var(--iso-muted);
        font-weight: 600;
    }

    .sr-search-info {
        display: flex;
        align-items: flex-start;
        gap: 14px;
        padding: 16px 18px;
        border-radius: 12px;
        margin-bottom: 14px;
        font-size: 0.88rem;
        line-height: 1.55;
    }

    .sr-search-info i {
        font-size: 1.3rem;
        flex-shrink: 0;
        margin-top: 1px;
    }

    .sr-search-info.is-user-selected {
        background: var(--iso-blue-bg);
        border: 1px solid var(--iso-blue-border);
        border-left: 4px solid var(--iso-primary);
        color: var(--iso-text);
    }

    .sr-search-info.is-user-selected i {
        color: var(--iso-primary);
    }

    .sr-search-info.is-official {
        background: var(--iso-green-bg);
        border: 1px solid #bbf7d0;
        border-left: 4px solid var(--iso-green);
        color: var(--iso-text);
    }

    .sr-search-info.is-official i {
        color: var(--iso-green);
    }

    .sr-search-info .muted {
        color: var(--iso-muted);
        font-weight: 600;
    }

    .sr-search-body {
        margin-top: 4px;
    }

    /* ---- Entry Signal card ---- */
    .entry-touch-strip {
        display: flex;
        gap: 16px;
        flex-wrap: wrap;
        font-size: 0.82rem;
        color: var(--iso-muted);
        padding: 12px 16px;
        background: #f8fafc;
        border: 1px solid var(--iso-border);
        border-radius: 10px;
        margin-bottom: 16px;
    }

    .entry-touch-strip strong {
        color: var(--iso-text);
    }

    .entry-candidates {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 16px;
    }

    @media (max-width: 900px) {
        .entry-candidates {
            grid-template-columns: 1fr;
        }
    }

    .entry-candidate {
        background: #ffffff;
        border: 1px solid var(--iso-border);
        border-radius: 12px;
        padding: 16px;
        position: relative;
        transition: all 0.15s ease;
    }

    .entry-candidate.is-chosen {
        border-color: var(--iso-green);
        box-shadow: 0 0 0 2px rgba(22, 163, 74, 0.15);
    }

    .entry-candidate.is-bias-side {
        border-top: 4px solid var(--iso-primary);
    }

    .entry-candidate.is-auto-reverse {
        border-top: 4px solid var(--iso-purple);
    }

    .entry-candidate-head {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        margin-bottom: 8px;
        flex-wrap: wrap;
    }

    .entry-candidate-symbol {
        font-weight: 800;
        font-size: 0.95rem;
        color: var(--iso-text);
        word-break: break-word;
    }

    .entry-candidate-pills {
        display: flex;
        gap: 6px;
        flex-wrap: wrap;
    }

    .entry-pill {
        font-size: 0.68rem;
        font-weight: 800;
        padding: 2px 8px;
        border-radius: 6px;
        text-transform: uppercase;
        letter-spacing: 0.3px;
    }

    .entry-pill.type-ce {
        background: var(--iso-green-bg);
        color: var(--iso-green);
    }

    .entry-pill.type-pe {
        background: var(--iso-red-bg);
        color: var(--iso-red);
    }

    .entry-pill.bias {
        background: var(--iso-blue-bg);
        color: var(--iso-primary);
    }

    .entry-pill.auto-reverse {
        background: var(--iso-purple-bg);
        color: var(--iso-purple);
    }

    /* G1 v4.1 §5.E — "PINNED" pill on the chosen candidate when its
       entry values come from `payload.frozen_entry` rather than the
       live-recomputed path. */
    .entry-pill.frozen {
        background: var(--iso-amber-bg);
        color: var(--iso-amber);
        border: 1px solid var(--iso-amber-border);
    }

    .entry-pill.state-waiting {
        background: #f1f5f9;
        color: var(--iso-muted);
    }

    .entry-pill.state-waiting_confirmation {
        background: var(--iso-amber-bg);
        color: var(--iso-amber);
    }

    .entry-pill.state-blocked {
        background: var(--iso-red-bg);
        color: var(--iso-red);
    }

    .entry-pill.state-armed {
        background: var(--iso-green-bg);
        color: var(--iso-green);
    }

    .entry-pill.state-discarded {
        background: #e2e8f0;
        color: var(--iso-muted);
    }

    .entry-pill.state-error {
        background: var(--iso-red-bg);
        color: var(--iso-red);
    }

    .entry-note {
        font-size: 0.78rem;
        color: var(--iso-muted);
        line-height: 1.55;
        margin: 8px 0 6px 0;
    }

    .entry-grid {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 6px 12px;
        font-size: 0.8rem;
        margin-top: 10px;
    }

    .entry-grid .label {
        color: var(--iso-muted);
    }

    .entry-grid .value {
        color: var(--iso-text);
        font-weight: 700;
        text-align: right;
        font-variant-numeric: tabular-nums;
    }

    .entry-grid .value small {
        color: var(--iso-muted);
        font-weight: 400;
        font-size: 0.7rem;
    }

    /* G1 v4.1 §5.E — inline "pinned" marker on the entry-price row
       when the value is frozen. */
    .entry-grid .value small.entry-frozen-mark {
        color: var(--iso-amber);
        font-weight: 700;
        font-size: 0.7rem;
    }

    .entry-warning {
        background: var(--iso-amber-bg);
        border-left: 4px solid var(--iso-amber);
        color: var(--iso-amber);
        padding: 8px 12px;
        border-radius: 6px;
        font-size: 0.78rem;
        margin-top: 12px;
        line-height: 1.5;
    }

    .entry-reversal {
        background: var(--iso-purple-bg);
        border-left: 4px solid var(--iso-purple);
        color: var(--iso-purple);
        padding: 8px 12px;
        border-radius: 6px;
        font-size: 0.78rem;
        margin-top: 12px;
        line-height: 1.5;
    }

    .entry-warnings-list {
        background: var(--iso-amber-bg);
        border-left: 4px solid var(--iso-amber);
        color: var(--iso-amber);
        padding: 10px 14px;
        border-radius: 8px;
        font-size: 0.8rem;
        margin-top: 12px;
        line-height: 1.55;
    }

    .entry-warnings-list ul {
        margin: 6px 0 0 0;
        padding-left: 20px;
    }

    .entry-warnings-list li {
        margin-bottom: 4px;
    }

    .entry-empty {
        grid-column: 1 / -1;
        padding: 20px;
        text-align: center;
        color: var(--iso-muted);
        font-size: 0.88rem;
        background: #f8fafc;
        border: 1px dashed var(--iso-border);
        border-radius: 10px;
    }

    .entry-empty i {
        display: block;
        font-size: 1.6rem;
        margin-bottom: 8px;
        opacity: 0.4;
    }

    .entry-error {
        grid-column: 1 / -1;
        padding: 20px;
        text-align: center;
        color: var(--iso-red);
        font-size: 0.88rem;
        background: var(--iso-red-bg);
        border: 1px solid #fecaca;
        border-radius: 10px;
    }

    .entry-promotion-banner {
        display: flex;
        align-items: flex-start;
        gap: 12px;
        padding: 12px 16px;
        background: var(--iso-purple-bg);
        border: 1px solid var(--iso-purple-border);
        border-left: 4px solid var(--iso-purple);
        border-radius: 10px;
        color: var(--iso-text);
        font-size: 0.85rem;
        line-height: 1.5;
        margin-bottom: 14px;
    }

    .entry-promotion-banner i {
        color: var(--iso-purple);
        font-size: 1.2rem;
        flex-shrink: 0;
        margin-top: 1px;
    }

    .entry-promotion-banner strong {
        color: var(--iso-purple);
    }

    .entry-promotion-banner .promo-pending {
        background: rgba(124, 58, 237, 0.08);
        padding: 2px 8px;
        border-radius: 6px;
        font-weight: 700;
        font-size: 0.78rem;
        margin-left: 6px;
        white-space: nowrap;
    }

    /* ---- G1 v4.1 §5.F — candidate side chips (Candidates table) ---- */
    .side-chip {
        display: inline-block;
        padding: 2px 9px;
        border-radius: 5px;
        font-size: 0.65rem;
        font-weight: 800;
        letter-spacing: 0.4px;
        text-transform: uppercase;
    }

    .side-chip.bias {
        background: var(--iso-blue-bg);
        color: var(--iso-primary);
        border: 1px solid var(--iso-blue-border);
    }

    .side-chip.counter {
        background: var(--iso-purple-bg);
        color: var(--iso-purple);
        border: 1px solid var(--iso-purple-border);
    }

    .side-chip.legacy {
        background: #f1f5f9;
        color: var(--iso-muted);
        border: 1px solid var(--iso-border);
        font-style: italic;
        text-transform: none;
        letter-spacing: 0;
    }

    /* ---- Simulation card (dummy-order replay) ---- */
    .sim-run-row {
        display: flex;
        gap: 10px;
        margin-bottom: 14px;
        flex-wrap: wrap;
        align-items: center;
    }

    .sim-run-row .sim-hint {
        font-size: 0.8rem;
        color: var(--iso-muted);
        font-weight: 600;
        line-height: 1.4;
        flex: 1 1 240px;
    }

    .sim-regime-banner {
        display: flex;
        align-items: flex-start;
        gap: 12px;
        padding: 12px 16px;
        border-radius: 10px;
        margin-bottom: 14px;
        font-size: 0.85rem;
        line-height: 1.5;
        background: var(--iso-purple-bg);
        border: 1px solid var(--iso-purple-border);
        border-left: 4px solid var(--iso-purple);
        color: var(--iso-text);
    }

    .sim-regime-banner i {
        font-size: 1.2rem;
        flex-shrink: 0;
        margin-top: 1px;
    }

    .sim-regime-banner strong {
        font-weight: 800;
    }

    .sim-verdict-winner {
        background: var(--iso-green-bg);
        border-left: 4px solid var(--iso-green);
        color: var(--iso-green);
        padding: 10px 14px;
        border-radius: 8px;
        font-size: 0.82rem;
        margin-top: 12px;
        line-height: 1.55;
    }

    .sim-verdict-tie {
        background: #f1f5f9;
        border-left: 4px solid var(--iso-muted);
        color: var(--iso-muted);
        padding: 10px 14px;
        border-radius: 8px;
        font-size: 0.82rem;
        margin-top: 12px;
    }

    .sim-notes {
        background: #f8fafc;
        border-left: 4px solid var(--iso-primary);
        color: var(--iso-text);
        padding: 10px 14px;
        border-radius: 8px;
        font-size: 0.8rem;
        margin-top: 12px;
        line-height: 1.55;
    }

    .sim-notes strong {
        color: var(--iso-primary);
        display: block;
        margin-bottom: 6px;
        font-size: 0.82rem;
        letter-spacing: 0.3px;
        text-transform: uppercase;
    }

    .sim-notes ul {
        margin: 0;
        padding-left: 18px;
    }

    .sim-notes li {
        margin-bottom: 3px;
    }

    /* ---- Dummy Orders card (live-tracked) ---- */
    .do-summary-strip {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
        gap: 12px;
        margin-bottom: 16px;
    }

    .do-summary-tile {
        padding: 12px 16px;
        background: #f8fafc;
        border: 1px solid var(--iso-border);
        border-radius: 10px;
    }

    .do-summary-tile .do-label {
        font-size: 0.7rem;
        font-weight: 800;
        color: var(--iso-muted);
        text-transform: uppercase;
        letter-spacing: 0.5px;
        margin-bottom: 4px;
    }

    .do-summary-tile .do-value {
        font-size: 1.15rem;
        font-weight: 800;
        font-variant-numeric: tabular-nums;
        color: var(--iso-text);
    }

    .do-summary-tile .do-value.up { color: var(--iso-green); }
    .do-summary-tile .do-value.down { color: var(--iso-red); }

    .do-status-pill {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        padding: 2px 8px;
        border-radius: 6px;
        font-size: 0.68rem;
        font-weight: 800;
        letter-spacing: 0.3px;
        text-transform: uppercase;
    }

    .do-status-pill.open             { background: var(--iso-blue-bg); color: var(--iso-primary); }
    .do-status-pill.target_hit       { background: var(--iso-green-bg); color: var(--iso-green); }
    .do-status-pill.stop_hit         { background: var(--iso-red-bg); color: var(--iso-red); }
    .do-status-pill.end_of_day       { background: #f1f5f9; color: var(--iso-muted); }
    .do-status-pill.cancelled        { background: #e2e8f0; color: var(--iso-muted); }

    .do-scenario-pill {
        display: inline-block;
        padding: 1px 7px;
        border-radius: 4px;
        font-size: 0.65rem;
        font-weight: 800;
        letter-spacing: 0.3px;
    }

    .do-scenario-pill.signal_v2       { background: #dbeafe; color: #1e40af; }
    .do-scenario-pill.regime          { background: var(--iso-purple-bg); color: var(--iso-purple); }
    .do-scenario-pill.path_b_sr_level { background: var(--iso-simulate-bg); color: var(--iso-simulate); }

    .do-table td.num {
        font-variant-numeric: tabular-nums;
    }

    .do-row-open td {
        background: #f0f7ff;
    }

    .do-row-closed td {
        opacity: 0.85;
    }

    .do-empty {
        padding: 24px;
        text-align: center;
        color: var(--iso-muted);
        font-size: 0.88rem;
        background: #f8fafc;
        border: 1px dashed var(--iso-border);
        border-radius: 10px;
    }

    .do-empty i {
        display: block;
        font-size: 1.8rem;
        margin-bottom: 8px;
        opacity: 0.4;
    }

    /* ---- G1 v4.2 — Path B UI additions ---- */
    .entry-pill.path-b {
        background: var(--iso-simulate-bg);
        color: var(--iso-simulate);
        border: 1px solid var(--iso-simulate-border);
        font-weight: 800;
    }
    .do-path-b-badge {
        display: inline-block;
        padding: 1px 7px;
        border-radius: 4px;
        font-size: 0.62rem;
        font-weight: 800;
        letter-spacing: 0.4px;
        background: var(--iso-simulate-bg);
        color: var(--iso-simulate);
        border: 1px solid var(--iso-simulate-border);
        margin-left: 4px;
    }

    /* ---- G1 v4.3 — Source column + lot badge + price-source subtext ---- */
    .do-source-cell {
        font-size: 0.72rem;
        line-height: 1.45;
        min-width: 120px;
    }
    .do-source-cell .src-type {
        display: inline-block;
        padding: 1px 6px;
        border-radius: 3px;
        font-size: 0.6rem;
        font-weight: 800;
        letter-spacing: 0.3px;
        margin-right: 4px;
        vertical-align: middle;
    }
    .do-source-cell .src-type.resistance {
        background: var(--iso-red-bg);
        color: var(--iso-red);
    }
    .do-source-cell .src-type.support {
        background: var(--iso-green-bg);
        color: var(--iso-green);
    }
    .do-source-cell .src-type.signal {
        background: var(--iso-blue-bg);
        color: var(--iso-primary);
    }
    .do-source-cell .src-price {
        font-weight: 700;
        color: var(--iso-text);
        vertical-align: middle;
    }
    .do-source-cell .src-meta {
        display: block;
        margin-top: 2px;
        color: var(--iso-muted);
        font-size: 0.65rem;
        font-weight: 600;
    }
    .do-lot-badge {
        display: inline-block;
        padding: 1px 6px;
        border-radius: 3px;
        font-size: 0.62rem;
        font-weight: 700;
        background: #f1f5f9;
        color: var(--iso-muted);
        border: 1px solid var(--iso-border);
        margin-left: 4px;
    }
    .do-price-src {
        display: block;
        font-size: 0.62rem;
        color: var(--iso-muted);
        font-weight: 600;
        margin-top: 2px;
        line-height: 1.3;
    }
    .do-pnl-rs {
        display: block;
        font-size: 0.65rem;
        color: var(--iso-muted);
        font-weight: 600;
        margin-top: 2px;
    }
</style>
{% endblock %}

{% block content %}
<div class="iso-page">
    <!-- Top bar -->
    <div class="iso-topbar">
        <div class="iso-topbar-left">
            <h1 class="iso-topbar-title">
                <i class="bi bi-bullseye"></i>
                Isolated Instrument
            </h1>
            <span class="iso-status-pill off" id="status-pill">Loading…</span>
            <span class="iso-status-pill simulate" id="simulate-pill" style="display: none;">
                <i class="bi bi-arrow-clockwise"></i> SIMULATE MODE
            </span>
        </div>
        <div class="iso-topbar-right">
            <span class="iso-day-label" id="day-label">—</span>
            <button
                type="button"
                class="iso-btn"
                id="refresh-snapshot-btn"
                style="display: none;"
                title="Re-collect the isolated instrument's own data at the touch"
            >
                <i class="bi bi-arrow-repeat"></i> Refresh Snapshot
            </button>
            <button type="button" class="iso-btn" id="refresh-btn">
                <i class="bi bi-arrow-clockwise"></i> Refresh
            </button>
            <button
                type="button"
                class="iso-btn simulate"
                id="backfill-btn"
                style="display: none;"
            >
                <i class="bi bi-recycle"></i> Run Backfill
            </button>
        </div>
    </div>

    <!-- Non-trading / waiting banner -->
    <div class="iso-banner" id="iso-banner" style="display: none;">
        <div class="banner-icon" id="banner-icon">
            <i class="bi bi-hourglass-split"></i>
        </div>
        <div class="banner-body">
            <div class="banner-title" id="banner-title">Not isolated yet</div>
            <div class="banner-message" id="banner-message">
                Waiting for the first R3 / S3 touch.
            </div>
            <div class="banner-subtext" id="banner-subtext"></div>
        </div>
    </div>

    <!-- Hero card: isolated instrument (or the promoted / active one) -->
    <div class="iso-hero" id="iso-hero" style="display: none;">
        <div class="iso-hero-left">
            <div class="iso-hero-label">ISOLATED INSTRUMENT</div>
            <div class="iso-hero-symbol" id="hero-symbol">—</div>
            <div class="iso-hero-key" id="hero-key">—</div>
            <div class="iso-hero-tags" id="hero-tags"></div>
            <div class="iso-hero-meta" id="hero-meta"></div>
        </div>
        <div class="iso-hero-right">
            <a href="#" class="iso-hero-chart-btn" id="hero-chart-btn">
                <i class="bi bi-graph-up-arrow"></i> Open Chart
            </a>
            <a href="#" class="iso-hero-layout-btn is-hidden" id="hero-layout-btn"
               target="_blank" rel="noopener noreferrer"
               title="Open a new tab with the underlying index and this isolated instrument side-by-side">
                <i class="bi bi-columns-gap"></i> Compare in Layout
            </a>
            <div class="iso-hero-price">
                <div class="price-label">Instrument LTP @ touch</div>
                <div class="price-value instrument" id="hero-instrument-ltp">—</div>
                <div class="price-sub" id="hero-instrument-sub">—</div>
            </div>
            <div class="iso-hero-price">
                <div class="price-label">Trigger LTP</div>
                <div class="price-value" id="hero-trigger-ltp">—</div>
                <div class="price-sub" id="hero-trigger-sub">—</div>
            </div>
        </div>
    </div>

    <!-- Live Feed card -->
    <div class="iso-card" id="iso-live-feed" style="display: none;">
        <div class="iso-card-header">
            <i class="bi bi-broadcast text-primary"></i>
            <span id="live-feed-card-title">Live Feed</span>
            <div class="sr-header-pills">
                <span class="sr-count-pill" id="live-feed-status-pill">Connecting…</span>
                <span class="sr-count-pill has" id="live-feed-sub-count-pill">0 subscribed</span>
            </div>
        </div>
        <div class="iso-card-body">
            <div class="live-feed-tiles" id="live-feed-tiles"></div>
            <div class="live-feed-meta" id="live-feed-meta">Waiting for first tick…</div>
        </div>
    </div>

    <!-- Entry Signal card -->
    <div class="iso-card" id="iso-entry-signal" style="display: none;">
        <div class="iso-card-header">
            <i class="bi bi-bullseye text-primary"></i>
            <span>Entry Signal</span>
            <div class="sr-header-pills">
                <span class="sr-count-pill" id="entry-chosen-pill">Waiting</span>
            </div>
        </div>
        <div class="iso-card-body">
            <div class="entry-promotion-banner" id="entry-path-b-banner" style="display:none;">
                <i class="bi bi-signpost-split-fill"></i>
                <div>
                    <strong>Path B · External-index trigger.</strong>
                    The 10-minute fast scan and auto-reverse are
                    intentionally <em>disabled</em> for this session.
                    The bias-side instrument stays isolated all day; every
                    post-isolation SR level spawns its own dummy BUY order
                    with Rupee-based profit trailing.
                </div>
            </div>
            <div class="entry-promotion-banner" id="entry-promotion-banner" style="display: none;"></div>
            <div class="entry-touch-strip" id="entry-touch-strip"></div>
            <div class="entry-candidates" id="entry-candidates">
                <div class="entry-empty">
                    <i class="bi bi-hourglass-split"></i>
                    Loading entry signal…
                </div>
            </div>
            <div class="entry-warnings-list" id="entry-warnings" style="display: none;"></div>
        </div>
    </div>

    <!-- SR Tracking card -->
    <div class="iso-card" id="iso-sr-tracking" style="display: none;">
        <div class="iso-card-header">
            <i class="bi bi-activity text-primary"></i>
            <span>SR Tracking · Isolated Instrument</span>
            <div class="sr-header-pills">
                <span class="sr-count-pill" id="sr-before-pill">Before: 0</span>
                <span class="sr-count-pill post" id="sr-after-pill">After: 0</span>
            </div>
        </div>
        <div class="iso-card-body">
            <div id="sr-tracking-body">
                <div class="sr-empty-state">
                    <i class="bi bi-hourglass-split"></i>
                    SR tracking activates when an instrument is isolated.
                </div>
            </div>
        </div>
    </div>

    <!-- SR Compare card -->
    <div class="iso-card" id="iso-sr-compare" style="display: none;">
        <div class="iso-card-header">
            <i class="bi bi-search text-primary"></i>
            <span>Compare with another instrument</span>
            <div class="sr-header-pills">
                <span class="sr-count-pill" id="sr-compare-status-pill">Idle</span>
            </div>
        </div>
        <div class="iso-card-body">
            <div class="sr-search-row">
                <div class="sr-search-input-wrap">
                    <i class="bi bi-search"></i>
                    <input
                        type="text"
                        id="sr-search-input"
                        placeholder="Search instrument (e.g. NIFTY 22400 CE, 22400, PE, SENSEX)…"
                        autocomplete="off"
                    >
                </div>
                <span class="sr-search-hint" id="sr-search-hint">
                    Searches the full instrument registry. Select a match to view its SR levels tagged
                    against the official isolation touch time.
                </span>
            </div>
            <div id="sr-search-results" class="sr-search-results"></div>
            <div id="sr-search-info" class="sr-search-info" style="display: none;"></div>
            <div id="sr-search-body" class="sr-search-body"></div>
        </div>
    </div>

    <!-- ============================================================ -->
    <!-- Simulation card — post-close dummy-order replay              -->
    <!-- ============================================================ -->
    <div class="iso-card" id="iso-simulation" style="display: none;">
        <div class="iso-card-header">
            <i class="bi bi-calculator text-primary"></i>
            <span>Simulation · Dummy-Order Replay</span>
            <div class="sr-header-pills">
                <span class="sr-count-pill" id="sim-status-pill">Idle</span>
            </div>
        </div>
        <div class="iso-card-body">
            <div class="sim-run-row">
                <button type="button" class="iso-btn primary" id="sim-run-btn">
                    <i class="bi bi-play-circle"></i> Run Simulation
                </button>
                <span class="sim-hint" id="sim-hint">
                    Replays the day's intraday candles from the entry fill forward.
                    Stateless — safe to click repeatedly.
                </span>
            </div>
            <div class="sim-regime-banner" id="sim-regime-banner" style="display: none;"></div>
            <div id="sim-body">
                <div class="sr-empty-state">
                    <i class="bi bi-calculator"></i>
                    Click <strong>Run Simulation</strong> to replay the entry
                    against the day's intraday candles.
                </div>
            </div>
        </div>
    </div>

    <!-- ============================================================ -->
    <!-- Dummy Orders card — live-tracked, persisted to disk          -->
    <!-- ============================================================ -->
    <div class="iso-card" id="iso-dummy-orders" style="display: none;">
        <div class="iso-card-header">
            <i class="bi bi-clipboard-data text-primary"></i>
            <span>Dummy Orders · Live Tracking</span>
            <div class="sr-header-pills">
                <span class="sr-count-pill" id="do-status-pill">Loading…</span>
            </div>
        </div>
        <div class="iso-card-body">
            <div class="sim-verdict-winner" id="do-lockdown-banner" style="display:none;">
                🔒 <strong>STRATEGY LOCKED.</strong>
                A Path B trade has reached its target. No new SR orders
                will be evaluated for the rest of the day. Existing open
                positions continue trailing until they exit.
            </div>
            <div class="do-summary-strip" id="do-summary-strip"></div>
            <div id="do-body">
                <div class="do-empty">
                    <i class="bi bi-hourglass-split"></i>
                    Dummy orders appear here as soon as the entry signal arms.
                </div>
            </div>
        </div>
    </div>

    <!-- Instrument snapshot -->
    <div class="iso-card" id="iso-instrument-snapshot" style="display: none;">
        <div class="iso-card-header">
            <i class="bi bi-graph-up text-primary"></i>
            <span id="snapshot-card-title">Isolated instrument · own data at touch</span>
        </div>
        <div class="iso-card-body">
            <div class="snapshot-tiles" id="snapshot-tiles"></div>
            <div id="snapshot-or-wrap" style="display: none;">
                <div class="snapshot-section-title">
                    Instrument's own first-candle OR levels
                </div>
                <div id="snapshot-or-grid"></div>
            </div>
        </div>
    </div>

    <!-- Reasoning card -->
    <div class="iso-card" id="iso-reasoning" style="display: none;">
        <div class="iso-card-header">
            <i class="bi bi-diagram-3-fill text-primary"></i>
            <span>How this instrument was isolated</span>
        </div>
        <div class="iso-card-body">
            <div class="reasoning-steps" id="iso-reasoning-body"></div>
        </div>
    </div>

    <!-- Two-column: candidates + touch state -->
    <div class="iso-row">
        <div class="iso-card">
            <div class="iso-card-header">
                <i class="bi bi-list-ol text-primary"></i>
                <span id="candidates-title">Candidates considered</span>
            </div>
            <div class="iso-card-body">
                <div class="iso-table-wrap">
                    <table class="iso-table" id="candidates-table">
                        <thead>
                            <tr>
                                <th>Side</th>
                                <th>Strike</th>
                                <th>Type</th>
                                <th>Levels</th>
                                <th class="num">Distance</th>
                                <th></th>
                            </tr>
                        </thead>
                        <tbody id="candidates-body">
                            <tr><td colspan="6" class="iso-empty">—</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <div class="iso-card">
            <div class="iso-card-header">
                <i class="bi bi-clipboard-check text-primary"></i>
                <span>Multi-index touch state</span>
                <div class="sr-header-pills">
                    <span class="sr-count-pill" id="touch-state-pill">0 / 8</span>
                </div>
            </div>
            <div class="iso-card-body">
                <div id="touch-body">
                    <div class="iso-empty">—</div>
                </div>
            </div>
        </div>
    </div>

    <!-- Event log -->
    <div class="iso-card">
        <div class="iso-card-header">
            <i class="bi bi-journal-text text-primary"></i>
            <span>Event log</span>
        </div>
        <div class="iso-card-body">
            <div class="iso-table-wrap">
                <table class="iso-table" id="events-table">
                    <thead>
                        <tr>
                            <th>Time (IST)</th>
                            <th>Index</th>
                            <th>Side</th>
                            <th class="num">Priority</th>
                            <th class="num">Trigger LTP</th>
                            <th class="num">Level</th>
                            <th>Status</th>
                            <th>Instrument</th>
                        </tr>
                    </thead>
                    <tbody id="events-body">
                        <tr><td colspan="8" class="iso-empty">—</td></tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- Index OR levels reference -->
    <div class="iso-card" id="or-levels-card" style="display: none;">
        <div class="iso-card-header">
            <i class="bi bi-bar-chart-line text-primary"></i>
            <span>Monitored index Opening Range levels</span>
        </div>
        <div class="iso-card-body">
            <div class="or-grid" id="or-levels-body"></div>
        </div>
    </div>
</div>
{% endblock %}

{% block scripts %}
<script>
    /* ============================================================
       G1 v3.2 — multi-index boundary surveillance + NIFTY routing
       ============================================================ */
    const DETAIL_URL = "/admin/level-isolation/detail";
    const BACKFILL_URL = "/admin/level-isolation/backfill";
    const REFRESH_SNAPSHOT_URL = "/admin/level-isolation/refresh-snapshot";
    const APPLY_PROMOTION_URL = "/admin/level-isolation/apply-promotion";
    const SEARCH_INSTRUMENTS_URL = "/admin/level-isolation/search-instruments";
    const INSTRUMENT_SR_URL = (key) =>
        "/admin/level-isolation/instrument-sr/" + encodeURIComponent(key);
    const ENTRY_SIGNAL_URL = "/admin/level-isolation/strategy-signal";
    const POLL_MS = 10000;
    const ENTRY_POLL_MS = 10000;

    /* Canonical index instrument keys — G1 v3.2 four-index universe. */
    const INDEX_KEYS = {
        MIDCPNIFTY:  "NSE_INDEX|NIFTY MID SELECT",
        BANKNIFTY:   "NSE_INDEX|Nifty Bank",
        NIFTY:       "NSE_INDEX|Nifty 50",
        SENSEX:      "BSE_INDEX|SENSEX",
    };

    /* Canonical name → instrument key. */
    const CANONICAL_TO_KEY = {
        MIDCPNIFTY:  INDEX_KEYS.MIDCPNIFTY,
        BANKNIFTY:   INDEX_KEYS.BANKNIFTY,
        NIFTY:       INDEX_KEYS.NIFTY,
        SENSEX:      INDEX_KEYS.SENSEX,
    };

    /* Ordered list — also the priority display order (v4.0 cross-index
       lead-lag: MIDCPNIFTY and BANKNIFTY lead, NIFTY is routed execution,
       SENSEX is the BSE anchor). */
    const INDEX_ORDER = ["MIDCPNIFTY", "BANKNIFTY", "NIFTY", "SENSEX"];

    /* Default priority ranks (overridden from server payload when present). */
    const DEFAULT_PRIORITY = {
        MIDCPNIFTY: 1,
        BANKNIFTY: 2,
        NIFTY: 3,
        SENSEX: 4,
    };

    /* Live-feed WebSocket — dedicated /list-feeds connection. */
    const LIVE_WS_URL = (seedKeys) => {
        const proto = location.protocol === "https:" ? "wss:" : "ws:";
        const base = `${proto}//${location.host}/list-feeds`;
        const fallback = INDEX_ORDER.map(n => CANONICAL_TO_KEY[n]);
        const keys = (Array.isArray(seedKeys) && seedKeys.length)
            ? seedKeys
            : fallback;
        return `${base}?instruments=${encodeURIComponent(keys.join(","))}`;
    };
    const LIVE_RECONNECT_MS = 4000;

    const state = {
        detail: null,
        pollTimer: null,
        entryPollTimer: null,
        promoInFlight: false,
        seenAllKeys: new Set(),
    };

    const compareState = {
        query: "",
        results: [],
        selectedKey: null,
        selected: null,
        searchTimer: null,
        inflight: null,
    };

    /* ============================================================
       Live-feed state
       ============================================================ */
    const liveFeedState = {
        ws: null,
        connected: false,
        reconnectTimer: null,
        subscribedKeys: new Set(),
        ticks: {},
        activeInstrumentKey: null,
        activeInstrumentSymbol: null,
        triggerIndexKey: null,
    };

    let liveRenderPending = false;
    function scheduleLiveRender() {
        if (liveRenderPending) return;
        liveRenderPending = true;
        window.requestAnimationFrame(() => {
            liveRenderPending = false;
            renderLiveFeed();
        });
    }

    /* ---------- small formatters ---------- */
    function parseNum(v) {
        if (v === null || v === undefined || v === "") return null;
        const n = Number(v);
        return Number.isFinite(n) ? n : null;
    }

    function fmt(v, d) {
        const n = parseNum(v);
        if (n === null) return "—";
        return n.toFixed(d === undefined ? 2 : d);
    }

    function fmtInt(v) {
        const n = parseNum(v);
        return n === null ? "0" : Math.round(n).toLocaleString();
    }

    function fmtRs(v) {
        const n = parseNum(v);
        if (n === null) return "—";
        const sign = n >= 0 ? "+" : "";
        return `${sign}₹${n.toFixed(2)}`;
    }

    function esc(s) {
        return String(s === null || s === undefined ? "" : s).replace(
            /[&<>"']/g,
            c => ({
                "&": "&amp;",
                "<": "&lt;",
                ">": "&gt;",
                '"': "&quot;",
                "'": "&#39;",
            }[c])
        );
    }

    function formatTime(iso) {
        if (!iso) return "—";
        try {
            const d = new Date(iso);
            if (isNaN(d.getTime())) return String(iso);
            return d.toLocaleTimeString("en-IN", {
                timeZone: "Asia/Kolkata",
                hour: "2-digit",
                minute: "2-digit",
                second: "2-digit",
                hour12: false,
            });
        } catch (_) {
            return String(iso);
        }
    }

    function formatExpiry(val) {
        if (val === null || val === undefined || val === "") return "—";
        const s = String(val).trim();

        const isoMatch = s.match(/^(\d{4})-(\d{2})-(\d{2})/);
        if (isoMatch) {
            const d = new Date(Date.UTC(
                Number(isoMatch[1]),
                Number(isoMatch[2]) - 1,
                Number(isoMatch[3]),
            ));
            if (!isNaN(d.getTime())) return formatExpiryDate(d);
            return s;
        }

        const num = Number(s);
        if (Number.isFinite(num) && num > 1e9) {
            const ms = num > 1e12 ? num : num * 1000;
            const d = new Date(ms);
            if (!isNaN(d.getTime())) return formatExpiryDate(d);
        }
        return s;
    }

    function formatExpiryDate(d) {
        try {
            return new Intl.DateTimeFormat("en-IN", {
                timeZone: "Asia/Kolkata",
                day: "2-digit",
                month: "short",
                year: "2-digit",
            }).format(d).toUpperCase();
        } catch (_) {
            return d.toISOString().slice(0, 10);
        }
    }

    /* ---------- G1 v3.2 reason labels (4 indices) ---------- */
    function reasonLabel(reason) {
        const map = {
            nifty_r3_touch:       "NIFTY touched R3",
            nifty_s3_touch:       "NIFTY touched S3",
            banknifty_r3_touch:   "BANKNIFTY touched R3",
            banknifty_s3_touch:   "BANKNIFTY touched S3",
            midcpnifty_r3_touch:  "MIDCPNIFTY touched R3",
            midcpnifty_s3_touch:  "MIDCPNIFTY touched S3",
            sensex_r3_touch:      "SENSEX touched R3",
            sensex_s3_touch:      "SENSEX touched S3",
            sensex_r3_with_nifty_reference: "SENSEX touched R3 · NIFTY reference",
            sensex_s3_with_nifty_reference: "SENSEX touched S3 · NIFTY reference",
            sensex_r3_fallback: "SENSEX touched R3 · fallback (NIFTY stale)",
            sensex_s3_fallback: "SENSEX touched S3 · fallback (NIFTY stale)",
        };
        return map[reason] || String(reason || "—");
    }

    function quoteSourceLabel(src) {
        const map = {
            live_feed_cache: "live tick",
            intraday_bar: "touch minute bar",
            intraday_latest_bar: "latest intraday bar",
            first_candle_close: "first-candle close",
            or_midpoint: "OR midpoint",
            entry_ltp_fallback: "entry LTP fallback",
            confirmation_bar_close: "confirmation bar close",
            candle_close_at_confirmed_fast: "candle close @ fast confirm",
            candle_close_at_confirmed: "candle close @ confirm",
            counter_candle_at_rejection: "counter candle @ rejection",
            live_ltp: "live LTP",
            trigger_ltp_fallback: "trigger LTP fallback",
            unavailable: "—",
        };
        return map[src] || String(src || "—");
    }

    function snapshotAvailability(d) {
        const iso = d && d.isolated;
        if (!iso) {
            return {
                hasAny: false, needsRefresh: false,
                hasLtp: false, hasTouch: false, hasFc: false, hasOr: false,
            };
        }
        const ltp = parseNum(iso.instrument_ltp);
        const hasLtp = ltp !== null;
        const hasTouch = Object.keys(iso.instrument_touch_ohlc || {}).length > 0;
        const hasFc = Object.keys(iso.instrument_first_candle || {}).length > 0;
        const hasOr = Object.keys(iso.instrument_or_levels || {}).length > 0;
        const hasAny = hasLtp || hasTouch || hasFc || hasOr;
        const needsRefresh = !hasLtp || !hasFc;
        return { hasAny, needsRefresh, hasLtp, hasTouch, hasFc, hasOr };
    }

    function isSimulateMode(detail) {
        if (!detail) return false;
        const iso = detail.isolated;
        return !!(iso && iso.source === "backfill");
    }

    /* ============================================================
       Compare-in-Layout URL builder (G1 v3.2: routed index + iso)
       ============================================================ */
    function buildLayoutCompareUrl(iso) {
        if (!iso || !iso.instrument_key) return null;

        let indexKey = "";
        const routed = String(iso.routed_index || "").toUpperCase();
        if (routed && CANONICAL_TO_KEY[routed]) {
            indexKey = CANONICAL_TO_KEY[routed];
        } else {
            const trig = String(iso.trigger_index || "").toUpperCase();
            if (trig && CANONICAL_TO_KEY[trig]) {
                indexKey = CANONICAL_TO_KEY[trig];
            }
        }

        const params = new URLSearchParams();
        if (indexKey) params.append("panes", indexKey);
        params.append("panes", iso.instrument_key);
        return "/layout_1?" + params.toString();
    }

    /* ============================================================
       Live Feed
       ============================================================ */
    function computeWantedLiveKeys() {
        const wanted = new Set(INDEX_ORDER.map(n => CANONICAL_TO_KEY[n]));
        const d = state.detail;
        if (d && d.isolated) {
            const isPromoted = !!(d.active && d.active.record);
            const iso = isPromoted ? d.active.record : d.isolated;
            const key = iso.instrument_key;
            if (key) wanted.add(key);
        }
        return wanted;
    }

    function setLiveFeedStatus(st) {
        const pill = document.getElementById("live-feed-status-pill");
        if (!pill) return;
        if (st === "connected") {
            pill.textContent = "Live";
            pill.className = "sr-count-pill has";
            pill.style.background = "";
            pill.style.color = "";
        } else if (st === "connecting") {
            pill.textContent = "Connecting…";
            pill.className = "sr-count-pill";
            pill.style.background = "";
            pill.style.color = "";
        } else {
            pill.textContent = "Disconnected";
            pill.className = "sr-count-pill";
            pill.style.background = "var(--iso-red-bg)";
            pill.style.color = "var(--iso-red)";
        }
    }

    function updateLiveSubCountPill() {
        const pill = document.getElementById("live-feed-sub-count-pill");
        if (!pill) return;
        const n = liveFeedState.subscribedKeys.size;
        pill.textContent = `${n} subscribed`;
    }

    function sendLiveSubscriptionSet(keys) {
        if (!liveFeedState.ws || liveFeedState.ws.readyState !== WebSocket.OPEN) {
            return false;
        }
        try {
            liveFeedState.ws.send(JSON.stringify({
                action: "set",
                instrument_keys: keys,
            }));
            liveFeedState.subscribedKeys = new Set(keys);
            updateLiveSubCountPill();
            return true;
        } catch (e) {
            console.warn("[live-feed] send set failed:", e);
            return false;
        }
    }

    function connectLiveFeed() {
        if (liveFeedState.ws &&
            (liveFeedState.ws.readyState === WebSocket.OPEN ||
             liveFeedState.ws.readyState === WebSocket.CONNECTING)) {
            return;
        }
        setLiveFeedStatus("connecting");

        let ws;
        try {
            const seed = Array.from(computeWantedLiveKeys());
            ws = new WebSocket(LIVE_WS_URL(seed));
        } catch (e) {
            console.warn("[live-feed] WebSocket ctor failed:", e);
            scheduleLiveFeedReconnect();
            return;
        }
        liveFeedState.ws = ws;

        ws.onopen = () => {
            liveFeedState.connected = true;
            setLiveFeedStatus("connected");
            const wanted = computeWantedLiveKeys();
            sendLiveSubscriptionSet(Array.from(wanted));
        };

        ws.onmessage = (event) => {
            let data;
            try {
                data = JSON.parse(event.data);
            } catch (_) {
                return;
            }
            handleLiveMessage(data);
        };

        ws.onclose = () => {
            liveFeedState.connected = false;
            liveFeedState.ws = null;
            setLiveFeedStatus("disconnected");
            scheduleLiveFeedReconnect();
        };

        ws.onerror = () => {};
    }

    function scheduleLiveFeedReconnect() {
        if (liveFeedState.reconnectTimer) return;
        liveFeedState.reconnectTimer = setTimeout(() => {
            liveFeedState.reconnectTimer = null;
            connectLiveFeed();
        }, LIVE_RECONNECT_MS);
    }

    function syncLiveSubscriptions() {
        const d = state.detail;
        const card = document.getElementById("iso-live-feed");
        const titleEl = document.getElementById("live-feed-card-title");
        if (!card) return;

        if (!d) {
            card.style.display = "none";
            return;
        }

        const wanted = computeWantedLiveKeys();

        const iso = (d.isolated && d.active && d.active.record)
            ? d.active.record
            : d.isolated;
        liveFeedState.activeInstrumentKey = iso && iso.instrument_key
            ? iso.instrument_key
            : null;
        liveFeedState.activeInstrumentSymbol = iso && iso.trading_symbol
            ? iso.trading_symbol
            : liveFeedState.activeInstrumentKey;

        const triggerCanonical = iso && iso.trigger_index
            ? String(iso.trigger_index).toUpperCase()
            : null;
        liveFeedState.triggerIndexKey = triggerCanonical
            ? (CANONICAL_TO_KEY[triggerCanonical] || null)
            : null;

        const sameSet =
            wanted.size === liveFeedState.subscribedKeys.size &&
            [...wanted].every(k => liveFeedState.subscribedKeys.has(k));
        if (!sameSet) {
            sendLiveSubscriptionSet(Array.from(wanted));
        }

        card.style.display = "block";
        if (titleEl) {
            if (d.isolated) {
                const isPromoted = !!(d.active && d.active.record);
                const suffix = isPromoted ? " (promoted)" : "";
                const simulateSuffix = isSimulateMode(d) ? " · SIMULATE" : "";
                titleEl.textContent =
                    "Live Feed · " + (liveFeedState.activeInstrumentSymbol || "Active Instrument") +
                    suffix + simulateSuffix;
            } else {
                titleEl.textContent = "Live Feed · Watching 4 indices";
            }
        }

        renderLiveFeed();
    }

    function handleLiveMessage(data) {
        if (!data || typeof data !== "object") return;

        const type = data.type;

        if (type === "subscribed" ||
            type === "subscriptions_replaced" ||
            type === "subscribed_list") {
            if (Array.isArray(data.instrument_keys)) {
                liveFeedState.subscribedKeys = new Set(data.instrument_keys);
                updateLiveSubCountPill();
            }
            return;
        }
        if (type === "unsubscribed") {
            if (Array.isArray(data.instrument_keys)) {
                for (const k of data.instrument_keys) {
                    liveFeedState.subscribedKeys.delete(k);
                }
                updateLiveSubCountPill();
            }
            return;
        }
        if (type === "subscriptions_cleared") {
            liveFeedState.subscribedKeys = new Set();
            updateLiveSubCountPill();
            return;
        }
        if (type === "error") {
            console.warn("[live-feed] server error:", data.error);
            return;
        }
        if (type === "ping" || type === "connected") return;

        const key = data.instrument_key;
        if (!key) return;

        const prev = liveFeedState.ticks[key] || {};
        const ltp = parseNum(data.ltp);
        const prevClose = parseNum(data.prev_close) ?? prev.prev_close ?? null;

        let change = parseNum(data.change);
        let changePct = parseNum(data.change_pct);
        if (change === null && ltp !== null && prevClose) {
            change = ltp - prevClose;
        }
        if (changePct === null && change !== null && prevClose) {
            changePct = (change / prevClose) * 100;
        }

        liveFeedState.ticks[key] = {
            ltp,
            prev_close: prevClose,
            change,
            change_pct: changePct,
            volume: parseNum(data.volume),
            timestamp: data.timestamp || new Date().toISOString(),
        };

        scheduleLiveRender();
    }

    function renderLiveTile({
        label,
        labelIcon,
        value,
        valueClass,
        sub,
        subClass,
        primary,
        trigger,
    }) {
        const tileCls =
            "live-tile" +
            (primary ? " live-tile-primary" : "") +
            (trigger ? " live-tile-trigger" : "");
        return `
            <div class="${tileCls}">
                <div class="live-tile-label">
                    ${labelIcon ? `<i class="bi ${labelIcon}"></i>` : ""}
                    ${esc(label)}
                </div>
                <div class="live-tile-value ${valueClass || ""}">${value}</div>
                ${sub ? `<div class="live-tile-sub ${subClass || ""}">${sub}</div>` : ""}
            </div>`;
    }

    function renderLiveFeed() {
        const card = document.getElementById("iso-live-feed");
        const tilesEl = document.getElementById("live-feed-tiles");
        const metaEl = document.getElementById("live-feed-meta");
        if (!card || !tilesEl) return;

        const d = state.detail;
        const hasIsolation = !!(d && d.isolated);

        let html = "";

        if (hasIsolation && liveFeedState.activeInstrumentKey) {
            const key = liveFeedState.activeInstrumentKey;
            const tick = liveFeedState.ticks[key] || {};
            const ltp = parseNum(tick.ltp);
            const change = parseNum(tick.change);
            const changePct = parseNum(tick.change_pct);
            const valueCls = change === null ? "" : (change >= 0 ? "up" : "down");
            const changePrefix = (change !== null && change >= 0) ? "+" : "";

            html += renderLiveTile({
                label: liveFeedState.activeInstrumentSymbol || key,
                labelIcon: "bi-lightning-charge-fill",
                value: ltp !== null ? `₹${fmt(ltp)}` : "—",
                valueClass: valueCls,
                sub: change !== null
                    ? `${changePrefix}${fmt(change)}${changePct !== null ? ` (${fmt(changePct)}%)` : ""}`
                    : "waiting for tick…",
                subClass: valueCls,
                primary: true,
            });

            if (tick.prev_close !== null && tick.prev_close !== undefined) {
                html += renderLiveTile({
                    label: "Prev Close",
                    value: `₹${fmt(tick.prev_close)}`,
                });
            }

            if (tick.volume !== null && tick.volume !== undefined) {
                html += renderLiveTile({
                    label: "Volume",
                    labelIcon: "bi-bar-chart-line",
                    value: fmtInt(tick.volume),
                });
            }
        }

        const indexDefs = [
            { name: "NIFTY 50",    key: CANONICAL_TO_KEY.NIFTY },
            { name: "BANKNIFTY",   key: CANONICAL_TO_KEY.BANKNIFTY },
            { name: "MIDCPNIFTY",  key: CANONICAL_TO_KEY.MIDCPNIFTY },
            { name: "SENSEX",      key: CANONICAL_TO_KEY.SENSEX },
        ];
        for (const def of indexDefs) {
            const tick = liveFeedState.ticks[def.key] || {};
            const ltp = parseNum(tick.ltp);
            const change = parseNum(tick.change);
            const changePct = parseNum(tick.change_pct);
            const valueCls = change === null ? "" : (change >= 0 ? "up" : "down");
            const changePrefix = (change !== null && change >= 0) ? "+" : "";
            const isTrigger = liveFeedState.triggerIndexKey === def.key;
            const triggerSuffix = isTrigger ? " · trigger" : "";

            html += renderLiveTile({
                label: def.name + triggerSuffix,
                labelIcon: isTrigger ? "bi-lightning-charge-fill" : "bi-graph-up",
                value: ltp !== null ? fmt(ltp) : "—",
                valueClass: valueCls,
                sub: change !== null
                    ? `${changePrefix}${fmt(change)}${changePct !== null ? ` (${fmt(changePct)}%)` : ""}`
                    : "waiting for tick…",
                subClass: valueCls,
                trigger: isTrigger,
            });
        }

        tilesEl.innerHTML = html;

        if (metaEl) {
            if (hasIsolation && liveFeedState.activeInstrumentKey) {
                const primary = liveFeedState.ticks[liveFeedState.activeInstrumentKey];
                if (primary && primary.timestamp) {
                    metaEl.textContent =
                        `Last update: ${formatTime(primary.timestamp)} · live via WebSocket`;
                } else {
                    metaEl.textContent = "Waiting for first tick on the isolated instrument…";
                }
            } else {
                metaEl.textContent =
                    "Watching 4 indices live. An isolated NIFTY strike will be added to the feed automatically.";
            }
        }
    }

    /* ---------- renderers ---------- */
    function renderStatusPill(d) {
        const pill = document.getElementById("status-pill");
        const simPill = document.getElementById("simulate-pill");
        if (!pill) return;

        const dayLabel = document.getElementById("day-label");
        if (dayLabel) {
            const day = d.day || "";
            const mkt = d.market || {};
            dayLabel.textContent = day
                ? `${day} · ${mkt.state || ""}`
                : "—";
        }

        const simulate = isSimulateMode(d);
        if (simPill) {
            simPill.style.display = simulate ? "inline-flex" : "none";
        }

        if (d.isolated) {
            if (simulate) {
                pill.className = "iso-status-pill simulate";
                pill.textContent = "Isolated · Simulate";
            } else {
                pill.className = "iso-status-pill isolated";
                pill.textContent = "Isolated";
            }
        } else if (!d.enabled) {
            pill.className = "iso-status-pill off";
            pill.textContent = "Disabled";
        } else {
            const mktState = (d.market || {}).state;
            if (mktState === "in_session" && (d.market || {}).or_ready) {
                pill.className = "iso-status-pill watching";
                pill.textContent = "Watching 4 indices";
            } else {
                pill.className = "iso-status-pill waiting";
                pill.textContent = "Waiting";
            }
        }
    }

    function renderBanner(d) {
        const banner = document.getElementById("iso-banner");
        if (!banner) return;

        if (d.isolated) {
            banner.style.display = "none";
            return;
        }

        const mkt = d.market || {};
        const mktState = mkt.state || "in_session";
        const isWatching = mktState === "in_session" && mkt.or_ready;
        const simulateAvailable = !!mkt.simulate_mode;

        banner.style.display = "flex";
        banner.className =
            "iso-banner" +
            (isWatching ? " is-watching" : "") +
            (simulateAvailable ? " is-simulate" : "");

        const iconEl = document.getElementById("banner-icon");
        const titleEl = document.getElementById("banner-title");
        const msgEl = document.getElementById("banner-message");
        const subEl = document.getElementById("banner-subtext");

        let icon = "bi-hourglass-split";
        let title = "Not isolated yet";
        let sub = "";

        switch (mktState) {
            case "weekend":
                icon = "bi-calendar-x"; title = "Weekend — market closed";
                sub = "Isolation resumes on the next trading day.";
                break;
            case "holiday":
                icon = "bi-calendar-x"; title = "Market holiday";
                sub = mkt.holiday_description
                    ? `Reason: ${mkt.holiday_description}` : "No trading today.";
                break;
            case "pre_open":
                icon = "bi-alarm"; title = "Pre-open — waiting for market open";
                sub = "OR levels will be computed at 09:17:10 IST; isolation will begin watching all 4 indices for R3 / S3 touches right after.";
                break;
            case "or_warming_up":
                icon = "bi-lightning-charge"; title = "OR levels being computed";
                sub = "First candle closed at 09:16 — isolation will start watching NIFTY / BANKNIFTY / MIDCPNIFTY / SENSEX for R3 / S3 touches in a moment.";
                break;
            case "post_close":
                if (simulateAvailable) {
                    icon = "bi-arrow-clockwise";
                    title = "Market closed · SIMULATE MODE available";
                    sub = "Click Run Backfill to reconstruct today's would-have-isolated NIFTY strike from the day's intraday candles.";
                } else {
                    icon = "bi-moon-stars";
                    title = "Market closed";
                    sub = "No new isolation until the next trading session.";
                }
                break;
            case "in_session":
                if (isWatching) {
                    icon = "bi-bullseye"; title = "Watching 4 indices for R3 / S3 touches";
                    sub = "Priority: NIFTY > BANKNIFTY > MIDCPNIFTY > SENSEX. Every touch routes to a NIFTY strike.";
                } else {
                    icon = "bi-hourglass-split"; title = "Not isolated yet";
                }
                break;
            default: break;
        }

        if (iconEl) iconEl.innerHTML = `<i class="bi ${icon}"></i>`;
        if (titleEl) titleEl.textContent = title;
        if (msgEl) msgEl.textContent = mkt.message || "";
        if (subEl) subEl.textContent = sub;

        const backfillBtn = document.getElementById("backfill-btn");
        if (backfillBtn) {
            const canBackfill = (isWatching || simulateAvailable) && !d.isolated;
            backfillBtn.style.display = canBackfill ? "inline-flex" : "none";
            backfillBtn.className = canBackfill
                ? "iso-btn simulate"
                : "iso-btn";
        }
    }

    function renderRefreshSnapshotBtn(d) {
        const btn = document.getElementById("refresh-snapshot-btn");
        if (!btn) return;
        const avail = snapshotAvailability(d);
        btn.style.display = avail.needsRefresh ? "inline-flex" : "none";
    }

    /* ---------- HERO ---------- */
    function renderHero(d) {
        const hero = document.getElementById("iso-hero");
        if (!hero) return;

        if (!d.isolated) {
            hero.style.display = "none";
            return;
        }

        hero.style.display = "flex";

        const active = d.active;
        const isPromoted = !!(active && active.record);
        const iso = isPromoted ? active.record : d.isolated;
        const ev = d.trigger_event || {};
        const simulate = isSimulateMode(d);

        hero.classList.toggle("is-simulate", simulate);

        document.getElementById("hero-symbol").textContent =
            iso.trading_symbol || iso.instrument_key || "—";
        document.getElementById("hero-key").textContent =
            iso.instrument_key || "";

        const tagsEl = document.getElementById("hero-tags");
        tagsEl.innerHTML = "";
        const tags = [];
        if (simulate) {
            tags.push({ cls: "sim", text: "SIMULATE" });
        }
        if (iso.type) {
            tags.push({
                cls: iso.type === "CE" ? "s" : "r",
                text: `${iso.type}`,
            });
        }
        if (iso.strike !== null && iso.strike !== undefined) {
            tags.push({ cls: "th", text: `Strike ${fmt(iso.strike, 0)}` });
        }
        (iso.levels || []).forEach(lv => {
            const cls = String(lv).startsWith("R")
                ? "r"
                : String(lv).startsWith("S")
                    ? "s"
                    : "th";
            tags.push({ cls, text: lv });
        });
        if (isPromoted) {
            tags.push({ cls: "promo", text: "PROMOTED" });
        }
        const path = String(
            (iso && iso.execution_path)
            || (d && d.execution_path)
            || ""
        );
        if (path === "B_external_trigger") {
            tags.push({
                cls: "th",
                text: "PATH B · MULTI-ORDER",
            });
        }
        tagsEl.innerHTML = tags
            .map(t => `<span class="tag ${t.cls}">${esc(t.text)}</span>`)
            .join("");

        const metaEl = document.getElementById("hero-meta");
        const originalReason = (d.isolated || {}).reason;
        const triggerIdx = iso.trigger_index || "—";
        const triggerPriority = iso.trigger_priority;
        const routedIdx = iso.routed_index || "NIFTY";
        const routedLtp = iso.routed_nifty_ltp;

        const triggerLine =
            triggerIdx + " " + (iso.trigger_side || "—") +
            (triggerPriority ? ` · P${triggerPriority}` : "");
        const routedLine =
            routedIdx + (routedLtp != null ? ` @ ${fmt(routedLtp)}` : "");

        const metaItems = [
            {
                label: isPromoted ? "Trigger · promoted" : "Trigger",
                value: isPromoted
                    ? `${reasonLabel(originalReason)} · promoted`
                    : triggerLine,
            },
            {
                label: "Routed To",
                value: routedLine,
                highlight: !simulate,
            },
        ];
        if (isPromoted) {
            metaItems.push({
                label: "Promoted From",
                value:
                    active.from_trading_symbol
                    || active.from_instrument_key
                    || "—",
            });
        }
        metaItems.push(
            { label: "Isolated At", value: formatTime(iso.isolated_at) },
            { label: "Expiry", value: formatExpiry(iso.expiry) },
            {
                label: "Trigger Level",
                value: `${iso.trigger_level || "—"} = ${fmt(iso.trigger_level_value)}`,
            },
            { label: "Touched At", value: formatTime(iso.touched_at) },
        );

        const alsoTouched = iso.simultaneous_touches || [];
        if (alsoTouched.length) {
            metaItems.push({
                label: "Also Touched",
                value: alsoTouched
                    .map(t => `${t.canonical || t.index} ${t.side || ""}`.trim())
                    .join(", "),
                highlight: false,
            });
        }

        if (simulate) {
            metaItems.push({
                label: "Source",
                value: "SIMULATE — post-close replay",
                simulate: true,
            });
        }

        metaEl.innerHTML = metaItems
            .map(m => {
                let cls = "meta-value";
                if (m.simulate) cls += " sim";
                else if (m.highlight) cls += " highlight";
                return (
                    `<div><span class="meta-label">${esc(m.label)}</span>` +
                    `<span class="${cls}">${esc(m.value)}</span></div>`
                );
            })
            .join("");

        const chartBtn = document.getElementById("hero-chart-btn");
        if (chartBtn && iso.instrument_key) {
            chartBtn.href = "/chart/" + encodeURIComponent(iso.instrument_key);
            chartBtn.target = "_blank";
            chartBtn.rel = "noopener noreferrer";
        }

        const layoutBtn = document.getElementById("hero-layout-btn");
        if (layoutBtn) {
            const url = buildLayoutCompareUrl(iso);
            if (url) {
                layoutBtn.href = url;
                layoutBtn.classList.remove("is-hidden");
                layoutBtn.title =
                    "Open " +
                    routedIdx +
                    " · " +
                    (iso.trading_symbol || iso.instrument_key) +
                    " side-by-side in a new tab";
            } else {
                layoutBtn.removeAttribute("href");
                layoutBtn.classList.add("is-hidden");
            }
        }

        const instrLtpEl = document.getElementById("hero-instrument-ltp");
        const instrSubEl = document.getElementById("hero-instrument-sub");
        const instrLtp = parseNum(iso.instrument_ltp);
        if (instrLtpEl) {
            instrLtpEl.textContent = instrLtp !== null ? `₹${fmt(instrLtp)}` : "—";
        }
        if (instrSubEl) {
            instrSubEl.textContent = iso.instrument_quote_source
                ? `source: ${quoteSourceLabel(iso.instrument_quote_source)}`
                : "—";
        }

        document.getElementById("hero-trigger-ltp").textContent =
            fmt(ev.trigger_ltp);
        document.getElementById("hero-trigger-sub").textContent =
            ev.index && ev.side
                ? `${ev.index} · ${ev.side}`
                : "—";
    }

    /* ---------- Entry Signal ---------- */
    function renderEntryTouchStrip(payload) {
        const el = document.getElementById("entry-touch-strip");
        if (!el) return;
        const t = payload.touch || {};
        const iso = payload.isolated || {};
        const simulate = iso.source === "backfill";

        const srcTag = simulate
            ? ' <strong style="color:var(--iso-simulate)">· SIMULATE</strong>'
            : "";

        el.innerHTML =
            '<span>Trigger: <strong>' +
                esc(iso.trigger_index || "—") + " " + esc(t.side || "—") +
                srcTag +
            '</strong></span>' +
            '<span>Bias: <strong>' + esc(t.bias_type || "—") + '</strong></span>' +
            '<span>Routed to: <strong>' +
                esc(iso.routed_index || "NIFTY") +
                (iso.routed_nifty_ltp != null
                    ? " @ " + fmt(iso.routed_nifty_ltp) : "") +
            '</strong></span>' +
            '<span>Touch: <strong>' + esc(formatTime(t.time)) + '</strong></span>' +
            '<span>Window: <strong>' +
                esc(formatTime(t.window_start)) + ' → ' +
                esc(formatTime(t.window_end)) +
            '</strong> (' + esc(t.buffer_minutes != null ? t.buffer_minutes : "—") +
            'm)</span>';
    }

    function renderPromotionBanner(payload) {
        const banner = document.getElementById("entry-promotion-banner");
        if (!banner) return;

        const promo = (payload && payload.promotion) || {};
        const already = !!promo.already_applied;
        const recommended = !!promo.recommended;
        const targetKey = promo.target_instrument_key;
        const targetSym = promo.target_trading_symbol || targetKey;

        if (!already && !recommended) {
            banner.style.display = "none";
            banner.innerHTML = "";
            return;
        }

        banner.style.display = "flex";
        if (already) {
            banner.innerHTML =
                '<i class="bi bi-check-circle-fill"></i>' +
                '<div><strong>Instrument promoted.</strong> ' +
                'This <span class="promo-pending">' +
                esc(payload.chosen_side || "") +
                '</span> is now the <strong>active</strong> isolated instrument ' +
                '(' + esc(targetSym || "") + '). ' +
                'The SR tracker and snapshot below track it.</div>';
        } else {
            banner.innerHTML =
                '<i class="bi bi-arrow-up-circle-fill"></i>' +
                '<div><strong>Promoting…</strong> ' +
                'The entry signal armed on ' +
                '<span class="promo-pending">' +
                esc(payload.chosen_side || "") +
                '</span>. ' +
                'Promoting ' + esc(targetSym || "") +
                ' to be the active isolated instrument.</div>';
        }
    }

    function entryCandidateNote(c) {
        if (c.state === "waiting") {
            if (c.state_note === "no_support_pivot_in_window") {
                return "No support pivot inside the post-touch buffer yet.";
            }
            if (c.state_note === "no_sr_levels") {
                return "No live SR levels persisted yet for this instrument.";
            }
            return "";
        }
        if (c.state === "waiting_confirmation") {
            const es = c.entry_support || {};
            const br = c.blocking_resistance || {};
            if (es.price) {
                let note = "Support at " + fmt(es.price) +
                    " (pivot " + formatTime(es.pivot_time) + "). " +
                    "Fast confirms at " +
                    formatTime(es.confirmed_time_fast || es.confirmed_time) + ".";
                if (c.seconds_until_confirmation != null) {
                    const mins = Math.max(0, Math.round(c.seconds_until_confirmation / 60));
                    note += " (~" + mins + "m)";
                }
                return note;
            }
            if (br.price) {
                let note = "Resistance at " + fmt(br.price) +
                    " (pivot " + formatTime(br.pivot_time) + "). " +
                    "Fast confirms at " + formatTime(br.confirmed_time) + ".";
                if (c.seconds_until_confirmation != null) {
                    const mins = Math.max(0, Math.round(c.seconds_until_confirmation / 60));
                    note += " " + (c.state_note && c.state_note.includes("auto_reverse")
                        ? "(awaiting auto-reverse, ~" + mins + "m)"
                        : "(~" + mins + "m)");
                }
                return note;
            }
            return "";
        }
        if (c.state === "blocked") {
            const b = c.blocking_resistance || {};
            const n = c.state_note || "";
            if (n === "all_candidates_in_window_blocked") {
                return "Buffer window expired with no tradeable confirmation.";
            }
            if (n === "entry_below_support") {
                return "Entry price broke the support during confirmation wait.";
            }
            if (n === "invalid_entry_price") {
                return "Invalid entry price (≤0) — bad tick or dead instrument.";
            }
            if (n === "no_entry_price") {
                return "No entry price available (candle close and LTP both missing).";
            }
            if (n === "invalid_counter_price") {
                return "Auto-reverse failed — counter-side LTP unavailable.";
            }
            if (b.price) {
                return "Blocked by resistance " + fmt(b.price) +
                    " (confirmed " + formatTime(b.confirmed_time) + ").";
            }
            return n || "Blocked.";
        }
        if (c.state === "discarded") {
            const rt = c.reversal_trigger || {};
            if (c.state_note === "rejected_and_reversed" && rt.bias_resistance_price != null) {
                const rejSide = c.side || "?";
                const armSide = rejSide === "CE" ? "PE" : "CE";
                return "Bias " + rejSide + " rejected at ₹" +
                    fmt(rt.bias_resistance_price) +
                    " (confirmed " + formatTime(rt.bias_resistance_confirmed_time_fast) + ")" +
                    " — auto-reversed to " + armSide + ".";
            }
            return "Another side confirmed first — entry discarded for the day.";
        }
        if (c.state === "armed") {
            if (c.entry_type === "auto_reverse_on_rejection") {
                return "Auto-reverse fired: bias side rejected → counter side armed.";
            }
            return "Entry support confirmed. Place MARKET order for this side.";
        }
        if (c.state === "error") {
            return c.state_note || "Computation error.";
        }
        return "";
    }

    function entryCandidateGrid(c) {
        if (c.state !== "armed" || !c.entry) {
            if (c.entry_support) {
                const es = c.entry_support;
                return (
                    '<div class="entry-grid">' +
                        '<div class="label">Support price</div>' +
                        '<div class="value">' + fmt(es.price) + '</div>' +
                        '<div class="label">Pivot time</div>' +
                        '<div class="value">' + formatTime(es.pivot_time) + '</div>' +
                        '<div class="label">Fast confirm</div>' +
                        '<div class="value">' + formatTime(es.confirmed_time_fast || es.confirmed_time) + '</div>' +
                        '<div class="label">Display confirm</div>' +
                        '<div class="value">' + formatTime(es.confirmed_time) + '</div>' +
                    '</div>'
                );
            }
            return "";
        }

        const e = c.entry;
        const isReversal = c.entry_type === "auto_reverse_on_rejection";
        const rt = c.reversal_trigger || {};
        const isFrozen = !!e._frozen;

        const priceRowValue =
            fmt(e.price) +
            ' <small>(' + esc(quoteSourceLabel(e.price_source)) + ')</small>' +
            (isFrozen
                ? ' <small class="entry-frozen-mark" title="Frozen at arming time — G1 v4.1 §5.E">📌 pinned</small>'
                : '');

        const commonRows = [
            '<div class="label">Entry price</div>',
            '<div class="value">' + priceRowValue + '</div>',
            '<div class="label">Entry time</div>',
            '<div class="value">' + formatTime(e.time) + '</div>',
            '<div class="label">Stop-loss</div>',
            '<div class="value">' + fmt(e.stop) +
                (e.stop_source ? ' <small>(' + esc(e.stop_source) + ')</small>' : '') + '</div>',
            '<div class="label">Stop risk</div>',
            '<div class="value">' +
                (e.stop_pct != null ? (e.stop_pct * 100).toFixed(1) + '%' : "—") +
            '</div>',
            '<div class="label">Target</div>',
            '<div class="value">' +
                (e.target != null ? fmt(e.target) : "—") +
            '</div>',
        ];

        let extraRows = '';
        if (isReversal) {
            extraRows =
                '<div class="label">Rejected side</div>' +
                '<div class="value">' + esc(rt.rejected_side || "—") + '</div>' +
                '<div class="label">Rejected at</div>' +
                '<div class="value">₹' + fmt(rt.bias_resistance_price) + '</div>' +
                '<div class="label">Rejection time</div>' +
                '<div class="value">' + formatTime(rt.bias_resistance_confirmed_time_fast) + '</div>' +
                '<div class="label">Entry type</div>' +
                '<div class="value">auto-reverse</div>';
        } else if (c.entry_support) {
            const es = c.entry_support;
            extraRows =
                '<div class="label">Support price</div>' +
                '<div class="value">' + fmt(es.price) + '</div>' +
                '<div class="label">Support pivot</div>' +
                '<div class="value">' + formatTime(es.pivot_time) + '</div>' +
                '<div class="label">Fast confirm</div>' +
                '<div class="value">' + formatTime(es.confirmed_time_fast || es.confirmed_time) + '</div>' +
                '<div class="label">Display confirm</div>' +
                '<div class="value">' + formatTime(es.confirmed_time) + '</div>';
        }

        return '<div class="entry-grid">' + commonRows.join('') + extraRows + '</div>';
    }

    function renderEntryCandidate(c) {
        const side = c.side || "—";
        const typeCls = side === "CE" ? "type-ce" : "type-pe";
        const stateVal = c.state || "waiting";
        const stateCls = "state-" + stateVal;
        const isChosen = !!c.chosen;
        const isBias = !!c.is_bias_side;
        const isReversal = c.entry_type === "auto_reverse_on_rejection";
        const isFrozen = !!(c.entry && c.entry._frozen);

        const pills = [
            '<span class="entry-pill ' + typeCls + '">' + esc(side) + '</span>',
            isBias ? '<span class="entry-pill bias">BIAS</span>' : '',
            isFrozen ? '<span class="entry-pill frozen" title="Entry values frozen at arming time (G1 v4.1 §5.E)">📌 PINNED</span>' : '',
            isReversal ? '<span class="entry-pill auto-reverse">↺ AUTO-REV</span>' : '',
            '<span class="entry-pill ' + stateCls + '">' +
                esc(stateVal.replace(/_/g, " ")) + '</span>',
        ].filter(Boolean).join("");

        const sym = c.trading_symbol || c.instrument_key || "—";
        const note = entryCandidateNote(c);
        const grid = entryCandidateGrid(c);

        let extraHtml = '';
        if (c.warning && c.warning_detail) {
            extraHtml += '<div class="entry-warning">⚠️ ' + esc(c.warning_detail) + '</div>';
        }
        if (isReversal && c.state === "discarded" && c.reversal_trigger) {
            const rt = c.reversal_trigger;
            extraHtml +=
                '<div class="entry-reversal">' +
                    '↺ <strong>Auto-reverse fired</strong> — ' +
                    esc(rt.rejected_side || side) + ' rejected at ₹' +
                    fmt(rt.bias_resistance_price) +
                    ' (confirmed ' + formatTime(rt.bias_resistance_confirmed_time_fast) + ').' +
                '</div>';
        }

        return (
            '<div class="entry-candidate' +
                (isChosen ? ' is-chosen' : '') +
                (isBias ? ' is-bias-side' : '') +
                (isReversal ? ' is-auto-reverse' : '') + '">' +
                '<div class="entry-candidate-head">' +
                    '<div class="entry-candidate-symbol">' + esc(sym) + '</div>' +
                    '<div class="entry-candidate-pills">' + pills + '</div>' +
                '</div>' +
                '<div class="entry-note">' + esc(note) + '</div>' +
                grid +
                extraHtml +
            '</div>'
        );
    }

    function renderEntrySignal(payload) {
        const card = document.getElementById("iso-entry-signal");
        const candidatesEl = document.getElementById("entry-candidates");
        const warningsEl = document.getElementById("entry-warnings");
        const chosenPill = document.getElementById("entry-chosen-pill");
        const touchStripEl = document.getElementById("entry-touch-strip");
        if (!card || !candidatesEl) return;

        if (!payload || payload.status === "disabled" || payload.status === "no_isolation") {
            card.style.display = "none";
            return;
        }

        card.style.display = "block";

        const pathBBanner = document.getElementById("entry-path-b-banner");
        if (payload.status === "path_b_mode") {
            card.style.display = "block";
            if (pathBBanner) pathBBanner.style.display = "flex";
            if (chosenPill) {
                chosenPill.textContent = "Path B · Multi-Order";
                chosenPill.className = "sr-count-pill chosen";
            }
            if (touchStripEl) {
                const iso = payload.isolated || {};
                touchStripEl.innerHTML =
                    '<span>Trigger: <strong>' +
                        esc(iso.trigger_index || "—") + " " +
                        esc(iso.trigger_level || "—") +
                    '</strong></span>' +
                    '<span>Bias: <strong>' +
                        esc((payload.path_b || {}).bias_side || "—") +
                    '</strong></span>' +
                    '<span>Routed: <strong>' +
                        esc(iso.routed_index || "NIFTY") +
                    '</strong></span>' +
                    '<span>Mode: <strong>Path B · multi-order SR watch</strong></span>';
            }
            if (candidatesEl) {
                candidatesEl.innerHTML =
                    '<div class="entry-empty">' +
                    '<i class="bi bi-signpost-split"></i>' +
                    'Path B: no single-trade candidate. ' +
                    'See the Dummy Orders card below for the multi-order list.' +
                    '</div>';
            }
            if (warningsEl) {
                warningsEl.style.display = "none";
                warningsEl.innerHTML = "";
            }
            return;
        } else {
            if (pathBBanner) pathBBanner.style.display = "none";
        }

        if (payload.status !== "ok") {
            if (chosenPill) {
                chosenPill.textContent = "Error";
                chosenPill.className = "sr-count-pill";
            }
            if (touchStripEl) touchStripEl.innerHTML = "";
            candidatesEl.innerHTML =
                '<div class="entry-error">' +
                '<i class="bi bi-exclamation-triangle"></i> ' +
                esc(payload.note || payload.error || payload.status) +
                '</div>';
            if (warningsEl) {
                warningsEl.style.display = "none";
                warningsEl.innerHTML = "";
            }
            const promoBanner = document.getElementById("entry-promotion-banner");
            if (promoBanner) {
                promoBanner.style.display = "none";
                promoBanner.innerHTML = "";
            }
            return;
        }

        renderPromotionBanner(payload);
        const promo = payload.promotion || {};
        if (
            promo.recommended
            && !promo.already_applied
            && !state.promoInFlight
        ) {
            state.promoInFlight = true;
            fetch(APPLY_PROMOTION_URL, { method: "POST" })
                .then(r => r.json())
                .then(res => {
                    state.promoInFlight = false;
                    if (res && res.promoted) {
                        loadDetail();
                        loadEntrySignal();
                    } else {
                        loadEntrySignal();
                    }
                })
                .catch(err => {
                    state.promoInFlight = false;
                    console.warn("[promotion] apply failed:", err);
                });
        }

        if (chosenPill) {
            const chosen = payload.chosen_side;
            const already = !!promo.already_applied;
            const entryType = payload.chosen_entry_type;
            if (chosen) {
                let label = already
                    ? `Promoted: ${chosen}`
                    : `Chosen: ${chosen}`;
                if (entryType === "auto_reverse_on_rejection") {
                    label += " ↺";
                }
                chosenPill.textContent = label;
                chosenPill.className = "sr-count-pill chosen";
            } else {
                chosenPill.textContent = "Waiting";
                chosenPill.className = "sr-count-pill";
            }
        }

        renderEntryTouchStrip(payload);

        let list = (payload.candidates || []).slice();
        const frozenEntry = payload.frozen_entry;
        if (frozenEntry && typeof frozenEntry === "object" && list.length) {
            list = list.map(c => {
                if (!c.chosen || !c.entry) return c;
                return {
                    ...c,
                    entry: {
                        ...c.entry,
                        price:         frozenEntry.price        ?? c.entry.price,
                        price_source:  frozenEntry.price_source ?? c.entry.price_source,
                        stop:          frozenEntry.stop         ?? c.entry.stop,
                        target:        frozenEntry.target       ?? c.entry.target,
                        time:          frozenEntry.time         ?? c.entry.time,
                        _frozen:       true,
                    },
                };
            });
        }

        if (!list.length) {
            candidatesEl.innerHTML =
                '<div class="entry-empty">' +
                '<i class="bi bi-info-circle"></i>' +
                'No candidate instruments resolved at this strike.' +
                '</div>';
        } else {
            candidatesEl.innerHTML = list.map(renderEntryCandidate).join("");
        }

        const warns = (payload.warnings || []).slice();
        if (frozenEntry && frozenEntry.price_source === "live_ltp") {
            warns.push(
                "Frozen entry captured from LIVE LTP, not a historical candle close. " +
                "The counter-side candle lookup probably failed — check that " +
                "_isolate() registered both sides and that STRATEGY_ENTRY_CANDLE_FALLBACK_ENABLED " +
                "is on (G1 v4.1 §5.A / §5.B)."
            );
        }
        if (warningsEl) {
            if (warns.length) {
                warningsEl.style.display = "block";
                warningsEl.innerHTML =
                    '<strong>⚠️ Warnings</strong>' +
                    '<ul>' +
                    warns.map(w => '<li>' + esc(w) + '</li>').join("") +
                    '</ul>';
            } else {
                warningsEl.style.display = "none";
                warningsEl.innerHTML = "";
            }
        }
    }

    async function loadEntrySignal() {
        try {
            const res = await fetch(ENTRY_SIGNAL_URL, {
                headers: { Accept: "application/json" },
            });
            if (!res.ok) throw new Error("HTTP " + res.status);
            const data = await res.json();
            renderEntrySignal(data);
        } catch (e) {
            console.warn("[entry-signal] load failed:", e);
        }
    }

    /* ---------- SR Tracking ---------- */
    function srDedupKey(lvl) {
        const price = parseNum(lvl.price);
        return [
            String(lvl.level || ""),
            price === null ? "0" : price.toFixed(4),
            String(lvl.confirmed_time || ""),
        ].join("|");
    }

    function srLevelRowHtml(lvl, opts) {
        const isNew = opts && opts.isNew;
        const isAfter = !!lvl._after_isolation;

        const typeStr = String(lvl.level || "").toLowerCase();
        const typeCls = typeStr === "resistance"
            ? "resistance"
            : typeStr === "support"
                ? "support"
                : "";
        const typeLabel = typeStr === "resistance"
            ? "RESISTANCE"
            : typeStr === "support"
                ? "SUPPORT"
                : (lvl.level || "—").toUpperCase();

        const lb = lvl.left_bars === undefined || lvl.left_bars === null
            ? "—"
            : String(lvl.left_bars);
        const rb = lvl.right_bars === undefined || lvl.right_bars === null
            ? "—"
            : String(lvl.right_bars);

        const rowClass = isAfter ? "sr-row-after-isolation" : "";
        const confPriceStr = lvl.confirmed_time_price != null ? fmt(lvl.confirmed_time_price) : "—";

        return `<tr class="${rowClass}">
            <td>
                <span class="sr-type ${typeCls}">${esc(typeLabel)}</span>
                ${isNew ? `<span class="sr-new-badge">NEW</span>` : ""}
            </td>
            <td class="num"><strong>${fmt(lvl.price)}</strong></td>
            <td>${formatTime(lvl.pivot_time)}</td>
            <td>${formatTime(lvl.confirmed_time)}</td>
            <td class="num">${confPriceStr}</td>
            <td class="num">${esc(lb)} / ${esc(rb)}</td>
        </tr>`;
    }

    function srEmptyStateHtml(message) {
        return `
            <div class="sr-empty-state">
                <i class="bi bi-hourglass-split"></i>
                ${esc(message)}
            </div>`;
    }

    function srLevelsTableHtml(levels, opts) {
        opts = opts || {};
        const showNew = !!opts.showNew;
        const newKeys = opts.newKeys || new Set();
        const rows = levels.map(lvl => {
            const key = srDedupKey(lvl);
            const isNew = showNew && !!lvl._after_isolation && newKeys.has(key);
            return srLevelRowHtml(lvl, { isNew });
        }).join("");
        return `
            <div class="iso-table-wrap">
                <table class="iso-table">
                    <thead>
                        <tr>
                            <th>Type</th>
                            <th class="num">Price</th>
                            <th>Pivot Time</th>
                            <th>Confirmed Time</th>
                            <th class="num">Confirmed Price</th>
                            <th class="num">L / R Bars</th>
                        </tr>
                    </thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>`;
    }

    function renderSRTracking(d) {
        const card = document.getElementById("iso-sr-tracking");
        const body = document.getElementById("sr-tracking-body");
        const beforePill = document.getElementById("sr-before-pill");
        const afterPill = document.getElementById("sr-after-pill");
        if (!card || !body) return;

        if (!d.isolated) {
            card.style.display = "none";
            if (beforePill) {
                beforePill.textContent = "Before: 0";
                beforePill.className = "sr-count-pill";
            }
            if (afterPill) {
                afterPill.textContent = "After: 0";
                afterPill.className = "sr-count-pill post";
            }
            state.seenAllKeys = new Set();
            return;
        }

        card.style.display = "block";

        const live = (d.active && d.active_live_sr_levels)
            ? d.active_live_sr_levels
            : (d.live_sr_levels || {});
        const levels = Array.isArray(live.levels) ? live.levels.slice() : [];
        const available = !!live.available;
        const beforeCount = typeof live.before_count === "number"
            ? live.before_count
            : levels.filter(l => !l._after_isolation).length;
        const afterCount = typeof live.after_count === "number"
            ? live.after_count
            : levels.filter(l => l._after_isolation).length;

        if (beforePill) {
            beforePill.textContent = `Before: ${beforeCount}`;
            beforePill.className = "sr-count-pill" + (beforeCount > 0 ? " has" : "");
        }
        if (afterPill) {
            afterPill.textContent = `After: ${afterCount}`;
            afterPill.className = "sr-count-pill" + (afterCount > 0 ? " post" : "");
        }

        if (!available || levels.length === 0) {
            const msg = live.error
                ? `Live SR levels file unavailable (${live.error}).`
                : "No SR levels recorded on this instrument yet.";
            body.innerHTML = srEmptyStateHtml(msg);
            state.seenAllKeys = new Set();
            return;
        }

        levels.sort((a, b) => {
            const ta = String(a.confirmed_time || "");
            const tb = String(b.confirmed_time || "");
            if (ta < tb) return -1;
            if (ta > tb) return 1;
            return 0;
        });

        const allKeys = new Set(levels.map(srDedupKey));
        const newKeys = new Set();
        if (state.seenAllKeys && state.seenAllKeys.size > 0) {
            for (const k of allKeys) {
                if (!state.seenAllKeys.has(k)) newKeys.add(k);
            }
        }

        const refLabel = live.touched_at
            ? `touch @ ${formatTime(live.touched_at)}`
            : (live.reference_at ? `ref @ ${formatTime(live.reference_at)}` : "");

        const isPromoted = !!(d.active && d.active.record);
        const headerTitle = isPromoted
            ? `SR Levels · Active (promoted) Instrument`
            : `SR Levels · Isolated Instrument`;

        body.innerHTML = `
            <div class="sr-phase-header">
                <span class="phase-title">${esc(headerTitle)}</span>
                <span class="phase-hint">oldest first</span>
                ${refLabel ? `<span class="ref-label">${esc(refLabel)}</span>` : ""}
                <span class="sr-legend">
                    <span class="sr-legend-item before">Before touch</span>
                    <span class="sr-legend-item after">After touch</span>
                </span>
            </div>
            ${srLevelsTableHtml(levels, { showNew: true, newKeys })}`;

        state.seenAllKeys = allKeys;
    }

    /* ---------- SR Compare ---------- */
    function renderCompareCard(d) {
        const card = document.getElementById("iso-sr-compare");
        const pill = document.getElementById("sr-compare-status-pill");
        if (!card) return;

        if (!d || !d.isolated) {
            card.style.display = "none";
            if (pill) {
                pill.textContent = "Idle";
                pill.className = "sr-count-pill";
            }
            return;
        }

        card.style.display = "block";
        if (pill && compareState.selected) {
            pill.textContent = compareState.selected.is_official_isolated
                ? "Showing official"
                : "Showing selection";
            pill.className = compareState.selected.is_official_isolated
                ? "sr-count-pill has"
                : "sr-count-pill post";
        } else if (pill) {
            pill.textContent = "Idle";
            pill.className = "sr-count-pill";
        }
    }

    function renderSearchResults(results) {
        const el = document.getElementById("sr-search-results");
        if (!el) return;
        if (!results || !results.length) {
            el.innerHTML = "";
            return;
        }
        const officialKey = state.detail && state.detail.isolated
            ? state.detail.isolated.instrument_key : null;

        el.innerHTML = results.map(r => {
            const isOfficial = r.instrument_key === officialKey;
            const typeStr = String(r.type || "").toUpperCase();
            const typeCls = typeStr === "CE"
                ? "resistance"
                : typeStr === "PE"
                    ? "support"
                    : "";
            const metaBits = [];
            if (r.strike !== null && r.strike !== undefined) {
                metaBits.push(`Strike ${fmt(r.strike, 0)}`);
            }
            if (r.underlying) metaBits.push(esc(r.underlying));
            if (isOfficial) metaBits.push("<strong>official</strong>");

            return `<button type="button"
                class="sr-search-result ${isOfficial ? "is-official" : ""}"
                data-key="${esc(r.instrument_key)}">
                <span class="sr-type ${typeCls}">${esc(typeStr || "—")}</span>
                <span class="sr-result-symbol">${esc(r.trading_symbol || r.instrument_key)}</span>
                <span class="sr-result-meta">${metaBits.join(" · ")}</span>
            </button>`;
        }).join("");

        el.querySelectorAll(".sr-search-result").forEach(btn => {
            btn.addEventListener("click", () => loadComparison(btn.dataset.key));
        });
    }

    function scheduleSearch(q) {
        compareState.query = q;
        if (compareState.searchTimer) clearTimeout(compareState.searchTimer);
        if (!q) {
            renderSearchResults([]);
            return;
        }
        compareState.searchTimer = setTimeout(() => runSearch(q), 250);
    }

    async function runSearch(q) {
        if (compareState.inflight) compareState.inflight.abort();
        compareState.inflight = new AbortController();
        try {
            const res = await fetch(
                SEARCH_INSTRUMENTS_URL + "?query=" + encodeURIComponent(q) + "&limit=15",
                { signal: compareState.inflight.signal }
            );
            if (!res.ok) throw new Error("HTTP " + res.status);
            const data = await res.json();
            compareState.results = Array.isArray(data.results) ? data.results : [];
            renderSearchResults(compareState.results);
        } catch (e) {
            if (e.name === "AbortError") return;
            console.warn("[compare] search failed:", e);
        }
    }

    async function loadComparison(instrumentKey) {
        if (!instrumentKey) return;
        compareState.selectedKey = instrumentKey;

        const resultsEl = document.getElementById("sr-search-results");
        if (resultsEl) resultsEl.innerHTML = "";
        const bodyEl = document.getElementById("sr-search-body");
        const infoEl = document.getElementById("sr-search-info");
        if (bodyEl) {
            bodyEl.innerHTML = `
                <div class="sr-empty-state">
                    <i class="bi bi-hourglass-split"></i>
                    Loading comparison…
                </div>`;
        }
        if (infoEl) infoEl.style.display = "none";

        try {
            const res = await fetch(INSTRUMENT_SR_URL(instrumentKey));
            if (!res.ok) throw new Error("HTTP " + res.status);
            const data = await res.json();
            compareState.selected = data;
            renderComparison(data);
            renderCompareCard(state.detail);
        } catch (e) {
            if (bodyEl) {
                bodyEl.innerHTML = `
                    <div class="sr-empty-state">
                        <i class="bi bi-exclamation-triangle"></i>
                        Failed to load comparison: ${esc(e.message)}
                    </div>`;
            }
        }
    }

    function renderComparison(data) {
        const info = document.getElementById("sr-search-info");
        const body = document.getElementById("sr-search-body");
        if (!body) return;

        if (!data || data.status === "no_official_isolation") {
            if (info) info.style.display = "none";
            body.innerHTML = `
                <div class="sr-empty-state">
                    <i class="bi bi-info-circle"></i>
                    No instrument has been officially isolated yet today.
                    Wait for an R3 / S3 touch on any of NIFTY / BANKNIFTY /
                    MIDCPNIFTY / SENSEX, then try again.
                </div>`;
            return;
        }

        if (data.status !== "ok") {
            if (info) info.style.display = "none";
            body.innerHTML = `
                <div class="sr-empty-state">
                    <i class="bi bi-exclamation-triangle"></i>
                    ${esc(data.error || "Could not load comparison.")}
                </div>`;
            return;
        }

        if (info) {
            info.style.display = "flex";
            const isOfficial = !!data.is_official_isolated;
            info.className = "sr-search-info " + (
                isOfficial ? "is-official" : "is-user-selected"
            );
            info.innerHTML = `
                <i class="bi ${isOfficial ? "bi-check-circle-fill" : "bi-info-circle-fill"}"></i>
                <div>
                    <strong>${isOfficial
                        ? "This is the officially isolated instrument"
                        : "You selected a different instrument"}</strong><br>
                    <span class="muted">Official isolated:</span>
                    <strong>${esc(data.official_trading_symbol || data.official_instrument_key || "—")}</strong>
                    <span class="muted">(touched at ${formatTime(data.official_touched_at)})</span><br>
                    <span class="muted">Your selection:</span>
                    <strong>${esc(data.trading_symbol || data.instrument_key)}</strong>
                    ${isOfficial
                        ? " — matches the official pick."
                        : " — showing its SR levels tagged against the SAME isolation time."}
                </div>`;
        }

        const live = data.live_sr_levels || {};
        const levels = Array.isArray(live.levels) ? live.levels.slice() : [];
        const available = !!live.available;
        const beforeCount = typeof live.before_count === "number"
            ? live.before_count : 0;
        const afterCount = typeof live.after_count === "number"
            ? live.after_count : 0;

        if (!available || levels.length === 0) {
            const msg = live.error
                ? `No live_sr_levels.json for this instrument (${live.error}).`
                : "No SR levels recorded for this instrument yet.";
            body.innerHTML = srEmptyStateHtml(msg);
            return;
        }

        levels.sort((a, b) => {
            const ta = String(a.confirmed_time || "");
            const tb = String(b.confirmed_time || "");
            if (ta < tb) return -1;
            if (ta > tb) return 1;
            return 0;
        });

        const refLabel = data.reference_at
            ? `ref @ ${formatTime(data.reference_at)}`
            : "";

        body.innerHTML = `
            <div class="sr-phase-header" style="margin-top: 8px;">
                <span class="phase-title">${esc(data.trading_symbol || data.instrument_key)}</span>
                <span class="phase-hint">oldest first</span>
                ${refLabel ? `<span class="ref-label">${esc(refLabel)}</span>` : ""}
                <span class="sr-legend">
                    <span class="sr-legend-item before">Before touch (${beforeCount})</span>
                    <span class="sr-legend-item after">After touch (${afterCount})</span>
                </span>
            </div>
            ${srLevelsTableHtml(levels, { showNew: false })}`;
    }

    /* ---------- Instrument snapshot ---------- */
    function renderInstrumentSnapshot(d) {
        const card = document.getElementById("iso-instrument-snapshot");
        const tilesEl = document.getElementById("snapshot-tiles");
        const orWrapEl = document.getElementById("snapshot-or-wrap");
        const orGridEl = document.getElementById("snapshot-or-grid");
        const titleEl = document.getElementById("snapshot-card-title");
        if (!card || !tilesEl || !orWrapEl || !orGridEl) return;

        if (!d.isolated) {
            card.style.display = "none";
            return;
        }

        const isPromoted = !!(d.active && d.active.record);
        const iso = isPromoted ? d.active.record : d.isolated;
        const avail = snapshotAvailability({ isolated: iso });

        card.style.display = "block";
        if (titleEl) {
            const suffix = isPromoted ? " · promoted" : "";
            titleEl.textContent =
                `Isolated instrument · own data at touch${suffix} · ` +
                `${iso.trading_symbol || iso.instrument_key || ""}`;
        }

        if (!avail.hasAny) {
            tilesEl.innerHTML = `
                <div class="snapshot-empty-state">
                    <i class="bi bi-info-circle"></i>
                    <div>No instrument-snapshot data is stored for this
                        isolated record yet.</div>
                    <div style="margin-top: 8px; font-size: 0.82rem;">
                        This record was created before the snapshot feature
                        was deployed. Click
                        <strong>Refresh Snapshot</strong> in the top bar, or
                        wait for the 5-minute recheck job to auto-collect it
                        (during market hours).
                    </div>
                </div>
            `;
            orWrapEl.style.display = "none";
            orGridEl.innerHTML = "";
            return;
        }

        const instrumentLtp = parseNum(iso.instrument_ltp);
        const touchOhlc = iso.instrument_touch_ohlc || {};
        const firstCandle = iso.instrument_first_candle || {};
        const orLevels = iso.instrument_or_levels || {};
        const source = iso.instrument_quote_source || "unavailable";

        tilesEl.innerHTML = "";

        if (instrumentLtp !== null) {
            tilesEl.insertAdjacentHTML("beforeend", `
                <div class="snapshot-tile highlight">
                    <div class="tile-label">Instrument LTP @ touch</div>
                    <div class="tile-value">₹${fmt(instrumentLtp)}</div>
                    <div class="tile-sub">source: ${esc(quoteSourceLabel(source))}</div>
                </div>
            `);
        }

        if (avail.hasTouch) {
            const t = touchOhlc.time_iso ? formatTime(touchOhlc.time_iso) : "—";
            tilesEl.insertAdjacentHTML("beforeend", `
                <div class="snapshot-tile">
                    <div class="tile-label">Instrument touch-bar · ${esc(t)}</div>
                    <div class="tile-value">
                        <div class="ohlc-grid">
                            <div class="ohlc-cell"><span class="ohlc-key">O</span><span>${fmt(touchOhlc.open)}</span></div>
                            <div class="ohlc-cell"><span class="ohlc-key">H</span><span>${fmt(touchOhlc.high)}</span></div>
                            <div class="ohlc-cell"><span class="ohlc-key">L</span><span>${fmt(touchOhlc.low)}</span></div>
                            <div class="ohlc-cell"><span class="ohlc-key">C</span><span>${fmt(touchOhlc.close)}</span></div>
                        </div>
                    </div>
                    ${touchOhlc.volume !== undefined && touchOhlc.volume !== null
                        ? `<div class="tile-sub">vol: ${fmtInt(touchOhlc.volume)}</div>`
                        : ""}
                </div>
            `);
        }

        if (avail.hasFc) {
            const fcTime = firstCandle.time_iso
                ? formatTime(firstCandle.time_iso)
                : "09:15:00";
            tilesEl.insertAdjacentHTML("beforeend", `
                <div class="snapshot-tile">
                    <div class="tile-label">Instrument first candle · ${esc(fcTime)}</div>
                    <div class="tile-value">
                        <div class="ohlc-grid">
                            <div class="ohlc-cell"><span class="ohlc-key">O</span><span>${fmt(firstCandle.open)}</span></div>
                            <div class="ohlc-cell"><span class="ohlc-key">H</span><span>${fmt(firstCandle.high)}</span></div>
                            <div class="ohlc-cell"><span class="ohlc-key">L</span><span>${fmt(firstCandle.low)}</span></div>
                            <div class="ohlc-cell"><span class="ohlc-key">C</span><span>${fmt(firstCandle.close)}</span></div>
                        </div>
                    </div>
                </div>
            `);
        }

        if (avail.hasOr) {
            orWrapEl.style.display = "block";
            const display = [
                "H", "L", "A",
                "R1", "R2", "R3", "R3_Threshold",
                "S1", "S2", "S3", "S3_Threshold",
            ];
            const cells = display
                .filter(k => orLevels[k] !== undefined && orLevels[k] !== null)
                .map(k => `<div class="or-cell">
                    <span class="or-label">${esc(k)}</span>
                    <span class="or-value">${fmt(orLevels[k])}</span>
                </div>`)
                .join("");

            const typeClass = iso.type === "CE"
                ? "ce" : iso.type === "PE" ? "pe" : "";

            orGridEl.innerHTML = `
                <div class="snapshot-or-block">
                    <div class="snapshot-or-block-title">
                        <span>${esc(iso.trading_symbol || iso.instrument_key || "")}</span>
                        <span class="snapshot-or-type-badge ${typeClass}">${esc(iso.type || "—")}</span>
                        <span style="font-weight: 700; color: var(--iso-muted); font-size: 0.72rem;">
                            Strike ${fmt(iso.strike, 0)} · Expiry ${formatExpiry(iso.expiry)}
                        </span>
                    </div>
                    <div class="or-grid" style="grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));">
                        ${cells}
                    </div>
                </div>
            `;
        } else {
            orWrapEl.style.display = "none";
            orGridEl.innerHTML = "";
        }
    }

    /* ---------- Reasoning (G1 v3.2 multi-index routing) ---------- */
    function renderReasoning(d) {
        const card = document.getElementById("iso-reasoning");
        if (!card) return;

        if (!d.isolated || !d.trigger_event) {
            card.style.display = "none";
            return;
        }
        card.style.display = "block";

        const iso = d.isolated;
        const ev = d.trigger_event;
        const simulate = iso.source === "backfill";
        const steps = [];

        if (simulate) {
            steps.push({
                icon: "bi-arrow-clockwise",
                title: "Simulate Mode",
                detail:
                    "Record reconstructed from today's intraday 1-min candles " +
                    "after the market close. Replay uses the same priority " +
                    "arbitration and NIFTY routing as live.",
            });
        }

        steps.push({
            icon: "bi-lightning-charge-fill",
            title: "Trigger",
            detail:
                `${iso.trigger_index || ev.index} touched ${ev.trigger_level} at ` +
                `${fmt(ev.trigger_level_value)} (LTP ${fmt(ev.trigger_ltp)}) ` +
                `at ${formatTime(ev.touched_at)}` +
                (iso.trigger_priority ? ` · priority P${iso.trigger_priority}` : "") + ".",
        });

        if (Array.isArray(iso.simultaneous_touches) && iso.simultaneous_touches.length) {
            steps.push({
                icon: "bi-list-ol",
                title: "Simultaneous touches",
                detail:
                    "Also touched in the same window: " +
                    iso.simultaneous_touches
                        .map(t => `${t.canonical || t.index} ${t.side || ""}`.trim())
                        .join(", ") + ". Priority order resolved to " +
                    iso.trigger_index + ".",
            });
        }

        const routedIdx = iso.routed_index || "NIFTY";
        const routedLtp = iso.routed_nifty_ltp;
        steps.push({
            icon: "bi-arrow-right-circle",
            title: "Routed to " + routedIdx,
            detail:
                "Regardless of which index fired the touch, strike selection " +
                "runs on " + routedIdx + " spot LTP" +
                (routedLtp != null ? " (" + fmt(routedLtp) + ")" : "") +
                " → nearest 50 strike = " + fmt(iso.strike, 0) + ".",
        });

        const instrLtp = parseNum(iso.instrument_ltp);
        if (instrLtp !== null) {
            steps.push({
                icon: "bi-cash-stack",
                title: "Instrument LTP @ touch",
                detail:
                    `${iso.trading_symbol || iso.instrument_key} traded at ` +
                    `₹${fmt(instrLtp)} at the touch moment ` +
                    `(source: ${quoteSourceLabel(iso.instrument_quote_source)}).`,
            });
        }

        steps.push({
            icon: "bi-check-circle-fill",
            title: "Isolated",
            detail: iso.trading_symbol || iso.instrument_key || "—",
        });

        if (d.active && d.active.record) {
            steps.push({
                icon: "bi-arrow-up-circle-fill",
                title: "Promoted",
                detail:
                    `Strategy entry armed on ${d.active.to_trading_symbol || d.active.to_instrument_key} ` +
                    `— promoted from ${d.active.from_trading_symbol || d.active.from_instrument_key}.`,
            });
        }

        const body = document.getElementById("iso-reasoning-body");
        body.innerHTML = steps
            .map(s =>
                `<div class="reason-step">` +
                `<div class="reason-step-icon"><i class="bi ${s.icon}"></i></div>` +
                `<div class="reason-step-body">` +
                `<div class="reason-step-title">${esc(s.title)}</div>` +
                `<div class="reason-step-detail">${esc(s.detail)}</div>` +
                `</div></div>`
            )
            .join("");
    }

    /* ---------- Candidates (G1 v4.1 §5.F — side column) ---------- */
    function renderCandidates(d) {
        const body = document.getElementById("candidates-body");
        const titleEl = document.getElementById("candidates-title");
        if (!body) return;

        const iso = d.isolated;
        const ev = d.trigger_event || {};

        let list;
        let distanceKey;
        let refLabel;
        if (Array.isArray(ev.candidates) && ev.candidates.length) {
            list = ev.candidates;
            distanceKey = "distance_to_trigger_ltp";
            const routedLtp = iso && iso.routed_nifty_ltp != null
                ? iso.routed_nifty_ltp
                : ev.trigger_ltp;
            const routedIdx = iso && iso.routed_index ? iso.routed_index : "NIFTY";
            refLabel = `${routedIdx} spot (${fmt(routedLtp)})`;
        } else {
            list = [];
            distanceKey = "distance_to_trigger_ltp";
            refLabel = "—";
        }

        const hasCounter = list.some(c => String(c.side || "").toLowerCase() === "counter");
        if (titleEl) {
            if (list.length) {
                const crossoverSuffix = hasCounter ? " · bias + counter side" : "";
                titleEl.textContent =
                    `Candidates considered · ${list.length} · ${refLabel}${crossoverSuffix}`;
            } else {
                titleEl.textContent = "Candidates considered";
            }
        }

        if (!list.length) {
            body.innerHTML =
                `<tr><td colspan="6" class="iso-empty">` +
                `No candidates recorded for this isolation yet.` +
                `</td></tr>`;
            return;
        }

        const pickedKey = iso && iso.instrument_key;

        body.innerHTML = list
            .map(c => {
                const isPicked = c.instrument_key === pickedKey;
                const type = (c.type || "").toUpperCase();

                const sideRaw = String(c.side || "").toLowerCase();
                let sideChip;
                if (sideRaw === "counter") {
                    sideChip = '<span class="side-chip counter">counter</span>';
                } else if (sideRaw === "bias") {
                    sideChip = '<span class="side-chip bias">bias</span>';
                } else {
                    sideChip = '<span class="side-chip legacy">—</span>';
                }

                const levelTags = (c.levels || [])
                    .map(lv => {
                        const cls = String(lv).startsWith("R")
                            ? "r"
                            : String(lv).startsWith("S")
                                ? "s"
                                : "";
                        return `<span class="level-tag ${cls}">${esc(lv)}</span>`;
                    })
                    .join("") || '<span style="color:var(--iso-muted)">—</span>';
                const dist = c[distanceKey];
                const distStr =
                    dist !== null && dist !== undefined ? fmt(dist) : "—";
                const chartUrl =
                    "/chart/" + encodeURIComponent(c.instrument_key);
                const rowCls =
                    (isPicked ? "is-picked " : "") +
                    (sideRaw === "counter" ? "is-counter-candidate" : "");
                return `<tr class="${rowCls.trim()}">
                    <td>${sideChip}</td>
                    <td><strong>${fmt(c.strike, 0)}</strong></td>
                    <td><span class="type-pill ${type.toLowerCase()}">${esc(type)}</span></td>
                    <td>${levelTags}</td>
                    <td class="num">${distStr}</td>
                    <td>
                        <a href="${chartUrl}" target="_blank" rel="noopener noreferrer" class="iso-btn" style="padding:4px 10px;font-size:0.72rem;">
                            <i class="bi bi-graph-up-arrow"></i> Chart
                        </a>
                    </td>
                </tr>`;
            })
            .join("");
    }

    /* ---------- Touch state (8 rows: 4 indices × 2 sides) ---------- */
    function renderTouchState(d) {
        const body = document.getElementById("touch-body");
        const pill = document.getElementById("touch-state-pill");
        if (!body) return;

        const ts = d.touch_state || {};
        const or = d.or_levels || {};
        const priorityMap = {};
        if (Array.isArray(d.monitored_indices)) {
            for (const m of d.monitored_indices) {
                if (m && m.canonical) {
                    priorityMap[String(m.canonical).toUpperCase()] = m.priority;
                }
            }
        }
        const getPriority = (canonical) =>
            priorityMap[canonical] || DEFAULT_PRIORITY[canonical] || 99;

        const rows = [];
        let touchedCount = 0;
        for (const canonical of INDEX_ORDER) {
            const priority = getPriority(canonical);
            const levels = or[canonical] || {};
            for (const side of ["R3", "S3"]) {
                const key = `${canonical}_${side}`;
                const touched = !!ts[key];
                if (touched) touchedCount++;
                const levelValue = levels[side];
                const chip = touched
                    ? `<span class="touch-chip touched"><i class="bi bi-check-circle-fill"></i> touched</span>`
                    : `<span class="touch-chip not-touched"><i class="bi bi-circle"></i> waiting</span>`;
                rows.push(`<tr>
                    <td><strong>${esc(canonical)}</strong></td>
                    <td>${esc(side)}</td>
                    <td class="num">P${esc(priority)}</td>
                    <td class="num">${levelValue != null ? fmt(levelValue) : "—"}</td>
                    <td>${chip}</td>
                </tr>`);
            }
        }

        if (pill) {
            pill.textContent = `${touchedCount} / 8`;
            pill.className = "sr-count-pill" + (touchedCount > 0 ? " has" : "");
        }

        body.innerHTML = `
            <table class="iso-table">
                <thead>
                    <tr>
                        <th>Index</th>
                        <th>Side</th>
                        <th class="num">Priority</th>
                        <th class="num">Level</th>
                        <th>Status</th>
                    </tr>
                </thead>
                <tbody>${rows.join("")}</tbody>
            </table>`;
    }

    /* ---------- Event log ---------- */
    function renderEvents(d) {
        const body = document.getElementById("events-body");
        if (!body) return;

        const events = (d.events || []).slice(-50).reverse();
        if (!events.length) {
            body.innerHTML =
                `<tr><td colspan="8" class="iso-empty">` +
                `No events today.` +
                `</td></tr>`;
            return;
        }
        body.innerHTML = events
            .map(ev => {
                const status = String(ev.status || "");
                const source = String(ev.source || "");
                const isSimulate = source === "backfill";
                let statusBadge =
                    `<span class="touch-chip not-touched">${esc(status || "—")}</span>`;
                if (status === "isolated") {
                    statusBadge = isSimulate
                        ? `<span class="touch-chip touched" style="background: var(--iso-simulate-bg); color: var(--iso-simulate);"><i class="bi bi-arrow-clockwise"></i> simulate</span>`
                        : `<span class="touch-chip touched"><i class="bi bi-check-circle-fill"></i> isolated</span>`;
                }
                const inst =
                    ev.isolated_instrument ||
                    ev.existing_isolated_instrument ||
                    "—";
                const instLabel = ev.status === "isolated"
                    ? `<a href="/chart/${encodeURIComponent(inst)}" target="_blank" rel="noopener noreferrer" style="color:var(--iso-primary);font-weight:700;text-decoration:none;">${esc(inst)}</a>`
                    : esc(inst);
                const rowCls =
                    (ev.status === "isolated" ? " is-trigger-row" : "") +
                    (isSimulate ? " is-simulate-row" : "");
                return `<tr class="${rowCls.trim()}">
                    <td>${formatTime(ev.touched_at)}</td>
                    <td><strong>${esc(ev.index || "—")}</strong></td>
                    <td>${esc(ev.side || "—")}</td>
                    <td class="num">P${esc(ev.trigger_priority || "—")}</td>
                    <td class="num">${fmt(ev.trigger_ltp)}</td>
                    <td class="num">${fmt(ev.trigger_level_value)}</td>
                    <td>${statusBadge}</td>
                    <td>${instLabel}</td>
                </tr>`;
            })
            .join("");
    }

    /* ---------- OR levels ---------- */
    function renderOrLevels(d) {
        const card = document.getElementById("or-levels-card");
        const body = document.getElementById("or-levels-body");
        if (!card || !body) return;

        const or = d.or_levels || {};
        const indexes = Object.keys(or);
        if (!indexes.length) {
            card.style.display = "none";
            return;
        }
        card.style.display = "block";

        const iso = d.isolated || {};
        const hitIndex = iso.trigger_index || null;
        const hitLevel = iso.trigger_level || null;

        const priorityMap = {};
        if (Array.isArray(d.monitored_indices)) {
            for (const m of d.monitored_indices) {
                if (m && m.canonical) {
                    priorityMap[String(m.canonical).toUpperCase()] = m.priority;
                }
            }
        }
        const sorted = indexes.slice().sort((a, b) => {
            const pa = priorityMap[a] || DEFAULT_PRIORITY[a] || 99;
            const pb = priorityMap[b] || DEFAULT_PRIORITY[b] || 99;
            return pa - pb;
        });

        body.innerHTML = sorted
            .map(idx => {
                const levels = or[idx] || {};
                const display = [
                    "H", "L", "A",
                    "R1", "R2", "R3", "R3_Threshold",
                    "S1", "S2", "S3", "S3_Threshold",
                ];
                const cells = display
                    .filter(k => levels[k] !== undefined && levels[k] !== null)
                    .map(k => {
                        const isHit = idx === hitIndex && k === hitLevel;
                        return `<div class="or-cell ${isHit ? "or-hit" : ""}">
                            <span class="or-label">${esc(k)}</span>
                            <span class="or-value">${fmt(levels[k])}</span>
                        </div>`;
                    })
                    .join("");
                const priority = priorityMap[idx] || DEFAULT_PRIORITY[idx] || "—";
                const isHitIndex = idx === hitIndex;
                return `<div class="or-block">
                    <div class="or-block-title">
                        ${esc(idx)}
                        <span style="font-weight:600;color:var(--iso-muted);font-size:0.7rem;margin-left:6px;">
                            (P${esc(priority)}${isHitIndex ? " · trigger" : ""})
                        </span>
                    </div>
                    ${cells}
                </div>`;
            })
            .join("");
    }

    /* ---------- Master render ---------- */
    function render() {
        const d = state.detail;
        if (!d) return;
        renderStatusPill(d);
        renderBanner(d);
        renderRefreshSnapshotBtn(d);
        renderHero(d);
        renderSRTracking(d);
        renderCompareCard(d);
        renderInstrumentSnapshot(d);
        renderReasoning(d);
        renderCandidates(d);
        renderTouchState(d);
        renderEvents(d);
        renderOrLevels(d);
        syncLiveSubscriptions();
    }

    /* ---------- data loading ---------- */
    async function loadDetail() {
        try {
            const res = await fetch(DETAIL_URL, {
                headers: { Accept: "application/json" },
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            state.detail = await res.json();
            render();
        } catch (err) {
            console.error("[isolated] detail load failed:", err);
        }
    }

    async function runBackfill() {
        const btn = document.getElementById("backfill-btn");
        if (!btn) return;
        const original = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<i class="bi bi-hourglass-split"></i> Running replay…';
        try {
            const res = await fetch(BACKFILL_URL, { method: "POST" });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            await loadDetail();
            await loadEntrySignal();
        } catch (err) {
            console.error("[isolated] backfill failed:", err);
        } finally {
            btn.disabled = false;
            btn.innerHTML = original;
        }
    }

    async function runRefreshSnapshot() {
        const btn = document.getElementById("refresh-snapshot-btn");
        if (!btn) return;
        const original = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<i class="bi bi-hourglass-split"></i> Refreshing…';
        try {
            const res = await fetch(REFRESH_SNAPSHOT_URL, { method: "POST" });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            await loadDetail();
            await loadEntrySignal();
        } catch (err) {
            console.error("[isolated] refresh-snapshot failed:", err);
        } finally {
            btn.disabled = false;
            btn.innerHTML = original;
        }
    }

    /* ---------- bootstrap ---------- */
    document.addEventListener("DOMContentLoaded", () => {
        if (window.__INITIAL_DETAIL__) {
            state.detail = window.__INITIAL_DETAIL__;
            render();
        }
        loadDetail();
        state.pollTimer = setInterval(loadDetail, POLL_MS);

        loadEntrySignal();
        state.entryPollTimer = setInterval(loadEntrySignal, ENTRY_POLL_MS);

        connectLiveFeed();

        const refreshBtn = document.getElementById("refresh-btn");
        if (refreshBtn) {
            refreshBtn.addEventListener("click", () => {
                loadDetail();
                loadEntrySignal();
            });
        }

        const backfillBtn = document.getElementById("backfill-btn");
        if (backfillBtn) backfillBtn.addEventListener("click", runBackfill);

        const refreshSnapshotBtn = document.getElementById("refresh-snapshot-btn");
        if (refreshSnapshotBtn) {
            refreshSnapshotBtn.addEventListener("click", runRefreshSnapshot);
        }

        const searchInput = document.getElementById("sr-search-input");
        if (searchInput) {
            searchInput.addEventListener("input", (ev) => {
                scheduleSearch(ev.target.value.trim());
            });
        }
    });

    document.addEventListener("visibilitychange", () => {
        if (!document.hidden &&
            (!liveFeedState.ws ||
             liveFeedState.ws.readyState === WebSocket.CLOSED)) {
            connectLiveFeed();
        }
    });

    window.addEventListener("beforeunload", () => {
        if (state.pollTimer) clearInterval(state.pollTimer);
        if (state.entryPollTimer) clearInterval(state.entryPollTimer);
        if (compareState.searchTimer) clearTimeout(compareState.searchTimer);
        if (liveFeedState.reconnectTimer) clearTimeout(liveFeedState.reconnectTimer);
        if (liveFeedState.ws) {
            try { liveFeedState.ws.close(); } catch (_) {}
        }
    });

    /* ============================================================
       SIMULATION — post-close dummy-order replay
       ============================================================ */
    (function attachSimulation() {
        const SIM_RUN_URL = "/admin/simulation/run";

        const runBtn = document.getElementById("sim-run-btn");
        const bodyEl = document.getElementById("sim-body");
        const statusPill = document.getElementById("sim-status-pill");
        const regimeBanner = document.getElementById("sim-regime-banner");
        const card = document.getElementById("iso-simulation");

        if (!runBtn || !bodyEl) return;

        function updateVisibility() {
            if (!card) return;
            const d = state && state.detail;
            if (d && d.isolated) {
                card.style.display = "block";
            } else {
                card.style.display = "none";
            }
        }
        updateVisibility();
        setInterval(updateVisibility, 1000);

        function setStatus(text, cls) {
            if (!statusPill) return;
            statusPill.textContent = text;
            statusPill.className = "sr-count-pill" + (cls ? " " + cls : "");
        }

        function fmtPct(v) {
            const n = Number(v);
            return Number.isFinite(n) ? (n * 100).toFixed(1) + "%" : "—";
        }
        function fmtR(v) {
            const n = Number(v);
            return Number.isFinite(n) ? (n >= 0 ? "+" : "") + n.toFixed(2) + "R" : "—";
        }
        function fmtTime(iso) {
            if (!iso) return "—";
            try {
                const d = new Date(iso);
                if (isNaN(d.getTime())) return String(iso);
                return d.toLocaleTimeString("en-IN", {
                    timeZone: "Asia/Kolkata",
                    hour: "2-digit",
                    minute: "2-digit",
                    second: "2-digit",
                    hour12: false,
                });
            } catch (_) {
                return String(iso);
            }
        }

        function regimeColor(regimeName) {
            const r = String(regimeName || "").toLowerCase();
            if (r.startsWith("high")) return "var(--iso-red)";
            if (r.startsWith("low")) return "var(--iso-green)";
            return "var(--iso-primary)";
        }

        function renderScenario(key, s, isWinner) {
            const r = (s && s.replay) || {};
            const exitReason = r.exit_reason || "—";
            const pnl = Number.isFinite(Number(r.pnl_pct)) ? Number(r.pnl_pct) : null;
            const rMult = Number.isFinite(Number(r.r_multiple)) ? Number(r.r_multiple) : null;
            const stopPct = Number.isFinite(Number(s.stop_pct)) ? Number(s.stop_pct) : null;
            const targetPct = Number.isFinite(Number(s.target_pct)) ? Number(s.target_pct) : null;
            const rr = Number.isFinite(Number(s.rr_ratio)) ? Number(s.rr_ratio) : null;

            const pnlCls = pnl == null ? "" : (pnl >= 0 ? "up" : "down");
            const stateCls = pnl == null
                ? "state-waiting"
                : (pnl >= 0 ? "state-armed" : "state-blocked");

            const exitTimeStr = String(r.exit_time || "").replace("T", " ").slice(0, 19) || "—";

            return `
                <div class="entry-candidate${isWinner ? " is-chosen" : ""}" data-scenario="${esc(key)}">
                    <div class="entry-candidate-head">
                        <div class="entry-candidate-symbol">${esc(s.label || key)}</div>
                        <div class="entry-candidate-pills">
                            ${isWinner ? '<span class="entry-pill state-armed">🏆 WINNER</span>' : ''}
                            <span class="entry-pill ${stateCls}">${esc(exitReason)}</span>
                        </div>
                    </div>
                    <div class="entry-grid">
                        <div class="label">Entry price</div>
                        <div class="value">₹${fmt(s.entry_price)}</div>

                        <div class="label">Stop-loss</div>
                        <div class="value">₹${fmt(s.stop_price)} (${fmtPct(-stopPct)})</div>

                        <div class="label">Target</div>
                        <div class="value">${s.target_price != null ? `₹${fmt(s.target_price)} (+${fmtPct(targetPct)})` : "—"}</div>

                        <div class="label">R:R (nominal)</div>
                        <div class="value">${rr != null ? rr.toFixed(2) + " : 1" : "—"}</div>

                        <div class="label">Stop source</div>
                        <div class="value"><small>${esc(s.stop_source || "—")}</small></div>

                        <div class="label">Candles scanned</div>
                        <div class="value">${r.candles_scanned != null ? r.candles_scanned : "—"}</div>

                        <div class="label">Exit price</div>
                        <div class="value">₹${fmt(r.exit_price)}</div>

                        <div class="label">Exit time</div>
                        <div class="value"><small>${esc(exitTimeStr)}</small></div>

                        <div class="label">PnL</div>
                        <div class="value ${pnlCls}">${pnl != null ? fmtPct(pnl) : "—"}</div>

                        <div class="label">R-multiple</div>
                        <div class="value ${pnlCls}">${rMult != null ? fmtR(rMult) : "—"}</div>

                        <div class="label">MFE</div>
                        <div class="value">₹${fmt(r.mfe)}</div>

                        <div class="label">MAE</div>
                        <div class="value">₹${fmt(r.mae)}</div>
                    </div>
                </div>
            `;
        }

        function renderResult(data) {
            if (!data || data.status !== "ok") {
                const reason = (data && (data.reason || data.error || data.status)) || "unknown";
                bodyEl.innerHTML = `
                    <div class="sr-empty-state">
                        <i class="bi bi-exclamation-triangle"></i>
                        Simulation could not run: <strong>${esc(reason)}</strong>
                    </div>`;
                setStatus("Error", "error");
                if (regimeBanner) {
                    regimeBanner.style.display = "none";
                    regimeBanner.innerHTML = "";
                }
                return;
            }

            setStatus("Replayed", "has");

            const regime = data.regime || {};
            if (regimeBanner) {
                regimeBanner.style.display = "flex";
                const clsColor = regimeColor(regime.regime);
                regimeBanner.style.borderColor = clsColor;
                regimeBanner.innerHTML = `
                    <i class="bi bi-tag-fill" style="color:${clsColor}"></i>
                    <div>
                        <strong style="color:${clsColor}">Regime: ${esc(regime.regime || "—")}</strong><br>
                        <span style="font-size:0.85rem;color:var(--iso-muted)">
                            ${esc(regime.reason || "")}
                        </span>
                    </div>
                `;
            }

            const sc = data.scenarios || {};
            const verdict = data.verdict || {};
            const winner = verdict.winner;

            const scenarioHtml = Object.entries(sc).map(([k, s]) => {
                const isWin = (k === "signal_v2" && winner === "signal_v2")
                    || (k === "regime" && winner === "regime");
                return renderScenario(k, s, isWin);
            }).join("");

            const notes = (data.notes || []).map(n => `<li>${esc(n)}</li>`).join("");

            const entry = data.entry || {};
            const entryStrip = `
                <div class="entry-touch-strip">
                    <span>Instrument: <strong>${esc(data.trading_symbol || data.instrument_key)}</strong></span>
                    <span>Side: <strong>${esc(data.side || "—")}</strong></span>
                    <span>Entry: <strong>₹${fmt(entry.price)}</strong></span>
                    <span>Entry time: <strong>${esc(String(entry.time || "").replace("T", " ").slice(0, 19) || "—")}</strong></span>
                    <span>Source: <strong>${esc(quoteSourceLabel(entry.price_source))}</strong></span>
                    <span>Candles in session: <strong>${data.candles_total || 0}</strong></span>
                </div>
            `;

            let verdictHtml = "";
            if (winner && winner !== "unknown" && winner !== "tie") {
                const wLabel = winner === "regime" ? "Regime scenario" : "Signal scenario";
                verdictHtml = `
                    <div class="sim-verdict-winner">
                        🏆 <strong>${esc(wLabel)} wins.</strong>
                        ${esc(verdict.reason || "")}
                    </div>`;
            } else if (winner === "tie") {
                verdictHtml = `
                    <div class="sim-verdict-tie">
                        Both scenarios produced the same R-multiple (tie).
                    </div>`;
            }

            bodyEl.innerHTML = `
                ${entryStrip}
                <div class="entry-candidates">${scenarioHtml}</div>
                ${verdictHtml}
                <div class="sim-notes">
                    <strong>📝 Notes</strong>
                    <ul>${notes || "<li>—</li>"}</ul>
                </div>
            `;
        }

        async function runSimulation() {
            if (!runBtn) return;
            const orig = runBtn.innerHTML;
            runBtn.disabled = true;
            runBtn.innerHTML = '<i class="bi bi-hourglass-split"></i> Running…';
            setStatus("Running…", "");

            try {
                const resp = await fetch(SIM_RUN_URL);
                if (!resp.ok) {
                    throw new Error("HTTP " + resp.status);
                }
                const data = await resp.json();
                renderResult(data);
            } catch (e) {
                bodyEl.innerHTML = `
                    <div class="sr-empty-state">
                        <i class="bi bi-exclamation-triangle"></i>
                        Simulation request failed: ${esc(e.message || String(e))}
                    </div>`;
                setStatus("Error", "error");
            } finally {
                runBtn.disabled = false;
                runBtn.innerHTML = orig;
            }
        }

        if (runBtn) {
            runBtn.addEventListener("click", runSimulation);
        }
    })();

    /* ============================================================
       DUMMY ORDERS — live tracking card
       ============================================================ */
    (function attachDummyOrders() {
        const STATE_URL = "/admin/dummy-orders";
        const SUMMARY_URL = "/admin/dummy-orders/summary";
        const POLL_MS = 15000;

        const card = document.getElementById("iso-dummy-orders");
        const summaryStrip = document.getElementById("do-summary-strip");
        const bodyEl = document.getElementById("do-body");
        const statusPill = document.getElementById("do-status-pill");
        const lockdownBanner = document.getElementById("do-lockdown-banner");

        if (!card || !summaryStrip || !bodyEl) return;

        let pollTimer = null;

        function setStatus(text, cls) {
            if (!statusPill) return;
            statusPill.textContent = text;
            statusPill.className = "sr-count-pill" + (cls ? " " + cls : "");
        }

        function fmtPct(v) {
            const n = Number(v);
            return Number.isFinite(n) ? (n * 100).toFixed(1) + "%" : "—";
        }
        function fmtR(v) {
            const n = Number(v);
            return Number.isFinite(n) ? (n >= 0 ? "+" : "") + n.toFixed(2) + "R" : "—";
        }
        function fmtTime(iso) {
            if (!iso) return "—";
            try {
                const d = new Date(iso);
                if (isNaN(d.getTime())) return String(iso);
                return d.toLocaleTimeString("en-IN", {
                    timeZone: "Asia/Kolkata",
                    hour: "2-digit",
                    minute: "2-digit",
                    second: "2-digit",
                    hour12: false,
                });
            } catch (_) {
                return String(iso);
            }
        }

        function computeTotalRsPnl(orders) {
            let total = 0;
            for (const o of orders || []) {
                if (!o || o.status === "OPEN") continue;
                const persisted = Number(o.closed_profit_rs);
                if (Number.isFinite(persisted)) {
                    total += persisted;
                    continue;
                }
                const pnlPct = Number(o.closed_pnl_pct);
                const entry = Number(o.entry_price);
                const lot = Number(o.lot_size);
                if (Number.isFinite(pnlPct) && Number.isFinite(entry) && entry > 0) {
                    if (Number.isFinite(lot) && lot > 0) {
                        total += entry * pnlPct * lot;
                    }
                }
            }
            return total;
        }

        function countPaths(orders) {
            let pathA = 0, pathB = 0;
            for (const o of orders || []) {
                if (!o) continue;
                if (String(o.path || "") === "B_external_trigger") pathB++;
                else pathA++;
            }
            return { pathA, pathB };
        }

        function renderSummary(data, orders) {
            if (!summaryStrip) return;
            const dayPnl = Number(data.day_pnl_pct) || 0;
            const dayR = Number(data.day_r_multiple) || 0;
            const pnlCls = dayPnl >= 0 ? "up" : "down";
            const rCls = dayR >= 0 ? "up" : "down";

            const totalRs = computeTotalRsPnl(orders);
            const rsCls = totalRs >= 0 ? "up" : "down";
            const { pathA, pathB } = countPaths(orders);

            summaryStrip.innerHTML = `
                <div class="do-summary-tile">
                    <div class="do-label">Day ₹ PnL</div>
                    <div class="do-value ${rsCls}">${fmtRs(totalRs)}</div>
                </div>
                <div class="do-summary-tile">
                    <div class="do-label">Day PnL %</div>
                    <div class="do-value ${pnlCls}">${fmtPct(dayPnl)}</div>
                </div>
                <div class="do-summary-tile">
                    <div class="do-label">Day R</div>
                    <div class="do-value ${rCls}">${fmtR(dayR)}</div>
                </div>
                <div class="do-summary-tile">
                    <div class="do-label">Open</div>
                    <div class="do-value">${data.open_count || 0}</div>
                </div>
                <div class="do-summary-tile">
                    <div class="do-label">Closed</div>
                    <div class="do-value">${data.closed_count || 0}</div>
                </div>
                <div class="do-summary-tile">
                    <div class="do-label">Total today</div>
                    <div class="do-value">${data.total_today || 0}</div>
                </div>
                <div class="do-summary-tile">
                    <div class="do-label">Path A / Path B</div>
                    <div class="do-value">${pathA} / ${pathB}</div>
                </div>
                <div class="do-summary-tile">
                    <div class="do-label">Mode</div>
                    <div class="do-value">${esc(data.mode || "—")}</div>
                </div>
            `;
        }

        function buildSourceCell(o) {
            const origin = o.origin_level || {};
            if (origin && origin.type && origin.price != null) {
                const t = String(origin.type).toLowerCase();
                const cls = t === "resistance" ? "resistance"
                          : t === "support"    ? "support"
                          : "signal";
                const label = t.toUpperCase();
                return (
                    `<span class="src-type ${cls}">${esc(label)}</span>` +
                    `<span class="src-price">₹${fmt(origin.price)}</span>` +
                    (origin.confirmed_time
                        ? `<span class="src-meta">conf ${fmtTime(origin.confirmed_time)}</span>`
                        : "")
                );
            }
            if (o.support_price != null) {
                return (
                    `<span class="src-type support">SUPPORT</span>` +
                    `<span class="src-price">₹${fmt(o.support_price)}</span>` +
                    (o.support_confirmed_fast
                        ? `<span class="src-meta">fast ${fmtTime(o.support_confirmed_fast)}</span>`
                        : "")
                );
            }
            return `<span class="src-type signal">SIGNAL</span>`;
        }

        function renderRow(o) {
            const status = String(o.status || "").toLowerCase();
            const scenario = String(o.scenario || "");
            const isPathB = String(o.path || "") === "B_external_trigger";
            const scnCls = scenario === "regime"
                ? "regime"
                : scenario === "path_b_sr_level"
                    ? "path_b_sr_level"
                    : "signal_v2";
            const scnLabel = scenario === "regime"
                ? "regime"
                : scenario === "path_b_sr_level"
                    ? "path b"
                    : "signal v2";
            const statusCls = status.replace(/_/g, "_");
            const statusLabel = status.replace(/_/g, " ");

            const entry = Number(o.entry_price);
            const stop = Number(o.stop_price);
            const target = Number(o.target_price);

            const closedPnl = Number(o.closed_pnl_pct);
            const curPnl = Number(o.current_pnl_pct);

            const pnlValue = Number.isFinite(closedPnl) ? closedPnl : curPnl;

            const pnlCls = Number.isFinite(pnlValue) ? (pnlValue >= 0 ? "up" : "down") : "";
            const rowCls = status === "open" ? "do-row-open" : "do-row-closed";

            // Source column
            const sourceHtml = buildSourceCell(o);

            // Lot badge (Path B only; Path A has no lot_size)
            const lotBadge = o.lot_size != null
                ? `<span class="do-lot-badge">${esc(o.lot_size)}L</span>`
                : "";

            // Entry price source subtext
            const priceSrcHtml = o.entry_price_source
                ? `<span class="do-price-src" title="${esc(o.entry_price_source)}">${esc(quoteSourceLabel(o.entry_price_source))}</span>`
                : "";

            // Target pivot time subtext (Path B only)
            const targetPivotHtml = o.target_pivot_time
                ? `<span class="do-price-src">@${fmtTime(o.target_pivot_time)}</span>`
                : "";

            // Path B badge on instrument cell
            const pathBadge = isPathB
                ? '<span class="do-path-b-badge">PATH B</span>'
                : "";

            // Rupee PnL subtext for closed orders
            let pnlRsSub = "";
            if (status !== "open") {
                const persisted = Number(o.closed_profit_rs);
                let rsVal = null;
                if (Number.isFinite(persisted)) {
                    rsVal = persisted;
                } else if (Number.isFinite(closedPnl) && Number.isFinite(entry) && entry > 0) {
                    const lot = Number(o.lot_size);
                    if (Number.isFinite(lot) && lot > 0) {
                        rsVal = entry * closedPnl * lot;
                    }
                }
                if (rsVal !== null && Number.isFinite(rsVal)) {
                    const rsCls = rsVal >= 0 ? "up" : "down";
                    pnlRsSub = `<span class="do-pnl-rs ${rsCls}">${fmtRs(rsVal)}</span>`;
                }
            }

            return `
                <tr class="${rowCls}">
                    <td>
                        <div style="font-weight:700;font-size:0.82rem;">
                            ${esc(o.trading_symbol || o.instrument_key || "")}
                            ${pathBadge}
                        </div>
                        <div style="font-size:0.7rem;color:var(--iso-muted)">${esc(o.instrument_key || "")}</div>
                    </td>
                    <td class="do-source-cell">${sourceHtml}</td>
                    <td>
                        <span class="do-scenario-pill ${scnCls}">${esc(scnLabel)}</span>
                        ${lotBadge}
                    </td>
                    <td>${esc(o.side || "—")}</td>
                    <td class="num">
                        ₹${fmt(entry)}
                        ${priceSrcHtml}
                    </td>
                    <td class="num">₹${fmt(stop)}</td>
                    <td class="num">
                        ₹${fmt(target)}
                        ${targetPivotHtml}
                    </td>
                    <td class="num">
                        ${Number.isFinite(Number(o.current_profit_rs))
                            ? `₹${Number(o.current_profit_rs).toFixed(2)}`
                            : "—"}
                        ${o.profit_state === "TRAILING"
                            ? `<div style="font-size:0.65rem;color:var(--iso-purple);">TRAIL ${o.trailing_stop_price ? "@ ₹" + Number(o.trailing_stop_price).toFixed(2) : ""}</div>`
                            : (o.profit_state === "PRE_TRAIL"
                                ? `<div style="font-size:0.65rem;color:var(--iso-muted);">PRE_TRAIL</div>`
                                : "")}
                    </td>
                    <td><span class="do-status-pill ${statusCls}">${esc(statusLabel)}</span></td>
                    <td class="num ${pnlCls}">
                        ${Number.isFinite(pnlValue) ? fmtPct(pnlValue) : "—"}
                        ${pnlRsSub}
                    </td>
                </tr>
            `;
        }

        function renderOrders(data) {
            if (!bodyEl) return;
            const orders = Array.isArray(data.orders) ? data.orders.slice() : [];

            if (!orders.length) {
                bodyEl.innerHTML = `
                    <div class="do-empty">
                        <i class="bi bi-hourglass-split"></i>
                        No dummy orders for today yet — they appear here as
                        soon as a candidate arms on the entry signal.
                    </div>`;
                return;
            }

            orders.sort((a, b) => {
                const aOpen = a.status === "OPEN" ? 0 : 1;
                const bOpen = b.status === "OPEN" ? 0 : 1;
                if (aOpen !== bOpen) return aOpen - bOpen;
                if (aOpen === 0) {
                    return String(a.entry_time || "").localeCompare(
                        String(a.entry_time || "")
                    );
                }
                return String(b.closed_at || "").localeCompare(
                    String(a.closed_at || "")
                );
            });

            bodyEl.innerHTML = `
                <div class="iso-table-wrap">
                    <table class="iso-table do-table">
                        <thead>
                            <tr>
                                <th>Instrument</th>
                                <th>Source</th>
                                <th>Scenario / Lot</th>
                                <th>Side</th>
                                <th class="num">Entry</th>
                                <th class="num">Stop</th>
                                <th class="num">Target</th>
                                <th class="num">₹ Profit</th>
                                <th>Status</th>
                                <th class="num">PnL</th>
                            </tr>
                        </thead>
                        <tbody>${orders.map(renderRow).join("")}</tbody>
                    </table>
                </div>
            `;
        }

        async function refresh() {
            try {
                const [stateRes, sumRes] = await Promise.all([
                    fetch(STATE_URL, { headers: { Accept: "application/json" } }),
                    fetch(SUMMARY_URL, { headers: { Accept: "application/json" } }),
                ]);
                if (!stateRes.ok || !sumRes.ok) throw new Error("HTTP error");
                const state = await stateRes.json();
                const summary = await sumRes.json();

                const orders = Array.isArray(state.orders) ? state.orders : [];

                renderSummary(summary, orders);
                renderOrders(state);

                const isPathB = orders.some(
                    o => String(o.path || "") === "B_external_trigger"
                );
                if (isPathB) {
                    setStatus(
                        state.strategy_locked_today
                            ? "LOCKED · Path B"
                            : "Path B · " + (summary.open_count || 0) + " open",
                        "post",
                    );
                } else {
                    const total = summary.total_today || 0;
                    const open = summary.open_count || 0;
                    if (total === 0) {
                        setStatus("Waiting", "");
                    } else if (open > 0) {
                        setStatus(`${open} open · ${total} total`, "has");
                    } else {
                        setStatus(`${total} closed`, "chosen");
                    }
                }

                if (lockdownBanner) {
                    lockdownBanner.style.display = state.strategy_locked_today
                        ? "block"
                        : "none";
                }
            } catch (e) {
                console.warn("[dummy-orders] refresh failed:", e);
                setStatus("Error", "error");
            }
        }

        function updateVisibility() {
            if (!card) return;
            const d = state && state.detail;
            if (d && d.isolated) {
                card.style.display = "block";
            } else {
                card.style.display = "none";
            }
        }

        updateVisibility();
        setInterval(updateVisibility, 1000);

        refresh();
        pollTimer = setInterval(refresh, POLL_MS);

        window.addEventListener("beforeunload", () => {
            if (pollTimer) clearInterval(pollTimer);
        });
    })();
</script>

<script>
    window.__INITIAL_DETAIL__ = {{ initial_detail|tojson|safe }};
</script>
{% endblock %}