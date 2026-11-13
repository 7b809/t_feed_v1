(function () {
  "use strict";
  var SNAPSHOT_URL = "/live/performance/today-snapshot";
  var INDEXES = [
    {name:"NIFTY 50",key:"NSE_INDEX|Nifty 50"},
    {name:"BANKNIFTY",key:"NSE_INDEX|Nifty Bank"},
    {name:"MIDCPNIFTY",key:"NSE_INDEX|NIFTY MID SELECT"},
    {name:"SENSEX",key:"BSE_INDEX|SENSEX"}
  ];
  var busy = false;
  function el(id){return document.getElementById(id);}
  function esc(value){return String(value === null || value === undefined ? "—" : value).replace(/[&<>"']/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];});}
  function num(value){
    if(value === null || value === undefined || value === "") return "—";
    var n = Number(value);
    return Number.isFinite(n) ? n.toLocaleString("en-IN",{minimumFractionDigits:2,maximumFractionDigits:2}) : esc(value);
  }
  function dateTime(value){
    if(!value) return "—";
    var d = new Date(value);
    return Number.isNaN(d.getTime()) ? esc(value) : d.toLocaleString("en-IN",{timeZone:"Asia/Kolkata",dateStyle:"medium",timeStyle:"medium"});
  }
  function status(text,kind){el("pv-status").className="pv-status "+kind;el("pv-status").textContent=text;}
  function notice(title,message){el("pv-notice-title").textContent=title;el("pv-notice-text").textContent=message;}
  function detail(label,value){return '<div class="pv-detail"><span>'+esc(label)+'</span><strong>'+esc(value)+'</strong></div>';}

  async function loadIndexes(){
    var cards = await Promise.all(INDEXES.map(async function(item){
      try{
        var response = await fetch("/live/snapshot?instrument_key="+encodeURIComponent(item.key),{headers:{Accept:"application/json"},cache:"no-store"});
        if(!response.ok) throw new Error("HTTP "+response.status);
        return {item:item,data:await response.json()};
      }catch(error){return {item:item,data:{status:"unavailable",ltp:null}};}
    }));
    el("pv-index-grid").innerHTML = cards.map(function(card){
      var data=card.data, item=card.item;
      return '<article class="pv-index"><div class="pv-index-top"><span class="pv-index-name">'+esc(item.name)+'</span><span class="pv-dot '+(data.ltp != null ? "ok" : "")+'"></span></div>'+
        '<div class="pv-index-ltp">'+(data.ltp != null ? num(data.ltp) : "—")+'</div>'+
        '<div class="pv-index-key">'+esc(item.key)+'</div>'+
        '<div class="pv-index-time">'+(data.ltp != null ? "Last tick "+dateTime(data.last_tick_at || data.timestamp) : "No live tick received")+'</div></article>';
    }).join("");
    var active=cards.filter(function(x){return x.data.ltp != null;}).length;
    el("pv-feed-state").textContent=active+" / "+cards.length+" indexes have a cached tick";
    return active;
  }

  function collectOrders(data){
    if(!data || typeof data!=="object") return [];
    var list=Array.isArray(data.orders) ? data.orders.slice() : [];
    // Include the canonical slots too; deduplication below keeps the
    // compatibility list and its source order objects from double-counting.
    if(data.order && typeof data.order==="object") list.push(data.order);
    if(data.post_1130_order && typeof data.post_1130_order==="object") list.push(data.post_1130_order);
    var seen={};
    return list.filter(function(order){
      var key=String(order.order_id || order.instrument_key || JSON.stringify(order));
      if(seen[key]) return false; seen[key]=true; return true;
    });
  }
  function statePill(order){
    var state=String(order.status || (order.closed_at ? "CLOSED" : "OPEN"));
    var upper=state.toUpperCase(), cls="open";
    if(/STOP|LOSS|REJECT|FAIL/.test(upper)) cls="loss";
    else if(/SKIP|FILTER/.test(upper)) cls="skip";
    else if(/CLOSED|EXIT|TARGET|END_OF_DAY|COMPLETE/.test(upper)) cls="closed";
    return '<span class="pv-pill '+cls+'">'+esc(state.replace(/_/g," "))+'</span>';
  }
  function renderOrders(data){
    var orders=collectOrders(data);
    var open=orders.filter(function(o){return String(o.status || "OPEN").toUpperCase()==="OPEN";}).length;
    var closed=orders.length-open;
    var realised=orders.reduce(function(sum,o){
      var isOpen=String(o.status || "OPEN").toUpperCase()==="OPEN";
      var v=Number(o.closed_profit_pts !== undefined ? o.closed_profit_pts : (o.pnl_points || 0));
      return sum+(!isOpen && Number.isFinite(v) ? v : 0);
    },0);
    el("pv-total-orders").textContent=String(orders.length);
    el("pv-open-orders").textContent=String(open);
    el("pv-closed-orders").textContent=String(closed);
    el("pv-pnl").textContent=orders.length ? num(realised) : "—";
    el("pv-pnl").className="pv-value "+(realised>0?"pv-positive":realised<0?"pv-negative":"");
    el("pv-orders-section").hidden=false;
    el("pv-no-orders").hidden=orders.length>0;
    el("pv-orders-updated").textContent="Updated "+dateTime(data && data.updated_at);
    el("pv-orders-body").innerHTML=orders.map(function(o){
      var pnl=Number(o.closed_profit_pts !== undefined ? o.closed_profit_pts : o.pnl_points);
      var rawPct=Number(o.closed_pnl_pct !== undefined ? o.closed_pnl_pct : o.pnl_pct);
      var pnlPct=Number.isFinite(rawPct) ? (Math.abs(rawPct)>1 ? rawPct : rawPct*100) : NaN;
      return '<tr><td><strong>'+esc(o.trading_symbol || o.instrument_key)+'</strong><span class="pv-subcell">'+esc(o.instrument_key)+'</span></td>'+
        '<td>'+statePill(o)+'</td><td>'+dateTime(o.entry_time || o.created_at)+'</td><td class="num">'+num(o.entry_price)+'</td>'+
        '<td class="num">'+num(o.active_stop_price !== undefined ? o.active_stop_price : o.stop_price)+'</td>'+
        '<td class="num">'+num(o.active_target_price !== undefined ? o.active_target_price : o.target_price)+'</td>'+
        '<td>'+dateTime(o.closed_time || o.closed_at)+'</td><td class="num">'+num(o.closed_price)+'</td>'+
        '<td class="num '+(Number.isFinite(pnl)&&pnl>0?"pv-positive":Number.isFinite(pnl)&&pnl<0?"pv-negative":"")+'">'+(Number.isFinite(pnl)?num(pnl):"—")+'</td>'+
        '<td class="num">'+(Number.isFinite(pnlPct)?num(pnlPct)+"%":"—")+'</td></tr>';
    }).join("");
    return orders.length;
  }

  function renderIsolation(container){
    var iso=container && container.isolated;
    if(!iso || typeof iso!=="object") return false;
    el("pv-isolation-section").hidden=false;
    el("pv-iso-symbol").textContent=iso.trading_symbol || iso.instrument_key || "Isolated instrument";
    el("pv-iso-key").textContent=iso.instrument_key || "—";
    var mode=iso.standard_execution_mode || (iso.early_extreme_reversal ? "EARLY_EXTREME_REVERSAL" : "STANDARD");
    el("pv-mode").textContent=mode;
    var tags=[];
    if(iso.type) tags.push('<span class="pv-tag">'+esc(iso.type)+'</span>');
    if(iso.trigger_side) tags.push('<span class="pv-tag">'+esc(iso.trigger_side)+'</span>');
    if(iso.source) tags.push('<span class="pv-tag">'+esc(iso.source)+'</span>');
    if(iso.execution_path) tags.push('<span class="pv-tag">'+esc(iso.execution_path)+'</span>');
    el("pv-iso-tags").innerHTML=tags.join("");
    var meta=[
      ["Isolated at",dateTime(iso.isolated_at)],["Trigger index",iso.trigger_index],
      ["Trigger level",iso.trigger_level],["Trigger price",num(iso.trigger_level_value)],
      ["Index LTP at trigger",num(iso.trigger_ltp)],["Option LTP at touch",num(iso.instrument_ltp)]
    ];
    el("pv-iso-meta").innerHTML=meta.map(function(x){return '<div class="pv-meta"><span>'+esc(x[0])+'</span><strong>'+esc(x[1])+'</strong></div>';}).join("");
    var trigger=[
      ["Trading day",container.day],["Reason",iso.reason],["Trigger index key",iso.trigger_instrument_key],
      ["Routed index",iso.routed_index],["Routed index LTP",num(iso.routed_nifty_ltp)],
      ["Execution mode",mode],["Mode decision price",num(iso.execution_mode_price)],
      ["Trigger priority",iso.trigger_priority],["Execution path",iso.execution_path],
      ["Levels touched",Array.isArray(iso.levels)?iso.levels.join(", "):"—"]
    ];
    el("pv-trigger-details").innerHTML=trigger.map(function(x){return detail(x[0],x[1]);}).join("");
    var levels=iso.instrument_or_levels || {};
    var keys=Object.keys(levels);
    if(keys.length){
      el("pv-levels-section").hidden=false;
      el("pv-levels").innerHTML=keys.map(function(k){return '<div class="pv-level">'+esc(k)+'<strong>'+num(levels[k])+'</strong></div>';}).join("");
    }
    return true;
  }

  function renderEvents(container){
    var events=container && Array.isArray(container.events) ? container.events : [];
    if(!events.length) return;
    el("pv-events-section").hidden=false;
    el("pv-event-count").textContent=events.length+" event(s)";
    el("pv-events").innerHTML=events.slice().reverse().map(function(event){
      var candidates=Array.isArray(event.candidates)?event.candidates:[];
      var html='<div class="pv-event"><strong>'+esc(event.index || "Index")+' · '+esc(event.side || event.trigger_level || "Touch")+' · '+esc(event.status || "")+'</strong>'+
        '<small>'+dateTime(event.touched_at)+' · Trigger '+num(event.trigger_level_value)+' · LTP '+num(event.trigger_ltp)+' · Source '+esc(event.source || "—")+'</small>';
      if(candidates.length){
        html+='<div class="pv-levels" style="margin-top:9px">'+candidates.map(function(c){
          return '<div class="pv-level">'+esc(c.trading_symbol || c.instrument_key)+'<strong>'+esc(c.side || "")+' · '+esc(Array.isArray(c.levels)?c.levels.join(", "):"")+'</strong></div>';
        }).join("")+'</div>';
      }
      return html+'</div>';
    }).join("");
  }

  function renderSnapshot(snapshot,response){
    var isolation=snapshot.isolated_instrument || {};
    var ordersData=snapshot.orders_data || {};
    var hasIso=renderIsolation(isolation);
    renderEvents(isolation);
    renderOrders(ordersData);
    el("pv-source-section").hidden=false;
    var info=[
      ["Document date",snapshot._id],["Created",dateTime(snapshot.created_datetime)],
      ["Last updated",dateTime(snapshot.last_updated_time)],["Database",response.source && response.source.database],
      ["Collection",response.source && response.source.collection],["Access mode","Read-only"]
    ];
    el("pv-source-details").innerHTML=info.map(function(x){return detail(x[0],x[1]);}).join("");
    el("pv-raw-json").textContent=JSON.stringify(snapshot,null,2);
    status("Today's record loaded","saved");
    notice(hasIso?"Today's performance snapshot is available":"Today's daily document is available","Loaded the upstream MongoDB document for "+response.date+". This page only displays saved values and does not run the trading strategy.");
  }

  async function refresh(){
    if(busy) return;
    busy=true; el("pv-refresh").disabled=true;
    try{
      var results=await Promise.all([
        loadIndexes(),
        fetch(SNAPSHOT_URL,{headers:{Accept:"application/json"},cache:"no-store"})
      ]);
      var liveCount=results[0], response=results[1];
      if(!response.ok) throw new Error("Snapshot endpoint returned HTTP "+response.status);
      var data=await response.json();
      el("pv-date").textContent=(data.date || "—") + (data.market && data.market.label ? " · " + data.market.label : "");
      if(data.status==="found" && data.snapshot){
        el("pv-watch-section").hidden=false;
        renderSnapshot(data.snapshot,data);
      }else{
        ["pv-isolation-section","pv-levels-section","pv-events-section","pv-orders-section","pv-source-section"].forEach(function(id){el(id).hidden=true;});
        ["pv-total-orders","pv-open-orders","pv-closed-orders","pv-pnl"].forEach(function(id){el(id).textContent="—";});
        el("pv-watch-section").hidden=false;
        if(data.status==="not_found"){
          status("Waiting for today's record","waiting");
          notice("No saved isolation document for today yet","Today ("+data.date+") has no document in the upstream collection. The four monitored indexes remain visible. The dashboard checks again automatically and switches to the saved performance view when today's document appears.");
        }else{
          status(data.status==="disabled"?"Snapshot reader disabled":"Snapshot unavailable","error");
          notice("Today's saved record could not be loaded",data.error || "Check the MongoDB connection and snapshot reader configuration. The index watch may still show cached market ticks.");
        }
      }
      if(liveCount===0) el("pv-feed-state").textContent="No cached ticks from the live feed";
    }catch(error){
      status("Dashboard refresh failed","error");
      notice("Unable to refresh dashboard",error.message || String(error));
    }finally{busy=false;el("pv-refresh").disabled=false;}
  }
  el("pv-refresh").addEventListener("click",refresh);
  refresh();
  window.setInterval(refresh,15000);
})();