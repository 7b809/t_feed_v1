let instruments = [];
const byId = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

function render() {
  const query = byId("search").value.trim().toLowerCase();
  const rows = instruments.filter((item) => JSON.stringify(item).toLowerCase().includes(query));
  byId("jobs").innerHTML = rows.length ? rows.map((item, index) => `
    <article class="job">
      <div class="order">${index + 1}</div>
      <div class="identity"><strong>${escapeHtml(item.trading_symbol)}</strong><code>${escapeHtml(item.instrument_key)}</code></div>
      <span class="type ${escapeHtml((item.instrument_type || "").toLowerCase())}">${escapeHtml(item.instrument_type)}</span>
      <div class="facts"><span>Strike <b>${escapeHtml(item.strike_price)}</b></span><span>Expiry <b>${escapeHtml(item.expiry)}</b></span><span>Mode <b>${escapeHtml(item.mode)}</b></span></div>
    </article>`).join("") : '<div class="empty">No instruments found.</div>';
  byId("status").textContent = `${rows.length} shown`;
}

async function load(hardRefresh = false) {
  byId("refresh").disabled = true;
  byId("status").textContent = hardRefresh ? "Refreshing upstream..." : "Loading...";
  try {
    const response = await fetch(hardRefresh ? "/api/hard-refresh" : "/api/instruments", { method: hardRefresh ? "POST" : "GET" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Request failed");
    instruments = data.active || [];
    byId("unique").textContent = data.meta.unique_count;
    byId("duplicates").textContent = data.meta.duplicate_count;
    byId("invalid").textContent = data.meta.invalid_count;
    render();
  } catch (error) {
    byId("status").textContent = `Error: ${error.message}`;
  } finally {
    byId("refresh").disabled = false;
  }
}

byId("refresh").addEventListener("click", () => load(true));
byId("search").addEventListener("input", render);
load();
