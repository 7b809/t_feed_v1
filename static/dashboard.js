(() => {
  const $ = id => document.getElementById(id);

  /* ------------------------------------------------------------------
     Resolve dependencies WITHOUT shadowing them.
     We must not write `const apiRequest = ...` anywhere below, or the
     outer name gets TDZ-shadowed and `typeof apiRequest` throws.
  ------------------------------------------------------------------ */
  const safeApi = typeof apiRequest === "function" ? apiRequest : null;

  const esc = typeof escapeHtml === "function"
    ? escapeHtml
    : s => String(s ?? "").replace(/[&<>"']/g, c => (
        { "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c]
      ));

  const toast = typeof showToast === "function"
    ? showToast
    : (msg, kind) => {
        console[kind === "error" ? "error" : "log"]("[toast]", msg);
        const area = document.getElementById("toastArea");
        if (!area) return;
        const el = document.createElement("div");
        el.className = "toast-item " + (kind || "info");
        el.textContent = msg;
        area.appendChild(el);
        setTimeout(() => el.remove(), 4000);
      };

  if (!safeApi) {
    const status = $("portfolioStatus");
    if (status) {
      status.textContent = "CRITICAL: apiRequest() missing in /static/app.js (or loaded after dashboard.js)";
      status.classList.add("text-danger");
    }
    console.error("[dashboard] apiRequest is not a function — check /static/app.js load order & global export");
    return;
  }

  /* ---------- State ---------- */
  let currentDocument = { orders: [] };
  let currentFilter = "all";

  const dateSelect = $("historyDate");
  const activeStatuses = new Set(["PLACED","PARTIALLY_FILLED","OPEN","PENDING","TRIGGER_PENDING","EXIT_REQUESTED"]);
  const terminalGttStatuses = new Set(["CANCELLED","CANCELLED_UNTRIGGERED","CANCEL_SUBMITTED","COMPLETE","COMPLETED","EXPIRED","FAILED","REJECTED","INACTIVE"]);

  /* ---------- Helpers ---------- */
  const fmt = v => v ? new Date(v).toLocaleString("en-IN", { hour12: true }) : "—";
  const statusClass = s => String(s || "").toLowerCase().replace(/[^a-z0-9_-]/g, "_");
  const money = v => (v === null || v === undefined || !Number.isFinite(Number(v)))
    ? "—"
    : new Intl.NumberFormat("en-IN", { style:"currency", currency:"INR", maximumFractionDigits:2 }).format(Number(v));
  const qty = v => (v === null || v === undefined)
    ? "—"
    : new Intl.NumberFormat("en-IN", { maximumFractionDigits:2 }).format(Number(v));
  const num = v => Number.isFinite(Number(v)) ? Number(v) : 0;
  const pnlClass = v => num(v) < 0 ? "neg" : (num(v) > 0 ? "pos" : "");

  const idsFor = item => [...new Set([...(item.order_ids || []), item.order_id].filter(Boolean).map(String))];
  const gttIdsFor = item => [...new Set([
    ...(item.gtt_order_ids || []),
    ...idsFor(item)
  ].filter(id => String(id).startsWith("GTT-")).map(String))];
  const isOrder = item => item.record_type === "ORDER";
  const isGtt = item => item.record_type === "GTT";
  const eligibleExit = item => isOrder(item) && idsFor(item).length &&
    !["BLOCKED","REJECTED","CANCELLED","EXIT_EXCEPTION"].includes(String(item.status).toUpperCase());

  /* ===================== PORTFOLIO ===================== */

  const renderPnl = data => {
    try {
      if (!data) {
        ["todayPnl","profitTarget","guardState","positionsCountCard"].forEach(id => {
          const el = $(id); if (el) el.textContent = "—";
        });
        const detail = $("guardDetail"); if (detail) detail.textContent = "P&L unavailable";
        const foot   = $("realisedPnl"); if (foot) foot.textContent = "Realised — · Unrealised —";
        const gp     = $("guardPnl");    if (gp) gp.textContent = "Guard P&L —";
        const tp     = $("targetProgress"); if (tp) tp.style.width = "0%";
        const tw     = $("targetProgressWrap"); if (tw) tw.classList.remove("is-negative");
        return;
      }

      const total      = num(data.total_pnl);
      const realised   = num(data.realised_pnl);
      const unrealised = num(data.unrealised_pnl);
      const target     = num(data.daily_profit_target);
      const guardPnl   = num(data.guard_pnl);

      $("todayPnl").textContent = money(total);
      $("todayPnl").classList.toggle("pnl-negative", total < 0);
      $("todayPnl").classList.toggle("pnl-positive", total >= 0);
      $("realisedPnl").textContent = `Realised ${money(realised)} · Unrealised ${money(unrealised)}`;

      $("profitTarget").textContent = money(target);
      $("guardPnl").textContent = `Guard P&L ${money(guardPnl)}${data.include_unrealised ? " · incl. unrealised" : " · realised only"}`;

      const pct = target > 0 ? Math.max(0, Math.min(100, (guardPnl / target) * 100)) : 0;
      $("targetProgress").style.width = pct.toFixed(1) + "%";
      $("targetProgressWrap").classList.toggle("is-negative", guardPnl < 0);

      let label = "—", color = "";
      if (!data.guard_enabled)           { label = "GUARD OFF";       color = "#ffc477"; }
      else if (data.lockout_active)      { label = "LOCKOUT";         color = "#ff6b6b"; }
      else if (data.target_reached)      { label = "TARGET HIT";      color = "#54d8a0"; }
      else if (data.new_entries_allowed) { label = "ENTRIES OPEN";    color = "#54d8a0"; }
      else                               { label = "ENTRIES BLOCKED"; color = "#ff6b6b"; }
      $("guardState").textContent = label;
      $("guardState").style.color = color;

      const parts = [];
      if (data.trade_date) parts.push("Trade date " + data.trade_date);
      if (data.currency)   parts.push(data.currency);
      if (data.lockout)    parts.push("Lockout: " + (data.lockout.until || data.lockout.reason || "active"));
      $("guardDetail").textContent = parts.length ? parts.join(" · ") : "Guard metadata unavailable";

      $("positionsCountCard").textContent = data.positions_count ?? "—";
      if (data.updated_at) {
        $("portfolioUpdated").textContent = "Upstox broker snapshot · " + fmt(data.updated_at);
      }
    } catch (err) {
      console.error("[dashboard] renderPnl failed:", err);
    }
  };

  const renderPositions = data => {
    try {
      const list = (data && Array.isArray(data.data)) ? data.data : [];
      if (data) {
        $("positionsCount").textContent = data.count ?? list.length;
        $("positionsCountCard").textContent = data.count ?? list.length;
      }
      if (!list.length) {
        $("positionsBody").innerHTML =
          '<tr><td colspan="8" class="table-empty">No current positions reported by Upstox.</td></tr>';
        return;
      }
      $("positionsBody").innerHTML = list.map(row => {
        const symbol  = row.trading_symbol || row.tradingsymbol || row.instrument_token || "—";
        const token   = row.instrument_token || "";
        const avg     = row.buy_price ?? row.average_price ?? 0;
        const totalPn = row.pnl ?? (num(row.realised) + num(row.unrealised));
        const dayBuy  = row.day_buy_quantity ?? 0;
        const daySell = row.day_sell_quantity ?? 0;
        return '<tr>' +
          '<td><strong>' + esc(symbol) + '</strong><small>' + esc(token) + '</small></td>' +
          '<td>' + esc(row.product || "—") + '</td>' +
          '<td>' + esc(qty(row.quantity)) + '</td>' +
          '<td>' + esc(qty(dayBuy)) + ' / ' + esc(qty(daySell)) + '</td>' +
          '<td>' + esc(money(avg)) + ' / ' + esc(money(row.last_price)) + '</td>' +
          '<td class="cell-pnl ' + pnlClass(row.realised) + '">' + esc(money(row.realised)) + '</td>' +
          '<td class="cell-pnl ' + pnlClass(row.unrealised) + '">' + esc(money(row.unrealised)) + '</td>' +
          '<td class="cell-pnl ' + pnlClass(totalPn) + '"><strong>' + esc(money(totalPn)) + '</strong></td>' +
          '</tr>';
      }).join("");
    } catch (err) {
      console.error("[dashboard] renderPositions failed:", err);
      $("positionsBody").innerHTML =
        '<tr><td colspan="8" class="table-empty">Position render error — see console.</td></tr>';
    }
  };

  const renderHoldings = data => {
    try {
      const list = (data && Array.isArray(data.data)) ? data.data : [];
      if (data) {
        $("holdingsCount").textContent = data.count ?? list.length;
        $("holdingsCountCard").textContent = "Holdings " + (data.count ?? list.length);
      }
      if (!list.length) {
        $("holdingsBody").innerHTML =
          '<tr><td colspan="7" class="table-empty">No long-term holdings reported by Upstox.</td></tr>';
        return;
      }
      $("holdingsBody").innerHTML = list.map(row => {
        const symbol = row.trading_symbol || row.tradingsymbol || row.company_name || row.instrument_token || "—";
        const sub    = row.company_name || row.instrument_token || "";
        const hPnl   = row.pnl ?? row.unrealised ?? null;
        return '<tr>' +
          '<td><strong>' + esc(symbol) + '</strong><small>' + esc(sub) + '</small></td>' +
          '<td>' + esc(row.exchange || "—") + '</td>' +
          '<td>' + esc(row.product || "—") + '</td>' +
          '<td>' + esc(qty(row.quantity)) + '</td>' +
          '<td>' + esc(money(row.average_price)) + '</td>' +
          '<td>' + esc(money(row.last_price)) + '</td>' +
          '<td class="cell-pnl ' + pnlClass(hPnl) + '"><strong>' + esc(money(hPnl)) + '</strong></td>' +
          '</tr>';
      }).join("");
    } catch (err) {
      console.error("[dashboard] renderHoldings failed:", err);
      $("holdingsBody").innerHTML =
        '<tr><td colspan="7" class="table-empty">Holdings render error — see console.</td></tr>';
    }
  };

  const loadPortfolio = async () => {
    $("portfolioStatus").textContent = "Refreshing live Upstox portfolio…";
    $("portfolioStatus").classList.remove("text-danger");
    console.log("[dashboard] fetching portfolio…");

    const [posR, holdR, pnlR] = await Promise.allSettled([
      safeApi("/api/v1/portfolio/positions"),
      safeApi("/api/v1/portfolio/holdings"),
      safeApi("/api/v1/portfolio/today-pnl")
    ]);

    console.log("[dashboard] positions:",  posR.status,  posR.status  === "fulfilled" ? posR.value  : posR.reason);
    console.log("[dashboard] holdings:",   holdR.status, holdR.status === "fulfilled" ? holdR.value : holdR.reason);
    console.log("[dashboard] today-pnl:",  pnlR.status,  pnlR.status  === "fulfilled" ? pnlR.value  : pnlR.reason);

    const failed = [];
    renderPnl      (pnlR.status  === "fulfilled" ? pnlR.value  : (failed.push("P&L"),       null));
    renderPositions(posR.status  === "fulfilled" ? posR.value  : (failed.push("positions"), null));
    renderHoldings (holdR.status === "fulfilled" ? holdR.value : (failed.push("holdings"),  null));

    if (posR.status  === "rejected") $("positionsBody").innerHTML =
      '<tr><td colspan="8" class="table-empty">Unable to load broker positions.</td></tr>';
    if (holdR.status === "rejected") $("holdingsBody").innerHTML =
      '<tr><td colspan="7" class="table-empty">Unable to load broker holdings.</td></tr>';

    if (failed.length) {
      $("portfolioStatus").textContent = "Partial broker data — failed: " + failed.join(", ");
      $("portfolioStatus").classList.add("text-danger");
      toast("Portfolio partial: " + failed.join(", ") + " failed", "error");
    } else {
      $("portfolioStatus").textContent = "Live broker positions, holdings and P&L";
      if (pnlR.status === "fulfilled" && pnlR.value?.updated_at) {
        $("portfolioUpdated").textContent = "Upstox broker snapshot · " + fmt(pnlR.value.updated_at);
      }
    }
  };

  /* ===================== LEDGER ===================== */

  const render = () => {
    try {
      const orders = currentDocument.orders || [];
      $("metricTotal").textContent = dateSelect.value === "today" ? orders.filter(isOrder).length : "—";
      $("metricOpen").textContent  = orders.filter(x =>
        activeStatuses.has(String(x.status || "").toUpperCase()) && isOrder(x)).length;

      const filtered = orders.filter(item => {
        if (currentFilter === "all") return true;
        if (currentFilter === "EXIT") return String(item.record_type || "").startsWith("EXIT");
        if (currentFilter === "EXCEPTION") {
          return ["EXCEPTION","PERSISTENCE_EXCEPTION"].includes(item.record_type) ||
                 String(item.status || "").includes("EXCEPTION") || item.status === "BLOCKED";
        }
        return item.record_type === currentFilter;
      }).slice().reverse();

      if (!filtered.length) {
        $("ordersContainer").innerHTML =
          '<div class="empty-state"><i class="bi bi-inbox fs-5"></i><span>No project ledger activity found for this trading date.</span></div>';
        return;
      }

      const rows = filtered.map(item => {
        const ids    = idsFor(item);
        const gttIds = gttIdsFor(item);
        const symbol = item.trading_symbol || item.instrument_name || item.instrument_token ||
                       item.action || item.record_type || "Order action";
        const sub    = (gttIds.length ? gttIds : ids).join(", ") || item.local_id || "";

        const exit = eligibleExit(item)
          ? '<button class="action-btn exit" data-action="exit" data-ids="' + encodeURIComponent(JSON.stringify(ids)) + '"><i class="bi bi-box-arrow-up-right me-1"></i>Exit</button>' : "";
        const pending = ["PLACED","OPEN","PENDING","TRIGGER_PENDING","PARTIALLY_FILLED"]
          .includes(String(item.status || "").toUpperCase());
        const cancel = isOrder(item) && pending && ids.length
          ? '<button class="action-btn" data-action="cancel" data-ids="' + encodeURIComponent(JSON.stringify(ids)) + '"><i class="bi bi-x-circle me-1"></i>Cancel</button>' : "";
        const gttPending = isGtt(item) && gttIds.length &&
          !terminalGttStatuses.has(String(item.status || "").toUpperCase());
        const cancelGtt = gttPending
          ? '<button class="action-btn" data-action="gtt-cancel" data-ids="' + encodeURIComponent(JSON.stringify(gttIds)) + '"><i class="bi bi-x-circle me-1"></i>Cancel GTT</button>' : "";

        return '<tr>' +
          '<td><span class="symbol-cell">' + esc(symbol) + '</span><span class="sub-cell">' + esc(sub) + '</span></td>' +
          '<td>' + esc(item.transaction_type || item.action || (isGtt(item) ? "GTT" : "—")) + '</td>' +
          '<td>' + esc(item.quantity ?? "—") + '</td>' +
          '<td><span class="status-pill ' + statusClass(item.status) + '">' + esc(item.status || "UNKNOWN") + '</span></td>' +
          '<td>' + esc(fmt(item.created_time || item.event_time)) + '</td>' +
          '<td>' + exit + cancel + cancelGtt + '</td>' +
          '</tr>';
      }).join("");

      $("ordersContainer").innerHTML =
        '<div class="orders-table-wrap"><table class="orders-table">' +
        '<thead><tr><th>Instrument / order ID</th><th>Side</th><th>Qty</th><th>Status</th><th>Created</th><th>Actions</th></tr></thead>' +
        '<tbody>' + rows + '</tbody></table></div>';
    } catch (err) {
      console.error("[dashboard] render (ledger) failed:", err);
      $("ordersContainer").innerHTML = '<div class="empty-state">Ledger render error — see console.</div>';
    }
  };

  const loadDate = async value => {
    try {
      currentDocument = value === "today"
        ? await safeApi("/api/v1/orders/today")
        : await safeApi("/api/v1/orders/history?date=" + encodeURIComponent(value));
      render();
    } catch (error) {
      console.error("[dashboard] loadDate failed:", error);
      toast("Could not load project order ledger: " + error.message, "error");
      $("ordersContainer").innerHTML = '<div class="empty-state">Unable to load this ledger.</div>';
    }
  };

  /* ===================== INIT ===================== */

  const init = async () => {
    loadPortfolio();

    const [datesR, defaultsR, todayR] = await Promise.allSettled([
      safeApi("/api/v1/orders/dates"),
      safeApi("/api/v1/config/order-defaults"),
      safeApi("/api/v1/orders/today")
    ]);
    console.log("[dashboard] orders/dates:",          datesR.status,    datesR.status    === "fulfilled" ? datesR.value    : datesR.reason);
    console.log("[dashboard] config/order-defaults:", defaultsR.status, defaultsR.status === "fulfilled" ? defaultsR.value : defaultsR.reason);
    console.log("[dashboard] orders/today:",         todayR.status,    todayR.status    === "fulfilled" ? todayR.value    : todayR.reason);

    if (datesR.status === "fulfilled" && Array.isArray(datesR.value?.dates)) {
      $("metricDays").textContent = datesR.value.dates.length;
      dateSelect.innerHTML = '<option value="today">Today</option>' +
        datesR.value.dates.map(d =>
          '<option value="' + esc(d) + '">' + esc(d) + '</option>'
        ).join("");
    } else {
      $("metricDays").textContent = "—";
    }

    if (defaultsR.status === "fulfilled" && defaultsR.value) {
      const def = defaultsR.value;
      $("tradingState").textContent = def.trading_enabled ? "ENABLED" : "DISABLED";
      $("tradingState").style.color = def.trading_enabled ? "#54d8a0" : "#ffc477";
      $("environmentState").textContent = def.upstox_sandbox
        ? "Upstox sandbox selected" : "Live environment selected";
    } else {
      $("tradingState").textContent = "UNKNOWN";
      $("environmentState").textContent = "Defaults unavailable";
    }

    if (todayR.status === "fulfilled" && todayR.value) {
      currentDocument = todayR.value;
    } else {
      currentDocument = { orders: [] };
      toast("Ledger bootstrap failed: " +
        (todayR.status === "rejected" ? todayR.reason.message : "unknown"), "error");
    }
    render();
  };

  /* ===================== EVENTS ===================== */

  dateSelect.addEventListener("change", () => loadDate(dateSelect.value));
  $("refreshOrders").addEventListener("click", () => loadDate(dateSelect.value));
  $("refreshPortfolio").addEventListener("click", loadPortfolio);

  document.querySelectorAll(".filter-tab").forEach(button =>
    button.addEventListener("click", () => {
      document.querySelectorAll(".filter-tab").forEach(x => x.classList.remove("active"));
      button.classList.add("active");
      currentFilter = button.dataset.filter;
      render();
    })
  );

  $("ordersContainer").addEventListener("click", async event => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const ids = JSON.parse(decodeURIComponent(button.dataset.ids || "%5B%5D"));
    if (!ids.length) return;
    const action = button.dataset.action;
    const prompt =
      action === "exit" ? "Submit opposite-side market exit order(s) for " + ids.join(", ") + "?" :
      action === "gtt-cancel" ? "Cancel untriggered GTT order(s) " + ids.join(", ") + "?" :
      "Cancel pending regular order(s) " + ids.join(", ") + "?";
    if (!confirm(prompt)) return;
    button.disabled = true;
    try {
      let result;
      if (action === "exit") {
        result = await safeApi("/api/v1/orders/exit-by-ids", { method:"POST", body: JSON.stringify({ order_ids: ids }) });
      } else if (action === "gtt-cancel") {
        result = ids.length === 1
          ? await safeApi("/api/v1/gtt/cancel",      { method:"POST", body: JSON.stringify({ gtt_order_id: ids[0] }) })
          : await safeApi("/api/v1/gtt/cancel/bulk", { method:"POST", body: JSON.stringify({ gtt_order_ids: ids }) });
      } else {
        result = await safeApi("/api/v1/orders/cancel/bulk", { method:"POST", body: JSON.stringify({ order_ids: ids }) });
      }
      toast("Request submitted.", "success");
      console.info("[dashboard] action response", result);
      await loadDate(dateSelect.value);
      if (action === "exit") await loadPortfolio();
    } catch (error) {
      toast(error.message, "error");
      button.disabled = false;
    }
  });

  $("exitAllBtn").addEventListener("click", async () => {
    if (!confirm("This submits an exit-all-positions request to Upstox. Continue?")) return;
    $("exitAllBtn").disabled = true;
    try {
      const result = await safeApi("/api/v1/orders/exit-all", { method:"POST", body: JSON.stringify({}) });
      toast("Exit-all submitted.", "success");
      console.info("[dashboard] exit-all response", result);
      await loadDate(dateSelect.value);
      await loadPortfolio();
    } catch (error) {
      toast(error.message, "error");
    } finally {
      $("exitAllBtn").disabled = false;
    }
  });

  init();
})();