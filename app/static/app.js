// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let instruments = [];

let feedStatus = {
  backend: null,
  running: false,
  interval_seconds: 60,
  feed_url: null,
  total_instruments: 0,
  connected_instruments: 0,
  last_tick_at: null,
  instruments: {},
};

const FEED_POLL_MS = 3000;
let feedPollHandle = null;

// ---------------------------------------------------------------------------
// DOM helpers
// ---------------------------------------------------------------------------
const byId = (id) => document.getElementById(id);

const escapeHtml = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[c]));

// ---------------------------------------------------------------------------
// Underlying normalisation (mirrors app/services/instrument_paths.py)
// ---------------------------------------------------------------------------
const INDEX_ALIASES = {
  nifty50: "nifty",
  nifty: "nifty",
  banknifty: "banknifty",
  finnifty: "finnifty",
  midcpnifty: "midcpnifty",
  sensex: "sensex",
  bankex: "bankex",
};

const DISPLAY_NAMES = {
  nifty: "NIFTY",
  banknifty: "BANK NIFTY",
  finnifty: "FIN NIFTY",
  midcpnifty: "MIDCP NIFTY",
  sensex: "SENSEX",
  bankex: "BANKEX",
  unknown: "OTHER",
};

function stripIndexPrefix(value) {
  if (typeof value !== "string") return "";
  const idx = value.indexOf("|");
  return idx >= 0 ? value.slice(idx + 1) : value;
}

function normalizeUnderlying(item) {
  if (!item || typeof item !== "object") return "unknown";

  // 1) Explicit fields
  for (const key of ["underlying", "underlying_symbol", "asset", "root_symbol"]) {
    const v = item[key];
    if (typeof v === "string" && v.trim()) {
      return normalizeName(v);
    }
  }

  // 2) underlying_key, e.g. "NSE_INDEX|Nifty 50"
  const uk = item.underlying_key;
  if (typeof uk === "string" && uk.trim()) {
    return normalizeName(stripIndexPrefix(uk));
  }

  // 3) trading_symbol prefix
  const symbol = String(item.trading_symbol || "").toUpperCase();
  for (const name of ["MIDCPNIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "NIFTY"]) {
    if (symbol.startsWith(name)) {
      return normalizeName(name);
    }
  }

  // 4) Fall back to instrument_key when it is an index row
  const ikey = String(item.instrument_key || "");
  if (ikey.startsWith("NSE_INDEX|") || ikey.startsWith("BSE_INDEX|")) {
    return normalizeName(stripIndexPrefix(ikey));
  }

  return "unknown";
}

function normalizeName(name) {
  const raw = String(name || "")
    .trim()
    .toLowerCase()
    .replace(/[ _]/g, "");
  if (!raw) return "unknown";
  return INDEX_ALIASES[raw] || raw;
}

function displayUnderlying(key) {
  return DISPLAY_NAMES[key] || key.toUpperCase();
}

// ---------------------------------------------------------------------------
// Grouping
// ---------------------------------------------------------------------------
function groupInstruments(list) {
  const groups = new Map();
  for (const item of list) {
    const key = normalizeUnderlying(item);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  }
  return groups;
}

// ---------------------------------------------------------------------------
// Feed status helpers
// ---------------------------------------------------------------------------
function feedEntryFor(instrumentKey) {
  if (!instrumentKey) return null;
  return feedStatus.instruments[instrumentKey] || null;
}

const STATUS_META = {
  connected:  { label: "LIVE",       cls: "text-bg-success", pulse: true  },
  connecting: { label: "CONNECTING", cls: "text-bg-warning", pulse: true  },
  pending:    { label: "PENDING",    cls: "text-bg-warning", pulse: false },
  closed:     { label: "CLOSED",     cls: "text-bg-secondary", pulse: false },
  stopped:    { label: "STOPPED",    cls: "text-bg-secondary", pulse: false },
  error:      { label: "ERROR",      cls: "text-bg-danger",  pulse: false },
  offline:    { label: "OFFLINE",    cls: "text-bg-secondary", pulse: false },
};

function feedStatusMeta(status) {
  return STATUS_META[status] || STATUS_META.offline;
}

function fmtLtp(value) {
  const n = Number(value);
  return isFinite(n) ? n.toFixed(2) : "—";
}

function fmtRelative(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (!isFinite(then)) return "";
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------
function render() {
  const query = byId("search").value.trim().toLowerCase();

  const filtered = instruments.filter((item) =>
    JSON.stringify(item).toLowerCase().includes(query)
  );

  const groups = groupInstruments(filtered);

  // Sort groups: known underlyings first (alphabetical), then "unknown".
  const sortedKeys = [...groups.keys()].sort((a, b) => {
    if (a === "unknown") return 1;
    if (b === "unknown") return -1;
    return a.localeCompare(b);
  });

  const html = sortedKeys
    .map((key) => renderGroup(key, groups.get(key)))
    .join("");

  byId("jobs").innerHTML =
    html ||
    `<div class="card border-0 shadow-sm">
       <div class="card-body text-center text-secondary py-5">
         <i class="bi bi-inbox fs-1 d-block mb-2"></i>
         No instruments found.
       </div>
     </div>`;

  byId("status").textContent = `${filtered.length} shown`;
}

function renderGroup(underlyingKey, list) {
  const title = displayUnderlying(underlyingKey);
  const liveCount = list.filter(
    (i) => (feedEntryFor(i.instrument_key)?.status) === "connected"
  ).length;

  return `
    <div class="card border-0 shadow-sm mb-3">
      <div class="card-header bg-body-secondary d-flex justify-content-between align-items-center">
        <div class="d-flex align-items-center gap-2">
          <i class="bi bi-bar-chart-line text-info"></i>
          <span class="fw-bold text-uppercase">${escapeHtml(title)}</span>
          <span class="badge text-bg-secondary">${list.length}</span>
        </div>
        <span class="small text-secondary">
          <i class="bi bi-broadcast me-1"></i>
          ${liveCount} live
        </span>
      </div>

      <div class="table-responsive">
        <table class="table table-hover align-middle mb-0">
          <thead class="table-group-divider">
            <tr class="text-secondary text-uppercase small">
              <th style="width:52px;">#</th>
              <th>Symbol</th>
              <th>Instrument key</th>
              <th style="width:80px;">Type</th>
              <th class="text-end" style="width:100px;">Strike</th>
              <th style="width:120px;">Expiry</th>
              <th class="text-end" style="width:120px;">LTP</th>
              <th class="text-end" style="width:90px;">Ticks</th>
              <th style="width:150px;">Feed</th>
            </tr>
          </thead>
          <tbody>
            ${list.map((item, i) => renderRow(item, i)).join("")}
          </tbody>
        </table>
      </div>
    </div>`;
}

function renderRow(item, index) {
  const entry = feedEntryFor(item.instrument_key);
  const status = entry?.status || (feedStatus.backend === "rest" ? "offline" : "offline");
  const meta = feedStatusMeta(status);

  const isLive = status === "connected";
  const ltp = entry?.last_ltp ?? null;
  const ticks = entry?.total_ticks ?? 0;
  const lastTickAt = entry?.last_tick_at ?? null;

  const ltpClass = isLive ? "text-success fw-bold tabular" : "text-secondary tabular";

  const badgeIcon = meta.pulse
    ? `<span class="pulse-dot"></span>`
    : `<i class="bi bi-circle-fill" style="font-size:.5rem;"></i>`;

  const typeClass =
    item.instrument_type === "CE" ? "text-bg-success" :
    item.instrument_type === "PE" ? "text-bg-danger"  :
    item.instrument_type === "FUT" ? "text-bg-primary" :
    "text-bg-secondary";

  return `
    <tr class="${isLive ? "row-live" : ""}">
      <td class="text-secondary small">${index + 1}</td>
      <td class="fw-semibold">${escapeHtml(item.trading_symbol || "—")}</td>
      <td><code class="small text-secondary">${escapeHtml(item.instrument_key || "—")}</code></td>
      <td>
        ${
          item.instrument_type
            ? `<span class="badge ${typeClass}">${escapeHtml(item.instrument_type)}</span>`
            : `<span class="text-secondary">—</span>`
        }
      </td>
      <td class="text-end tabular">${escapeHtml(item.strike_price ?? "—")}</td>
      <td class="small text-secondary">${escapeHtml(item.expiry ?? "—")}</td>
      <td class="text-end ${ltpClass}">${escapeHtml(fmtLtp(ltp))}</td>
      <td class="text-end small text-secondary tabular">
        ${ticks ? escapeHtml(String(ticks)) : "—"}
        ${lastTickAt ? `<div class="tick-age">${escapeHtml(fmtRelative(lastTickAt))}</div>` : ""}
      </td>
      <td>
        <span class="badge ${meta.cls} d-inline-flex align-items-center gap-1">
          ${badgeIcon}${escapeHtml(meta.label)}
        </span>
      </td>
    </tr>`;
}

// ---------------------------------------------------------------------------
// Loaders
// ---------------------------------------------------------------------------
async function load(hardRefresh = false) {
  byId("refresh").disabled = true;
  byId("status").textContent = hardRefresh ? "Refreshing upstream…" : "Loading…";

  try {
    const response = await fetch(
      hardRefresh ? "/api/hard-refresh" : "/api/instruments",
      { method: hardRefresh ? "POST" : "GET" }
    );
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Request failed");

    instruments = data.active || [];
    byId("unique").textContent     = data.meta.unique_count;
    byId("duplicates").textContent = data.meta.duplicate_count;
    byId("invalid").textContent    = data.meta.invalid_count;
    render();
  } catch (error) {
    byId("status").textContent = `Error: ${error.message}`;
  } finally {
    byId("refresh").disabled = false;
  }
}

async function loadFeedStatus() {
  try {
    const res = await fetch("/api/live-ema/feed-status", { cache: "no-store" });
    if (!res.ok) return;

    const data = await res.json();
    feedStatus = {
      backend: data.backend || null,
      running: !!data.running,
      interval_seconds: data.interval_seconds || 60,
      feed_url: data.feed_url || null,
      total_instruments: data.total_instruments || 0,
      connected_instruments: data.connected_instruments || 0,
      last_tick_at: data.last_tick_at || null,
      instruments: data.instruments || {},
    };

    // ----- Header badge -----
    const headerBadge = byId("live-indicator");
    let headerClass = "badge rounded-pill";
    let headerHtml;

    if (!feedStatus.running) {
      headerClass += " text-bg-secondary";
      headerHtml = `<i class="bi bi-plug me-1"></i>FEED OFFLINE`;
    } else if (feedStatus.backend === "rest") {
      headerClass += " text-bg-warning";
      headerHtml = `<i class="bi bi-arrow-repeat me-1"></i>REST MODE`;
    } else {
      headerClass += " text-bg-success";
      headerHtml = `<span class="pulse-dot me-1"></span>` +
                   `LIVE · ${feedStatus.connected_instruments}/${feedStatus.total_instruments}`;
    }

    headerBadge.className = headerClass;
    headerBadge.innerHTML = headerHtml;

    // ----- Live-feeds stat card -----
    byId("live-count").textContent = feedStatus.running
      ? `${feedStatus.connected_instruments}/${feedStatus.total_instruments}`
      : "0";

    // ----- Re-render grouped tables -----
    render();
  } catch {
    // Silent — a flaky poll should not disturb the UI.
  }
}

// ---------------------------------------------------------------------------
// Polling
// ---------------------------------------------------------------------------
function startFeedPolling() {
  if (feedPollHandle) clearInterval(feedPollHandle);
  feedPollHandle = setInterval(loadFeedStatus, FEED_POLL_MS);
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------
byId("refresh").addEventListener("click", () => load(true));
byId("search").addEventListener("input", render);

load();
loadFeedStatus();
startFeedPolling();