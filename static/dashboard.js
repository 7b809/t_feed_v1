(() => {
  const $ = id => document.getElementById(id);
  let currentDocument = {orders: []};
  let currentFilter = "all";
  const dateSelect = $("historyDate");
  const activeStatuses = new Set(["PLACED","PARTIALLY_FILLED","OPEN","PENDING","TRIGGER_PENDING","EXIT_REQUESTED"]);
  const terminalGttStatuses = new Set(["CANCELLED","CANCELLED_UNTRIGGERED","CANCEL_SUBMITTED","COMPLETE","COMPLETED","EXPIRED","FAILED","REJECTED","INACTIVE"]);
  const fmt = value => value ? new Date(value).toLocaleString("en-IN", {hour12:true}) : "—";
  const statusClass = status => String(status || "").toLowerCase().replace(/[^a-z0-9_-]/g,"_");
  const money = value => value === null || value === undefined || !Number.isFinite(Number(value)) ? "—" :
    new Intl.NumberFormat("en-IN", {style:"currency",currency:"INR",maximumFractionDigits:2}).format(Number(value));
  const qty = value => value === null || value === undefined ? "—" : new Intl.NumberFormat("en-IN",{maximumFractionDigits:2}).format(Number(value));
  const idsFor = item => [...new Set([...(item.order_ids || []), item.order_id].filter(Boolean).map(String))];
  const gttIdsFor = item => [...new Set([...(item.gtt_order_ids || []), ...idsFor(item)].filter(id => String(id).startsWith("GTT-")).map(String))];
  const isOrder = item => item.record_type === "ORDER";
  const isGtt = item => item.record_type === "GTT";
  const eligibleExit = item => isOrder(item) && idsFor(item).length && !["BLOCKED","REJECTED","CANCELLED","EXIT_EXCEPTION"].includes(String(item.status).toUpperCase());

  const renderPortfolio = data => {
    $("todayPnl").textContent = money(data.today_pnl);
    $("totalPnl").textContent = money(data.total_pnl);
    $("realisedPnl").textContent = "Realised " + money(data.realised_pnl) + " · Unrealised " + money(data.unrealised_pnl);
    $("holdingsPnl").textContent = "Long-term holdings P&L " + money(data.holdings_pnl);
    $("portfolioUpdated").textContent = "Upstox broker snapshot · " + fmt(data.updated_at);
    $("positionsCount").textContent = data.positions_count ?? "—";
    $("holdingsCount").textContent = data.holdings_count ?? "—";
    $("todayPnl").classList.toggle("pnl-negative", Number(data.today_pnl) < 0);
    $("todayPnl").classList.toggle("pnl-positive", Number(data.today_pnl) >= 0);
    $("totalPnl").classList.toggle("pnl-negative", Number(data.total_pnl) < 0);
    $("totalPnl").classList.toggle("pnl-positive", Number(data.total_pnl) >= 0);

    const positions = data.positions || [];
    $("positionsBody").innerHTML = positions.length ? positions.map(row => {
      const symbol = row.trading_symbol || row.tradingsymbol || row.instrument_token || "—";
      return '<tr><td><strong>' + escapeHtml(symbol) + '</strong><small>' + escapeHtml(row.instrument_token || "") + '</small></td><td>' + escapeHtml(row.product || "—") + '</td><td>' + escapeHtml(qty(row.quantity)) + '</td><td>' + escapeHtml(money(row.average_price)) + '</td><td>' + escapeHtml(money(row.last_price)) + '</td><td>' + escapeHtml(money(row.realised)) + '</td><td>' + escapeHtml(money(row.unrealised)) + '</td></tr>';
    }).join("") : '<tr><td colspan="7" class="table-empty">' + escapeHtml(data.errors?.positions || "No current positions reported by Upstox.") + '</td></tr>';

    const holdings = data.holdings || [];
    $("holdingsBody").innerHTML = holdings.length ? holdings.map(row => {
      const symbol = row.trading_symbol || row.tradingsymbol || row.company_name || row.instrument_token || "—";
      return '<tr><td><strong>' + escapeHtml(symbol) + '</strong><small>' + escapeHtml(row.company_name || row.instrument_token || "") + '</small></td><td>' + escapeHtml(row.exchange || "—") + '</td><td>' + escapeHtml(row.product || "—") + '</td><td>' + escapeHtml(qty(row.quantity)) + '</td><td>' + escapeHtml(money(row.average_price)) + '</td><td>' + escapeHtml(money(row.last_price)) + '</td><td>' + escapeHtml(money(row.pnl)) + '</td></tr>';
    }).join("") : '<tr><td colspan="7" class="table-empty">' + escapeHtml(data.errors?.holdings || "No long-term holdings reported by Upstox.") + '</td></tr>';

    const errors = Object.values(data.errors || {});
    $("portfolioStatus").textContent = errors.length ? "Partial broker data: " + errors.join(" ") : "Live broker positions and holdings";
    $("portfolioStatus").classList.toggle("text-danger", errors.length > 0);
  };

  const loadPortfolio = async () => {
    $("portfolioStatus").textContent = "Refreshing live Upstox portfolio…";
    try {
      const data = await apiRequest("/api/v1/portfolio/overview");
      renderPortfolio(data);
    } catch (error) {
      $("portfolioStatus").textContent = "Portfolio unavailable: " + error.message;
      $("positionsBody").innerHTML = '<tr><td colspan="7" class="table-empty">Unable to load broker positions.</td></tr>';
      $("holdingsBody").innerHTML = '<tr><td colspan="7" class="table-empty">Unable to load broker holdings.</td></tr>';
      $("todayPnl").textContent = "—";
      $("totalPnl").textContent = "—";
      showToast("Could not refresh broker portfolio: " + error.message, "error");
    }
  };

  const render = () => {
    const orders = currentDocument.orders || [];
    $("metricTotal").textContent = dateSelect.value === "today" ? orders.filter(isOrder).length : "—";
    $("metricOpen").textContent = orders.filter(x => activeStatuses.has(String(x.status || "").toUpperCase()) && isOrder(x)).length;
    const filtered = orders.filter(item => {
      if (currentFilter === "all") return true;
      if (currentFilter === "EXIT") return String(item.record_type || "").startsWith("EXIT");
      if (currentFilter === "EXCEPTION") return ["EXCEPTION","PERSISTENCE_EXCEPTION"].includes(item.record_type) || String(item.status || "").includes("EXCEPTION") || item.status === "BLOCKED";
      return item.record_type === currentFilter;
    }).slice().reverse();
    if (!filtered.length) {
      $("ordersContainer").innerHTML = '<div class="empty-state"><i class="bi bi-inbox fs-5"></i><span>No project ledger activity found for this trading date.</span></div>';
      return;
    }
    const rows = filtered.map(item => {
      const ids = idsFor(item);
      const gttIds = gttIdsFor(item);
      const symbol = item.trading_symbol || item.instrument_name || item.instrument_token || item.action || item.record_type || "Order action";
      const sub = (gttIds.length ? gttIds : ids).join(", ") || item.local_id || "";
      const exit = eligibleExit(item) ? '<button class="action-btn exit" data-action="exit" data-ids="' + encodeURIComponent(JSON.stringify(ids)) + '"><i class="bi bi-box-arrow-up-right me-1"></i>Exit</button>' : "";
      const pending = ["PLACED","OPEN","PENDING","TRIGGER_PENDING","PARTIALLY_FILLED"].includes(String(item.status || "").toUpperCase());
      const cancel = isOrder(item) && pending && ids.length ? '<button class="action-btn" data-action="cancel" data-ids="' + encodeURIComponent(JSON.stringify(ids)) + '"><i class="bi bi-x-circle me-1"></i>Cancel</button>' : "";
      const gttPending = isGtt(item) && gttIds.length && !terminalGttStatuses.has(String(item.status || "").toUpperCase());
      const cancelGtt = gttPending ? '<button class="action-btn" data-action="gtt-cancel" data-ids="' + encodeURIComponent(JSON.stringify(gttIds)) + '"><i class="bi bi-x-circle me-1"></i>Cancel GTT</button>' : "";
      return '<tr><td><span class="symbol-cell">' + escapeHtml(symbol) + '</span><span class="sub-cell">' + escapeHtml(sub) + '</span></td><td>' + escapeHtml(item.transaction_type || item.action || (isGtt(item) ? "GTT" : "—")) + '</td><td>' + escapeHtml(item.quantity ?? "—") + '</td><td><span class="status-pill ' + statusClass(item.status) + '">' + escapeHtml(item.status || "UNKNOWN") + '</span></td><td>' + escapeHtml(fmt(item.created_time || item.event_time)) + '</td><td>' + exit + cancel + cancelGtt + '</td></tr>';
    }).join("");
    $("ordersContainer").innerHTML = '<div class="orders-table-wrap"><table class="orders-table"><thead><tr><th>Instrument / order ID</th><th>Side</th><th>Qty</th><th>Status</th><th>Created</th><th>Actions</th></tr></thead><tbody>' + rows + '</tbody></table></div>';
  };

  const loadDate = async value => {
    try {
      currentDocument = value === "today" ? await apiRequest("/api/v1/orders/today") :
        await apiRequest("/api/v1/orders/history?date=" + encodeURIComponent(value));
      render();
    } catch (error) {
      showToast("Could not load project order ledger: " + error.message, "error");
      $("ordersContainer").innerHTML = '<div class="empty-state">Unable to load this ledger.</div>';
    }
  };

  const init = async () => {
    try {
      const [dates, defaults, today] = await Promise.all([
        apiRequest("/api/v1/orders/dates"),
        apiRequest("/api/v1/config/order-defaults"),
        apiRequest("/api/v1/orders/today")
      ]);
      currentDocument = today;
      $("metricDays").textContent = dates.dates.length;
      dateSelect.innerHTML = '<option value="today">Today</option>' + dates.dates.map(d => '<option value="' + escapeHtml(d) + '">' + escapeHtml(d) + '</option>').join("");
      $("tradingState").textContent = defaults.trading_enabled ? "ENABLED" : "DISABLED";
      $("tradingState").style.color = defaults.trading_enabled ? "#54d8a0" : "#ffc477";
      $("environmentState").textContent = defaults.upstox_sandbox ? "Upstox sandbox selected" : "Live environment selected";
      render();
      await loadPortfolio();
    } catch (error) {
      showToast("Dashboard initialisation failed: " + error.message, "error");
    }
  };

  dateSelect.addEventListener("change", () => loadDate(dateSelect.value));
  $("refreshOrders").addEventListener("click", () => loadDate(dateSelect.value));
  $("refreshPortfolio").addEventListener("click", loadPortfolio);
  document.querySelectorAll(".filter-tab").forEach(button => button.addEventListener("click", () => {
    document.querySelectorAll(".filter-tab").forEach(x => x.classList.remove("active"));
    button.classList.add("active"); currentFilter = button.dataset.filter; render();
  }));

  $("ordersContainer").addEventListener("click", async event => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const ids = JSON.parse(decodeURIComponent(button.dataset.ids || "%5B%5D"));
    if (!ids.length) return;
    const action = button.dataset.action;
    const prompt = action === "exit" ? "Submit opposite-side market exit order(s) for " + ids.join(", ") + "?" :
      action === "gtt-cancel" ? "Cancel untriggered GTT order(s) " + ids.join(", ") + "? Triggered GTT orders require lifecycle cleanup." :
      "Cancel pending regular order(s) " + ids.join(", ") + "?";
    if (!confirm(prompt)) return;
    button.disabled = true;
    try {
      let result;
      if (action === "exit") {
        result = await apiRequest("/api/v1/orders/exit-by-ids", {method:"POST",body:JSON.stringify({order_ids:ids})});
      } else if (action === "gtt-cancel") {
        result = ids.length === 1
          ? await apiRequest("/api/v1/gtt/cancel", {method:"POST",body:JSON.stringify({gtt_order_id:ids[0]})})
          : await apiRequest("/api/v1/gtt/cancel/bulk", {method:"POST",body:JSON.stringify({gtt_order_ids:ids})});
      } else {
        result = await apiRequest("/api/v1/orders/cancel/bulk", {method:"POST",body:JSON.stringify({order_ids:ids})});
      }
      showToast(action === "exit" ? "Exit request submitted. Check broker position quantities." :
        action === "gtt-cancel" && result.failed ? "GTT cancellation had " + result.failed + " failure(s)." :
        "Cancellation request submitted.", action === "gtt-cancel" && result.failed ? "error" : "success");
      console.info("Order action response", result);
      await loadDate(dateSelect.value);
      if (action === "exit") await loadPortfolio();
    } catch (error) {
      showToast(error.message, "error");
      button.disabled = false;
    }
  });

  $("exitAllBtn").addEventListener("click", async () => {
    if (!confirm("This submits an exit-all-positions request to Upstox for the selected account. Continue?")) return;
    $("exitAllBtn").disabled = true;
    try {
      const result = await apiRequest("/api/v1/orders/exit-all", {method:"POST",body:JSON.stringify({})});
      showToast("Exit-all request submitted. Refresh the broker portfolio to verify remaining quantities.", "success");
      console.info("Exit all response", result);
      await loadDate(dateSelect.value);
      await loadPortfolio();
    } catch (error) { showToast(error.message, "error"); }
    finally { $("exitAllBtn").disabled = false; }
  });
  init();
})();
