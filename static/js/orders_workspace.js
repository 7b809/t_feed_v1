(function () {
"use strict";
var API="/live/performance", view="today", todayOrders=[], page=1, pageSize=20, totalPages=0, total=0, search="", loading=false, refreshTimer=null;
function el(id){return document.getElementById(id);}
function esc(v){return String(v===null||v===undefined||v===""?"—":v).replace(/[&<>"']/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];});}
function value(o,keys){for(var i=0;i<keys.length;i++){if(o[keys[i]]!==undefined&&o[keys[i]]!==null&&o[keys[i]]!=="")return o[keys[i]];}return null;}
function fmt(v){if(v===null||v===undefined||v==="")return "—";var n=Number(v);return Number.isFinite(n)?n.toLocaleString("en-IN",{minimumFractionDigits:2,maximumFractionDigits:2}):esc(v);}
function dt(v){if(!v)return "—";var d=new Date(v);return Number.isNaN(d.getTime())?esc(v):d.toLocaleString("en-IN",{timeZone:"Asia/Kolkata",dateStyle:"medium",timeStyle:"short"});}
function timeOnly(v){if(!v)return "—";var d=new Date(v);return Number.isNaN(d.getTime())?esc(v):d.toLocaleTimeString("en-IN",{timeZone:"Asia/Kolkata",hour:"2-digit",minute:"2-digit",second:"2-digit"});}
function stateOf(o){return String(value(o,["status","order_status"])||(value(o,["closed_at","closed_time"])?"CLOSED":"OPEN")).toUpperCase();}
function badge(o){var s=stateOf(o),c="";if(s==="OPEN"||s==="PENDING"||s==="ACTIVE")c="open";else if(/STOP|LOSS|REJECT|FAIL/.test(s))c="loss";else if(/SKIP|FILTER/.test(s))c="skip";else if(/CLOSED|EXIT|TARGET|END_OF_DAY|COMPLETE/.test(s))c="closed";return '<span class="ow-badge '+c+'">'+esc(s.replace(/_/g," "))+'</span>';}
function pnl(o){var v=Number(value(o,["closed_profit_pts","pnl_points","profit_points","realised_points"]));return Number.isFinite(v)?v:null;}
function orderSearchText(item){var o=item.order||item;return [o.trading_symbol,o.instrument_key,o.status,o.order_id,o.strategy,o.execution_path,item.trading_date].join(" ").toLowerCase();}
function setStatus(text,kind){el("ow-status").className="ow-status "+kind;el("ow-status").textContent=text;}
function showEmpty(title,message){el("ow-empty").hidden=false;el("ow-empty-title").textContent=title;el("ow-empty-text").textContent=message;el("ow-table-wrap").hidden=true;el("ow-mobile").hidden=true;el("ow-pagination").hidden=true;}
function setStats(orders,summary){
var open=0,closed=0,realised=0;
orders.forEach(function(o){var s=stateOf(o);if(s==="OPEN"||s==="ACTIVE"||s==="PENDING")open++;else{closed++;var p=pnl(o);if(p!==null)realised+=p;}});
el("ow-total").textContent=String(summary&&summary.total_orders!==undefined?summary.total_orders:orders.length);
el("ow-open").textContent=String(summary&&summary.open_orders!==undefined?summary.open_orders:open);
el("ow-closed").textContent=String(summary&&summary.closed_orders!==undefined?summary.closed_orders:closed);
var p=summary&&summary.realised_points!==undefined?Number(summary.realised_points):realised;
el("ow-pnl").textContent=orders.length||summary?" "+fmt(p):"—";
el("ow-pnl").className="ow-value "+(p>0?"ow-pos":p<0?"ow-neg":"");
}
function rowHtml(item){
var o=item.order||item, date=item.trading_date||item.date||"Today";
var symbol=value(o,["trading_symbol","symbol","instrument_key"]);
var key=value(o,["instrument_key","instrument_token"]);
var entry=value(o,["entry_price","entry_ltp","entry"]);
var stop=value(o,["active_stop_price","stop_price","sl_price"]);
var target=value(o,["active_target_price","target_price"]);
var exit=value(o,["closed_price","exit_price","exit_ltp"]);
var points=pnl(o);
var strategy=value(o,["strategy","execution_path","standard_execution_mode","strategy_name"]);
var entryTime=value(o,["entry_time","created_at","opened_at"]);
var exitTime=value(o,["closed_time","closed_at","exit_time"]);
return '<tr><td>'+esc(date)+'</td><td><span class="ow-symbol">'+esc(symbol)+'</span><span class="ow-muted">'+esc(key)+'</span></td><td>'+esc(strategy)+'</td><td>'+badge(o)+'</td><td>'+timeOnly(entryTime)+'</td><td class="right">'+fmt(entry)+'</td><td class="right">'+fmt(stop)+'</td><td class="right">'+fmt(target)+'</td><td>'+timeOnly(exitTime)+'</td><td class="right">'+fmt(exit)+'</td><td class="right '+(points>0?"ow-pos":points<0?"ow-neg":"")+'">'+(points===null?"—":fmt(points))+'</td></tr>';
}
function mobileHtml(item){
var o=item.order||item, symbol=value(o,["trading_symbol","symbol","instrument_key"]), key=value(o,["instrument_key","instrument_token"]);
var points=pnl(o);
return '<article class="ow-mobile-item"><div class="ow-mobile-top"><div><div class="ow-mobile-name">'+esc(symbol)+'</div><span class="ow-muted">'+esc(key)+' · '+esc(item.trading_date||"Today")+'</span></div>'+badge(o)+'</div>'+
'<div class="ow-mobile-grid"><div><span>Entry</span><strong>'+fmt(value(o,["entry_price","entry_ltp","entry"]))+'</strong></div><div><span>Stop</span><strong>'+fmt(value(o,["active_stop_price","stop_price","sl_price"]))+'</strong></div><div><span>Target</span><strong>'+fmt(value(o,["active_target_price","target_price"]))+'</strong></div><div><span>Exit</span><strong>'+fmt(value(o,["closed_price","exit_price","exit_ltp"]))+'</strong></div><div><span>P&amp;L pts</span><strong class="'+(points>0?"ow-pos":points<0?"ow-neg":"")+'">'+(points===null?"—":fmt(points))+'</strong></div><div><span>Strategy</span><strong>'+esc(value(o,["strategy","execution_path","standard_execution_mode"]))+'</strong></div></div></article>';
}
function render(items,opts){
var filtered=items;
if(view==="today"&&search)filtered=items.filter(function(x){return orderSearchText(x).indexOf(search)>=0;});
var pages=Math.max(1,Math.ceil((opts.server?total:filtered.length)/pageSize));
if(!opts.server){total=filtered.length;totalPages=pages;filtered=filtered.slice((page-1)*pageSize,page*pageSize);}
el("ow-count").textContent=(opts.server?total:filtered.length)+" order record(s)";
el("ow-rows").innerHTML=filtered.map(rowHtml).join("");
el("ow-mobile").innerHTML=filtered.map(mobileHtml).join("");
el("ow-empty").hidden=filtered.length>0;
el("ow-table-wrap").hidden=filtered.length===0;
el("ow-mobile").hidden=filtered.length===0;
el("ow-pagination").hidden=totalPages<=1;
el("ow-page-info").textContent="Showing "+(total?((page-1)*pageSize+1):0)+"–"+Math.min(page*pageSize,total)+" of "+total;
el("ow-page-label").textContent="Page "+page+" of "+Math.max(totalPages,1);
el("ow-prev").disabled=page<=1;
el("ow-next").disabled=page>=totalPages;
if(!filtered.length)showEmpty(search?"No matching orders":"No orders found",search?"Try another symbol, status or order ID.":"No saved order records are available for this view yet.");
}
async function getJson(url){
var r=await fetch(url,{method:"GET",credentials:"same-origin",headers:{Accept:"application/json"},cache:"no-store"});
var data={};try{data=await r.json();}catch(_){}
if(!r.ok)throw new Error(data.detail||data.error||("HTTP "+r.status));
return data;
}
async function loadToday(){
if(loading)return;loading=true;el("ow-refresh").disabled=true;
try{
var data=await getJson(API+"/today-orders");
el("ow-date").textContent=(data.date||"—")+(data.market&&data.market.label?" · "+data.market.label:"");
if(data.status==="found"){
todayOrders=(data.orders||[]).map(function(o){return {trading_date:data.date,order:o};});
setStats(data.orders||[],data.summary||null);
setStatus("Today's record loaded","ok");
el("ow-note-title").textContent="Today's upstream order record";
el("ow-note-text").textContent="Loaded "+todayOrders.length+" order(s) from "+(data.source&&data.source.database||"the source database")+"."+(data.document&&data.document.last_updated_time?" Last updated "+dt(data.document.last_updated_time)+".":"");
if(!todayOrders.length)showEmpty("No orders saved for today","Today's MongoDB document exists, but its orders_data does not contain any order records.");
else render(todayOrders,{server:false});
}else if(data.status==="not_found"){
todayOrders=[];setStats([],null);setStatus("Waiting for today's record","wait");
el("ow-note-title").textContent="Today's order document has not been published";
el("ow-note-text").textContent="No document was found for "+data.date+". The dashboard will keep checking automatically; use Order History to view previously saved days.";
showEmpty("No order document for today","The upstream project has not saved today's dummy_orders document yet.");
}else{
todayOrders=[];setStats([],null);setStatus(data.status==="disabled"?"Reader disabled":"Data unavailable","err");
el("ow-note-title").textContent="Today's orders could not be loaded";
el("ow-note-text").textContent=data.error||"Check MongoDB connectivity and the snapshot reader configuration.";
showEmpty("Today's orders unavailable",data.error||"Please retry in a moment.");
}
}catch(e){setStatus("Unable to load orders","err");el("ow-note-title").textContent="Could not load today's orders";el("ow-note-text").textContent=e.message;showEmpty("Connection problem",e.message);}
finally{loading=false;el("ow-refresh").disabled=false;}
}
async function loadHistory(){
if(loading)return;loading=true;el("ow-refresh").disabled=true;
try{
var params=new URLSearchParams({page:String(page),page_size:String(pageSize)});
if(search)params.set("search",search);
var data=await getJson(API+"/order-history?"+params.toString());
if(data.status!=="ok")throw new Error(data.error||"Order history unavailable");
total=Number(data.total||0);totalPages=Number(data.total_pages||0);
setStatus("History loaded","ok");
el("ow-date").textContent="All saved dates";
el("ow-note-title").textContent="Historical order archive";
el("ow-note-text").textContent="Orders are read from daily MongoDB documents, newest trading date first. Pagination and search are processed on the server.";
render(data.items||[],{server:true});
}catch(e){setStatus("History unavailable","err");showEmpty("Unable to load order history",e.message);}
finally{loading=false;el("ow-refresh").disabled=false;}
}
function setView(next){
view=next;page=1;
el("ow-tab-today").classList.toggle("active",view==="today");
el("ow-tab-history").classList.toggle("active",view==="history");
el("ow-page-size").hidden=view==="today";
el("ow-search").placeholder=view==="today"?"Filter today's orders…":"Search all dates by symbol, key, status or order ID…";
if(view==="today"){el("ow-search").value="";search="";loadToday();}
else loadHistory();
}
function refresh(){if(view==="today")loadToday();else loadHistory();}
var searchTimer=null;
el("ow-tab-today").addEventListener("click",function(){setView("today");});
el("ow-tab-history").addEventListener("click",function(){setView("history");});
el("ow-refresh").addEventListener("click",refresh);
el("ow-prev").addEventListener("click",function(){if(page>1){page--;refresh();}});
el("ow-next").addEventListener("click",function(){if(page<totalPages){page++;refresh();}});
el("ow-page-size").addEventListener("change",function(){pageSize=Number(this.value)||20;page=1;if(view==="history")loadHistory();});
el("ow-search").addEventListener("input",function(){
search=this.value.trim();
if(view==="today"){page=1;render(todayOrders,{server:false});return;}
window.clearTimeout(searchTimer);searchTimer=window.setTimeout(function(){page=1;loadHistory();},350);
});
setView("today");
refreshTimer=window.setInterval(function(){if(view==="today"&&!document.hidden)loadToday();},15000);
window.addEventListener("beforeunload",function(){if(refreshTimer)window.clearInterval(refreshTimer);});
})();