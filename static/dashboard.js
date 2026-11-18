(() => {
  const $ = id => document.getElementById(id);
  let currentDocument = {orders: []};
  let currentFilter = "all";
  const dateSelect = $("historyDate");
  const activeStatuses = new Set(["PLACED","PARTIALLY_FILLED","OPEN","PENDING","TRIGGER_PENDING","EXIT_REQUESTED"]);
  const fmt = value => value ? new Date(value).toLocaleString("en-IN", {hour12:true}) : "—";
  const statusClass = status => String(status || "").toLowerCase().replace(/[^a-z0-9_-]/g,"_");
  const idsFor = item => [...new Set([...(item.order_ids || []), item.order_id].filter(Boolean).map(String))];
  const isOrder = item => item.record_type === "ORDER";
  const eligibleExit = item => isOrder(item) && idsFor(item).length && !["BLOCKED","REJECTED","CANCELLED","EXIT_EXCEPTION"].includes(String(item.status).toUpperCase());
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
      $("ordersContainer").innerHTML = '<div class="empty-state"><i class="bi bi-inbox fs-5"></i><span>No orders found for this trading date.</span></div>';
      return;
    }
    const rows = filtered.map(item => {
      const ids = idsFor(item);
      const symbol = item.trading_symbol || item.instrument_name || item.instrument_token || item.action || item.record_type || "Order action";
      const sub = ids.join(", ") || item.local_id || "";
      const exit = eligibleExit(item) ? '<button class="action-btn exit" data-action="exit" data-ids="' + encodeURIComponent(JSON.stringify(ids)) + '"><i class="bi bi-box-arrow-up-right me-1"></i>Exit</button>' : "";
      const pending = ["PLACED","OPEN","PENDING","TRIGGER_PENDING","PARTIALLY_FILLED"].includes(String(item.status || "").toUpperCase());
      const cancel = isOrder(item) && pending && ids.length ? '<button class="action-btn" data-action="cancel" data-ids="' + encodeURIComponent(JSON.stringify(ids)) + '"><i class="bi bi-x-circle me-1"></i>Cancel</button>' : "";
      return '<tr><td><span class="symbol-cell">' + escapeHtml(symbol) + '</span><span class="sub-cell">' + escapeHtml(sub) + '</span></td><td>' + escapeHtml(item.transaction_type || item.action || "—") + '</td><td>' + escapeHtml(item.quantity ?? "—") + '</td><td><span class="status-pill ' + statusClass(item.status) + '">' + escapeHtml(item.status || "UNKNOWN") + '</span></td><td>' + escapeHtml(fmt(item.created_time || item.event_time)) + '</td><td>' + exit + cancel + '</td></tr>';
    }).join("");
    $("ordersContainer").innerHTML = '<div class="orders-table-wrap"><table class="orders-table"><thead><tr><th>Instrument / order ID</th><th>Side</th><th>Qty</th><th>Status</th><th>Created</th><th>Actions</th></tr></thead><tbody>' + rows + '</tbody></table></div>';
  };
  const loadDate = async value => {
    try {
      currentDocument = value === "today"
        ? await apiRequest("/api/v1/orders/today")
        : await apiRequest("/api/v1/orders/history?date=" + encodeURIComponent(value));
      render();
    } catch (error) {
      showToast("Could not load orders: " + error.message, "error");
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
    } catch (error) {
      showToast("Dashboard initialisation failed: " + error.message, "error");
    }
  };
  dateSelect.addEventListener("change", () => loadDate(dateSelect.value));
  $("refreshOrders").addEventListener("click", () => loadDate(dateSelect.value));
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
    const prompt = action === "exit" ? "Submit an opposite-side market order to exit the filled quantity for " + ids.join(", ") + "?" : "Cancel pending order(s) " + ids.join(", ") + "?";
    if (!confirm(prompt)) return;
    button.disabled = true;
    try {
      const result = action === "exit"
        ? await apiRequest("/api/v1/orders/exit-by-ids", {method:"POST",body:JSON.stringify({order_ids:ids})})
        : await apiRequest("/api/v1/orders/cancel/bulk", {method:"POST",body:JSON.stringify({order_ids:ids})});
      showToast(action === "exit" ? "Exit request submitted. Check the new order status." : "Cancellation request submitted.", "success");
      console.info("Order action response", result);
      await loadDate(dateSelect.value);
    } catch (error) { showToast(error.message, "error"); button.disabled = false; }
  });
  $("exitAllBtn").addEventListener("click", async () => {
    if (!confirm("This submits an exit-all-positions request to Upstox for the selected account. Continue?")) return;
    $("exitAllBtn").disabled = true;
    try {
      const result = await apiRequest("/api/v1/orders/exit-all", {method:"POST",body:JSON.stringify({})});
      showToast("Exit-all request submitted. Broker response recorded.", "success");
      console.info("Exit all response", result);
      await loadDate(dateSelect.value);
    } catch (error) { showToast(error.message, "error"); }
    finally { $("exitAllBtn").disabled = false; }
  });
  init();
})();
