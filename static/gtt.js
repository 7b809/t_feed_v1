(() => {
  const $ = id => document.getElementById(id);
  let selected = null, timer = null;
  const show = (message, kind="danger") => {
    const box=$("gttResponse"); box.className="alert alert-"+kind+" mt-3 mb-0"; box.textContent=message;
  };
  const updateQuantity = () => {
    const lots=Number($("gttLots").value||1), size=Number(selected?.lot_size||0);
    $("gttQtySummary").textContent = size>0 ? lots+" lot(s) × "+size+" units = "+(lots*size)+" quantity" : "Lot-size metadata unavailable. Cannot safely calculate quantity.";
    $("gttPlaceBtn").disabled = !selected || size<1;
  };
  const render = rows => {
    if (!rows.length) { $("gttResults").innerHTML='<div class="search-placeholder">No matching instruments found</div>'; return; }
    $("gttResults").innerHTML=rows.map((r,i)=>'<button type="button" class="instrument-option" data-i="'+i+'"><span><strong>'+escapeHtml(r.trading_symbol||r.short_name||r.name||"Instrument")+'</strong><small>'+escapeHtml([r.segment,r.expiry,r.instrument_key].filter(Boolean).join(" · "))+'</small></span><span class="instrument-tag">'+escapeHtml(r.lot_size ? "LOT "+r.lot_size : "NO LOT DATA")+'</span></button>').join("");
    $("gttResults")._rows=rows;
  };
  $("gttSearch").addEventListener("input", e=>{
    clearTimeout(timer); const q=e.target.value.trim();
    timer=setTimeout(async()=>{
      if(q.length<2){$("gttResults").innerHTML='<div class="search-placeholder">Type at least 2 characters</div>';return;}
      $("gttSpinner").classList.remove("d-none");
      try { const p=await apiRequest("/api/v1/instruments/search?query="+encodeURIComponent(q)+"&records=20"); render(p.data||[]); }
      catch(err){$("gttResults").innerHTML='<div class="search-placeholder">'+escapeHtml(err.message)+'</div>';}
      finally{$("gttSpinner").classList.add("d-none");}
    },300);
  });
  $("gttResults").addEventListener("click",e=>{
    const b=e.target.closest("button[data-i]"); if(!b)return;
    selected=$("gttResults")._rows[Number(b.dataset.i)];
    $("gttSelected").textContent=(selected.trading_symbol||selected.name||"Instrument")+" · "+selected.instrument_key;
    if(!selected.lot_size){show("This instrument search result did not include a lot_size. Please choose a result with lot-size metadata; quantity will not be guessed.","warning");}
    else $("gttResponse").classList.add("d-none");
    updateQuantity();
  });
  $("gttLots").addEventListener("input",updateQuantity);
  $("gttPlaceBtn").addEventListener("click",async()=>{
    if(!selected)return;
    const target=Number($("gttTarget").value), sl=Number($("gttStoploss").value);
    const entryRaw=$("gttEntry").value.trim(), entry=entryRaw===""?null:Number(entryRaw), lots=Number($("gttLots").value);
    if(!(target>0&&sl>0&&Number.isInteger(lots)&&lots>0))return show("Enter valid target, stop-loss and lot count.");
    if(entry!==null&&!(sl<entry&&entry<target))return show("For a BUY GTT, stop-loss must be below entry and entry below target.");
    if(!confirm("Place live BUY GTT?\n\nInstrument: "+(selected.trading_symbol||selected.instrument_key)+"\nEntry: "+(entry===null?"Current LTP (IMMEDIATE)":entry)+"\nTarget: "+target+"\nStop-loss: "+sl+"\nLots: "+lots+" × "+selected.lot_size+" = "+(lots*selected.lot_size)+" units\n\nTARGET and STOPLOSS legs are SELL orders. Ensure EDIS authorization is complete."))return;
    const btn=$("gttPlaceBtn"); btn.disabled=true; btn.textContent="Submitting to Upstox…";
    try{
      const r=await apiRequest("/api/v1/gtt/place",{method:"POST",body:JSON.stringify({instrument_key:selected.instrument_key,entry,target,stoploss:sl,lot:lots,lot_size:Number(selected.lot_size),product:$("gttProduct").value})});
      show("GTT placed. ID(s): "+((r.gtt_order_ids||[]).join(", ")||"See broker response")+" · Quantity "+r.quantity+" · Entry "+r.entry_price+" ("+r.entry_trigger_type+")","success");
    }catch(err){show("GTT not confirmed: "+err.message);}
    finally{btn.disabled=false;btn.innerHTML='<i class="bi bi-bullseye me-2"></i>Review and place GTT';}
  });
})();
