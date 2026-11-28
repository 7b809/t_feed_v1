(function () {
  "use strict";
  var TODAY_URL = "/live/performance/isolation-events/today";
  var HISTORY_URL = "/live/performance/isolation-events/history";
  var PAGE_SIZE = 10;
  var activeView = "today";
  var page = 1;
  var busyToday = false;
  var busyHistory = false;

  function byId(id) { return document.getElementById(id); }
  function esc(value) {
    if (value === null || value === undefined || value === "") return "—";
    return String(value).replace(/[&<>"']/g, function (c) {
      return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
    });
  }
  function n(value) {
    if (value === null || value === undefined || value === "") return "—";
    var valueNum = Number(value);
    return Number.isFinite(valueNum) ? valueNum.toLocaleString("en-IN", {maximumFractionDigits:2}) : esc(value);
  }
  function dt(value) {
    if (!value) return "—";
    var d = new Date(value);
    return Number.isNaN(d.getTime()) ? esc(value) : d.toLocaleString("en-IN", {
      timeZone:"Asia/Kolkata", dateStyle:"medium", timeStyle:"medium"
    });
  }
  function setStatus(message, kind) {
    var node = byId("pv-archive-status");
    node.className = "pv-status " + kind;
    node.textContent = message;
  }
  function metric(label, value) {
    return '<div class="pv-event-metric"><span>' + esc(label) + '</span><strong>' + esc(value) + '</strong></div>';
  }
  function badge(value, extra) {
    if (!value) return "";
    return '<span class="pv-event-badge ' + (extra || "") + '">' + esc(value) + '</span>';
  }
  function eventCard(doc) {
    doc = doc || {};
    var opposite = doc.opposite_leg || {};
    var trend = doc.trend || (doc.explanation && doc.explanation.trend) || {};
    var explanation = doc.explanation || {};
    var touch = doc.isolation_touch_time || doc.execution_mode_decided_at || doc.isolation_created_at;
    var type = String(doc.option_type || doc.bias || "").toUpperCase();
    var tags = [
      badge(doc.status || "RECORDED"),
      badge(doc.trigger_side || "TRIGGER"),
      badge(doc.trigger_index_canonical || doc.trigger_index),
      badge(doc.bias || type, type.toLowerCase()),
      badge(doc.standard_execution_mode || "STANDARD")
    ].join("");
    var details = {
      explanation: explanation,
      level_ref: doc.level_ref,
      simultaneous_touches: doc.simultaneous_touches,
      trend: trend,
      nifty_routing: explanation.nifty_routing || doc.nifty_routing,
      strike_generation: explanation.strike_generation || doc.strike_generation,
      option_snapshot: explanation.option_snapshot || doc.option_snapshot,
      opposite_leg: opposite,
      supertrend_backfill: doc.supertrend_backfill
    };
    var summary = explanation.summary || doc.summary || "";
    return '<article class="pv-event-card">' +
      '<div class="pv-event-card-head"><div class="pv-event-card-top">' +
      '<span class="pv-event-time">' + esc(dt(touch)) + '</span>' +
      '<span class="pv-event-time">ID · ' + esc(doc.isolation_id || doc._id) + '</span></div>' +
      '<div class="pv-event-symbol">' + esc(doc.trading_symbol || doc.instrument_key || "Isolated instrument") + '</div>' +
      '<div class="pv-event-key">' + esc(doc.instrument_key) + ' · ' + esc(doc.underlying || doc.name) + '</div>' +
      '<div class="pv-event-badges">' + tags + '</div></div>' +
      '<div class="pv-event-metrics">' +
      metric("Trigger index", doc.trigger_index_canonical || doc.trigger_index) +
      metric("Trigger level", (doc.trigger_level_name || doc.trigger_side || "Level") + " · " + n(doc.trigger_level)) +
      metric("Trigger value", n(doc.trigger_value)) +
      metric("Index LTP at touch", n(doc.nifty_ltp_at_isolation || doc.routed_ltp)) +
      metric("Option LTP at isolation", n(doc.isolated_option_ltp || doc.execution_mode_price)) +
      metric("Strike / expiry", (doc.strike !== undefined ? n(doc.strike) : "—") + " · " + (doc.expiry || "—")) +
      metric("Trend / bias", (trend.trend || "—") + " · " + (trend.bias || doc.bias || "—")) +
      metric("Opposite leg", opposite.trading_symbol || opposite.instrument_key) +
      metric("Mode decision", doc.execution_mode_reason) +
      metric("Supertrend", (doc.supertrend_backfill && doc.supertrend_backfill.status) || "Not available") +
      '</div><div class="pv-event-foot"><details><summary>View complete event context' +
      (summary ? ' · ' + esc(summary) : '') +
      '</summary><pre class="pv-event-json">' + esc(JSON.stringify(details, null, 2)) +
      '\n\nFull source document:\n' + esc(JSON.stringify(doc, null, 2)) +
      '</pre></details></div></article>';
  }
  function renderEvents(items, history) {
    var list = byId("pv-archive-list");
    if (!items || !items.length) {
      list.innerHTML = '<div class="pv-archive-empty" style="grid-column:1/-1">' +
        (history ? "No historical isolation events match these filters." : "No isolation event has been saved for today yet. This section will refresh automatically.") +
        '</div>';
      return;
    }
    var html = "";
    var previousDate = "";
    items.forEach(function (doc) {
      var day = String(doc.trade_date_ist || "Unknown trading date");
      if (history && day !== previousDate) {
        html += '<div class="pv-archive-date">TRADING DATE · ' + esc(day) + '</div>';
        previousDate = day;
      }
      html += eventCard(doc);
    });
    list.innerHTML = html;
  }
  function setView(view) {
    activeView = view;
    byId("pv-archive-today-tab").setAttribute("aria-selected", view === "today" ? "true" : "false");
    byId("pv-archive-history-tab").setAttribute("aria-selected", view === "history" ? "true" : "false");
    byId("pv-archive-history-tools").hidden = view !== "history";
    byId("pv-archive-pagination").hidden = view !== "history";
    if (view === "today") loadToday();
    else { page = 1; loadHistory(); }
  }
  async function loadToday() {
    if (busyToday || activeView !== "today") return;
    busyToday = true;
    setStatus("Refreshing today's events", "waiting");
    try {
      var response = await fetch(TODAY_URL, {headers:{Accept:"application/json"}, cache:"no-store"});
      if (!response.ok) throw new Error("HTTP " + response.status);
      var data = await response.json();
      if (data.status === "unavailable" || data.status === "disabled") {
        throw new Error(data.error || "Isolation event reader is unavailable.");
      }
      if (activeView !== "today") return;
      renderEvents(data.events || [], false);
      byId("pv-archive-summary").textContent = (data.count || 0) + " event(s) saved for " + (data.date || "today") +
        (data.truncated ? " · showing latest " + (data.returned || 0) : "");
      byId("pv-archive-updated").textContent = "Checked " + dt(data.checked_at);
      setStatus(data.count ? "Today's events loaded" : "No event yet today", data.count ? "saved" : "waiting");
    } catch (error) {
      byId("pv-archive-list").innerHTML = '<div class="pv-archive-empty" style="grid-column:1/-1">' + esc(error.message || error) + '</div>';
      byId("pv-archive-summary").textContent = "Unable to load today's isolation events.";
      setStatus("Source unavailable", "error");
    } finally { busyToday = false; }
  }
  async function loadHistory() {
    if (busyHistory || activeView !== "history") return;
    busyHistory = true;
    setStatus("Loading history", "waiting");
    try {
      var params = new URLSearchParams({page:String(page), page_size:String(PAGE_SIZE)});
      var search = byId("pv-archive-search").value.trim();
      var tradeDate = byId("pv-archive-date").value;
      if (search) params.set("search", search);
      if (tradeDate) params.set("trade_date", tradeDate);
      var response = await fetch(HISTORY_URL + "?" + params.toString(), {headers:{Accept:"application/json"}, cache:"no-store"});
      if (!response.ok) throw new Error("HTTP " + response.status);
      var data = await response.json();
      if (data.status !== "ok") throw new Error(data.error || "Unable to load isolation history.");
      if (activeView !== "history") return;
      renderEvents(data.items || [], true);
      byId("pv-archive-summary").textContent = data.total + " matching event(s) · " +
        (data.source && data.source.collection ? data.source.collection : "isolation_events");
      byId("pv-archive-updated").textContent = "10 event cards per page";
      byId("pv-archive-page-label").textContent = "Page " + (data.page || page) + " of " + (data.total_pages || 0);
      byId("pv-archive-prev").disabled = !data.has_previous;
      byId("pv-archive-next").disabled = !data.has_next;
      byId("pv-archive-pagination").hidden = !(data.total > 0);
      setStatus("History loaded", "saved");
    } catch (error) {
      byId("pv-archive-list").innerHTML = '<div class="pv-archive-empty" style="grid-column:1/-1">' + esc(error.message || error) + '</div>';
      byId("pv-archive-summary").textContent = "Unable to load historical isolation events.";
      byId("pv-archive-pagination").hidden = true;
      setStatus("History unavailable", "error");
    } finally { busyHistory = false; }
  }

  byId("pv-archive-today-tab").addEventListener("click", function () { setView("today"); });
  byId("pv-archive-history-tab").addEventListener("click", function () { setView("history"); });
  byId("pv-archive-apply").addEventListener("click", function () { page = 1; loadHistory(); });
  byId("pv-archive-clear").addEventListener("click", function () {
    byId("pv-archive-search").value = "";
    byId("pv-archive-date").value = "";
    page = 1;
    loadHistory();
  });
  byId("pv-archive-search").addEventListener("keydown", function (event) {
    if (event.key === "Enter") { page = 1; loadHistory(); }
  });
  byId("pv-archive-prev").addEventListener("click", function () {
    if (page > 1) { page -= 1; loadHistory(); }
  });
  byId("pv-archive-next").addEventListener("click", function () { page += 1; loadHistory(); });

  loadToday();
  window.setInterval(function () {
    if (activeView === "today") loadToday();
  }, 15000);
})();
