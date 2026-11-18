(() => {
  const $ = id => document.getElementById(id);
  let selected = null;
  let searchTimer = null;
  let defaults = {quantity:65, product:"I", order_type:"MARKET", validity:"DAY", trading_enabled:false};
  const showEmpty = message => $("instrumentResults").innerHTML = '<div class="search-placeholder"><i class="bi bi-search"></i><span>' + escapeHtml(message) + '</span></div>';
  const renderResults = rows => {
    if (!rows.length) return showEmpty("No matching instruments found");
    $("instrumentResults").innerHTML = rows.map((item, index) => {
      const name = item.trading_symbol || item.short_name || item.name || "Instrument";
      const details = [item.segment, item.expiry, item.instrument_type, item.strike_price ? "Strike " + item.strike_price : null].filter(Boolean).join(" · ");
      return '<button type="button" class="instrument-option" data-index="' + index + '"><span><strong>' + escapeHtml(name) + '</strong><small>' + escapeHtml(details + " · " + (item.instrument_key || "")) + '</small></span><span class="instrument-tag">' + escapeHtml(item.exchange || item.segment || "SELECT") + '</span></button>';
    }).join("");
    $("instrumentResults")._rows = rows;
  };
  const search = async query => {
    if (query.trim().length < 2) return showEmpty("Type at least 2 characters to search");
    $("searchSpinner").classList.remove("d-none");
    try {
      const payload = await apiRequest("/api/v1/instruments/search?query=" + encodeURIComponent(query.trim()) + "&records=20");
      renderResults(payload.data || []);
    } catch (error) { showEmpty("Search failed: " + error.message); }
    finally { $("searchSpinner").classList.add("d-none"); }
  };
  $("instrumentQuery").addEventListener("input", event => {
    clearTimeout(searchTimer);
    const query = event.target.value;
    searchTimer = setTimeout(() => search(query), 320);
  });
  $("instrumentResults").addEventListener("click", event => {
    const button = event.target.closest("button[data-index]");
    if (!button) return;
    selected = $("instrumentResults")._rows[Number(button.dataset.index)];
    const name = selected.trading_symbol || selected.short_name || selected.name || "Selected instrument";
    $("selectedInstrument").classList.add("d-none");
    $("buyOrderForm").classList.remove("d-none");
    $("instrumentSummary").innerHTML = '<strong>' + escapeHtml(name) + '</strong><small>' + escapeHtml(selected.instrument_key) + '</small><small>' + escapeHtml([selected.segment, selected.expiry, selected.instrument_type, selected.lot_size ? "Lot size: " + selected.lot_size : null].filter(Boolean).join(" · ")) + '</small>';
    $("quantity").value = defaults.quantity || 65;
    $("tradingDisabledNotice").classList.toggle("d-none", defaults.trading_enabled);
    $("placeBuyBtn").disabled = !defaults.trading_enabled;
    $("placeBuyBtn").innerHTML = defaults.trading_enabled ? '<i class="bi bi-bag-check me-2"></i>Review and place BUY order' : '<i class="bi bi-shield-lock me-2"></i>Trading disabled';
  });
  $("buyOrderForm").addEventListener("submit", async event => {
    event.preventDefault();
    if (!selected) return showToast("Select an instrument first.", "error");
    if (!defaults.trading_enabled) return showToast("Trading is disabled in the server configuration.", "error");
    const quantity = Number($("quantity").value);
    if (!Number.isInteger(quantity) || quantity < 1) return showToast("Enter a valid whole-number quantity.", "error");
    const name = selected.trading_symbol || selected.name || selected.instrument_key;
    const message = "Confirm BUY order\n\nInstrument: " + name + "\nKey: " + selected.instrument_key + "\nQuantity: " + quantity + "\nOrder: MARKET · Intraday · DAY\n\nSubmit to Upstox?";
    if (!confirm(message)) return;
    $("placeBuyBtn").disabled = true;
    try {
      const response = await apiRequest("/api/v1/orders/place", {
        method:"POST",
        body:JSON.stringify({instrument_token:selected.instrument_key, trading_symbol:selected.trading_symbol || selected.short_name || selected.name, segment:selected.segment, quantity, transaction_type:"BUY", product:"I", order_type:"MARKET", validity:"DAY", price:0, slice:true})
      });
      showToast("BUY order submitted. Order IDs: " + ((response.order_ids || []).join(", ") || response.order_id || "See dashboard"), "success");
      console.info("Place order response", response);
    } catch (error) { showToast("Order was not submitted: " + error.message, "error"); }
    finally { $("placeBuyBtn").disabled = false; }
  });
  apiRequest("/api/v1/config/order-defaults").then(value => {
    defaults = value;
    $("quantity").value = defaults.quantity || 65;
    if (!defaults.trading_enabled) $("tradingDisabledNotice").classList.remove("d-none");
  }).catch(error => showToast("Could not load order defaults: " + error.message, "error"));
})();
