/**
 * PS Vibe — Standalone API Server for VPS  (v2 — full port)
 * All endpoints from the Replit API server, ported to VPS.
 * - Google Sheets via googleapis + service_account.json
 * - Customer bookings via better-sqlite3 (replaces PostgreSQL)
 */
"use strict";

const express    = require("express");
const fs         = require("fs");
const path       = require("path");
const { google } = require("googleapis");

// ── Config ────────────────────────────────────────────────────────────────────
const PORT           = parseInt(process.env.PORT ?? "3000", 10);
const SHEET_ID       = process.env.SHEET_ID ?? "";
const RECEIPT_SECRET = process.env.RECEIPT_SECRET ?? "";
const BASE_DIR       = __dirname;
const SA_PATH        = path.join(BASE_DIR, "service_account.json");
const RECEIPTS_DIR   = path.join(BASE_DIR, "receipts");
const LOGO_PATH      = path.join(BASE_DIR, "logo.png");
const BK_JSON_PATH   = path.join(BASE_DIR, "bookings.json");
const WL_JSON_PATH   = path.join(BASE_DIR, "waitlist.json");
const CUSTOMER_BOT_TOKEN = process.env.CUSTOMER_BOT_TOKEN ?? "";

// ── JSON file bookings store ─────────────────────────────────────────────────
// Sync helpers (Node.js single-thread safe for reads; writes are always
// serialised through _withBkLock so concurrent requests never race).
function _bkRead() {
  try { return JSON.parse(fs.readFileSync(BK_JSON_PATH, "utf-8")); }
  catch { return { next_id: 1, rows: [] }; }
}
function _bkWrite(store) {
  fs.writeFileSync(BK_JSON_PATH, JSON.stringify(store, null, 2), "utf-8");
}
// Ensure file exists
if (!fs.existsSync(BK_JSON_PATH)) _bkWrite({ next_id: 1, rows: [] });

// ── Async write lock — serialises all read-modify-write cycles ────────────────
// Prevents data loss when two simultaneous booking requests both read the file,
// modify, and write (last-write-wins race). All mutation routes use _withBkLock.
let _bkWriteLock = Promise.resolve();
function _withBkLock(fn) {
  const p = _bkWriteLock.then(fn);
  _bkWriteLock = p.catch(() => {}); // keep chain alive even on error
  return p;
}

// ── Waitlist JSON store ───────────────────────────────────────────────────────
function _wlRead() {
  try { return JSON.parse(fs.readFileSync(WL_JSON_PATH, "utf-8")); }
  catch { return { next_id: 1, rows: [] }; }
}
function _wlWrite(store) {
  fs.writeFileSync(WL_JSON_PATH, JSON.stringify(store, null, 2), "utf-8");
}
if (!fs.existsSync(WL_JSON_PATH)) _wlWrite({ next_id: 1, rows: [] });

let _wlWriteLock = Promise.resolve();
function _withWlLock(fn) {
  const p = _wlWriteLock.then(fn);
  _wlWriteLock = p.catch(() => {});
  return p;
}

// ── Telegram send helper (uses CUSTOMER_BOT_TOKEN) ───────────────────────────
const https = require("https");
function _tgSend(chatId, text) {
  if (!CUSTOMER_BOT_TOKEN || !chatId) return Promise.resolve();
  return new Promise((resolve) => {
    const body = JSON.stringify({ chat_id: String(chatId), text, parse_mode: "HTML" });
    const req = https.request({
      hostname: "api.telegram.org",
      path: `/bot${CUSTOMER_BOT_TOKEN}/sendMessage`,
      method: "POST",
      headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) },
    }, (res) => { res.resume(); resolve(res.statusCode); });
    req.on("error", (e) => { console.error("[tgSend]", e.message); resolve(null); });
    req.write(body);
    req.end();
  });
}

// ── Internal FIFO notify: find next waiting entry matching pref, notify them ─
async function _wlNotifyNext(consolePref) {
  return _withWlLock(async () => {
    const store = _wlRead();
    const candidate = store.rows
      .filter(r => r.status === "waiting" &&
        (r.console_pref === consolePref || r.console_pref === "Any" ||
         consolePref === "Any" || consolePref === null))
      .sort((a, b) => a.joined_at.localeCompare(b.joined_at))[0];

    if (!candidate) return null;

    candidate.status = "notified";
    candidate.notified_at = new Date().toISOString();
    _wlWrite(store);

    const prefLabel = consolePref && consolePref !== "Any" ? consolePref : "PS5";
    const msg = [
      `🎮 <b>PS Vibe — Console ပြန်လွတ်ပါပြီ!</b>`,
      `━━━━━━━━━━━━━━━━━━`,
      `<b>${prefLabel}</b> Console တစ်ခု ပြန်ရနိုင်ပါပြီ။`,
      ``,
      `⏰ မိနစ် ၁၅ အတွင်း Booking မလုပ်ပါက`,
      `   Waitlist မှ အလိုအလျောက် ထွက်သွားမည်ဖြစ်ပါသည်။`,
      ``,
      `/book ကို နှိပ်ပြီး ယခုပင် Booking လုပ်ပါ 👇`,
    ].join("\n");

    await _tgSend(candidate.telegram_chat_id, msg);
    console.log(`[waitlist] Notified entry #${candidate.id} (${candidate.customer_name}) for pref=${consolePref}`);
    return candidate;
  });
}

// Normalize internal snake_case booking row → camelCase (matches original Drizzle ORM output)
function _normalizeBk(r) {
  return {
    id:             r.id,
    customerName:   r.customer_name,
    phone:          r.phone,
    date:           r.date,
    timeSlot:       r.time_slot,
    durationMins:   r.duration_mins,
    consoleId:      r.console_id   || null,
    consoleType:    r.console_type || null,
    consolePref:    r.console_pref || null,
    gameName:       r.game         || null,
    notes:          r.notes        || null,
    status:         r.status,
    staffNote:      r.staff_note   || null,
    memberId:       r.member_id    || null,
    telegramChatId: r.telegram_chat_id || null,
    source:         r.source       || null,
    createdAt:      r.created_at,
    updatedAt:      r.updated_at,
  };
}

// ── Google Sheets auth ────────────────────────────────────────────────────────
const SCOPES_RO = ["https://www.googleapis.com/auth/spreadsheets.readonly"];
const SCOPES_RW = ["https://www.googleapis.com/auth/spreadsheets"];
let _roClient = null, _rwClient = null;
function getSheetsRo() {
  if (!_roClient) { const a = new google.auth.GoogleAuth({ keyFile: SA_PATH, scopes: SCOPES_RO }); _roClient = google.sheets({ version: "v4", auth: a }); }
  return _roClient;
}
function getSheetsRw() {
  if (!_rwClient) { const a = new google.auth.GoogleAuth({ keyFile: SA_PATH, scopes: SCOPES_RW }); _rwClient = google.sheets({ version: "v4", auth: a }); }
  return _rwClient;
}
async function sheetGet(range) {
  const r = await getSheetsRo().spreadsheets.values.get({ spreadsheetId: SHEET_ID, range });
  return r.data.values ?? [];
}
async function sheetsGetBatch(ranges) {
  const r = await getSheetsRo().spreadsheets.values.batchGet({ spreadsheetId: SHEET_ID, ranges });
  return (r.data.valueRanges ?? []).map(vr => vr.values ?? []);
}
async function sheetAppend(range, values) {
  await getSheetsRw().spreadsheets.values.append({
    spreadsheetId: SHEET_ID, range,
    valueInputOption: "USER_ENTERED",
    requestBody: { values },
  });
}
async function sheetBatchUpdate(data) {
  await getSheetsRw().spreadsheets.values.batchUpdate({
    spreadsheetId: SHEET_ID,
    requestBody: { valueInputOption: "USER_ENTERED", data },
  });
}

// ── TTL cache ─────────────────────────────────────────────────────────────────
const _cache = new Map();
function cacheGet(key) {
  const e = _cache.get(key); if (!e) return null;
  if (Date.now() > e.expires) { _cache.delete(key); return null; }
  return e.data;
}
function cacheSet(key, data, ttlMs) { _cache.set(key, { data, expires: Date.now() + ttlMs }); }
async function withCache(key, ttlMs, fn) {
  const hit = cacheGet(key); if (hit !== null) return hit;
  const r = await fn(); cacheSet(key, r, ttlMs); return r;
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function nowMmt() { return new Date(Date.now() + 6.5*60*60*1000); }
function todayStr() { const d = nowMmt(); return `${d.getUTCMonth()+1}/${d.getUTCDate()}/${d.getUTCFullYear()}`; }
function parseNum(s) { return parseFloat((String(s??'0')).replace(/,/g,'')) || 0; }
function toMins(hhmm) { const [h,m] = String(hhmm||'').split(':').map(Number); return (h||0)*60+(m||0); }
function overlaps(sA,dA,sB,dB) { const a1=toMins(sA),a2=a1+(dA||60),b1=toMins(sB),b2=b1+(dB||60); return a1<b2&&b1<a2; }
function inMonth(val,year,month) { const p=String(val||'').split('/'); return p.length===3&&parseInt(p[2])===year&&parseInt(p[0])===month; }

// ── Logo base64 ───────────────────────────────────────────────────────────────
const LOGO_DATA_URL = (() => { try { return `data:image/png;base64,${fs.readFileSync(LOGO_PATH).toString('base64')}`; } catch { return ''; } })();

// ── Receipt HTML helpers ──────────────────────────────────────────────────────
function esc(s) { return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function fmt(n) { return Number(n??0).toLocaleString('en-US'); }
function renderMemberBalance(d) {
  if (d.prev_balance==null||d.balance_change==null) return '';
  const change=Number(d.balance_change),after=Number(d.balance_after??0);
  const lbl=change>=0?'Added Mins':'Deducted Mins';
  const disp=change>=0?`+${fmt(Math.abs(change))} mins`:`-${fmt(Math.abs(change))} mins`;
  return `<div class="divider"></div><div class="section-label">Member Balance</div>
    <table><tr><td>Previous Balance</td><td class="right">${fmt(d.prev_balance)} mins</td></tr>
    <tr><td>${lbl}</td><td class="right">${disp}</td></tr></table>
    <div class="divider thick"></div>
    <table><tr class="balance-row"><td>REMAINING BALANCE</td><td class="right">${fmt(after)} mins</td></tr></table>`;
}
function renderSale(d) {
  const isGuest=Boolean(d.is_guest);
  const mLine=isGuest?`<tr><td colspan="2" class="center muted">[ Guest — No Member ]</td></tr>`:`<tr><td>Member ID</td><td class="right">${esc(d.member_id)}</td></tr>`;
  const gLine=isGuest?`<tr><td>Game (${esc(d.play_mins)} min × ${esc(d.multiplier)}x)</td><td class="right">${fmt(d.game_amt)} Ks</td></tr>`:`<tr><td>Play Time</td><td class="right">${esc(d.play_mins)} mins</td></tr>`;
  const items=Array.isArray(d.food_items)?d.food_items:[];
  const foodRows=items.length?items.map(i=>`<tr><td>${esc(i.name)} ×${esc(i.qty)}</td><td class="right">${fmt(i.subtotal)} Ks</td></tr>`).join(''):`<tr><td colspan="2" class="muted center">— No food ordered —</td></tr>`;
  return `<div class="section-label">Session Info</div><table>${mLine}<tr><td>Console</td><td class="right">${esc(d.console_id)}</td></tr>${gLine}</table>
    <div class="divider"></div><div class="section-label">Food &amp; Drinks</div><table>${foodRows}</table>
    <div class="divider"></div><div class="section-label">Billing Summary</div>
    <table><tr><td>Game Amount</td><td class="right">${fmt(d.game_amt)} Ks</td></tr><tr><td>Food Total</td><td class="right">${fmt(d.food_total)} Ks</td></tr></table>
    <div class="divider thick"></div><table><tr class="total-row"><td>TOTAL AMOUNT</td><td class="right">${fmt(d.net_total)} Ks</td></tr></table>
    ${renderMemberBalance(d)}<div class="divider"></div><div class="section-label">Payment Breakdown</div>
    <table><tr class="payment-row"><td>KPay</td><td class="right">${fmt(d.kpay)} Ks</td></tr><tr class="payment-row"><td>Cash</td><td class="right">${fmt(d.cash)} Ks</td></tr></table>`;
}
function renderTopup(d) {
  return `<div class="section-label">Member Info</div>
    <table><tr><td>Member ID</td><td class="right">${esc(d.member_id)}</td></tr><tr><td>Rank</td><td class="right">${esc(d.rank)}</td></tr><tr><td>Phone</td><td class="right">${esc(d.phone)}</td></tr></table>
    <div class="divider"></div><div class="section-label">Top-Up Details</div>
    <table><tr><td>Amount Paid</td><td class="right">${fmt(d.amount)} Ks</td></tr><tr><td>Base Mins</td><td class="right">${fmt(d.base_mins)} mins</td></tr><tr><td>Bonus Mins</td><td class="right">+${fmt(d.bonus_mins)} mins</td></tr></table>
    <div class="divider thick"></div><table><tr class="total-row"><td>TOTAL ADDED</td><td class="right">+${fmt(d.total_mins)} mins</td></tr></table>
    ${renderMemberBalance(d)}<div class="divider"></div><div class="section-label">Payment Breakdown</div>
    <table><tr class="payment-row"><td>KPay</td><td class="right">${fmt(d.kpay)} Ks</td></tr><tr class="payment-row"><td>Cash</td><td class="right">${fmt(d.cash)} Ks</td></tr></table>`;
}
function renderNewMember(d) {
  const rankRow=d.rank?`<tr><td>Rank</td><td class="right">${esc(d.rank)}</td></tr>`:'';
  return `<div class="section-label">New Member Info</div>
    <table><tr><td>Name</td><td class="right">${esc(d.name)}</td></tr><tr><td>Member ID</td><td class="right">${esc(d.member_id)}</td></tr><tr><td>Phone</td><td class="right">${esc(d.phone)}</td></tr>${rankRow}</table>
    <div class="divider"></div><div class="section-label">Membership Activation</div>
    <table><tr><td>Amount Paid</td><td class="right">${fmt(d.amount)} Ks</td></tr><tr><td>Mins Added</td><td class="right">+${fmt(d.mins)} mins</td></tr></table>
    ${renderMemberBalance(d)}<div class="divider"></div><div class="section-label">Payment Breakdown</div>
    <table><tr class="payment-row"><td>KPay</td><td class="right">${fmt(d.kpay)} Ks</td></tr><tr class="payment-row"><td>Cash</td><td class="right">${fmt(d.cash)} Ks</td></tr></table>`;
}
function typeLabel(t) { if(t==='topup')return'TOP-UP RECEIPT'; if(t==='new_member')return'MEMBERSHIP ACTIVATION'; return'SALES RECEIPT'; }
const RCSS=`* { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Courier New', Courier, monospace; font-size: 12px; font-weight: 800; color: #000; background: #e8e8e8; display: flex; justify-content: center; padding: 24px 0 48px; }
  .receipt { background: #fff; width: 80mm; padding: 8mm 6mm 10mm; }
  .header { text-align: center; padding-bottom: 6px; }
  .logo { display: block; max-width: 200px; width: 100%; margin: 0 auto 6px; }
  .shop-name-text { font-size: 20px; font-weight: 800; letter-spacing: 2px; }
  .subtitle { font-size: 13px; font-weight: 800; letter-spacing: .5px; margin-top: 2px; }
  .slogan { font-size: 10px; font-weight: 800; font-style: italic; margin-top: 3px; letter-spacing: .3px; }
  .receipt-type { text-align: center; font-size: 12px; font-weight: 800; letter-spacing: 3px; margin: 8px 0 3px; text-transform: uppercase; }
  .voucher-id { text-align: center; font-size: 15px; font-weight: 800; margin-bottom: 2px; }
  .receipt-date { text-align: center; font-size: 11px; font-weight: 800; margin-bottom: 6px; }
  .divider { border: none; border-top: 3px dashed #000; margin: 8px 0; }
  .divider.thick { border-top: 3px dashed #000; margin: 10px 0; }
  .section-label { font-size: 10px; font-weight: 800; text-transform: uppercase; letter-spacing: 1px; margin: 8px 0 4px; }
  table { width: 100%; border-collapse: collapse; }
  td { padding: 3px 0; vertical-align: top; font-size: 11px; font-weight: 800; }
  td.right { text-align: right; white-space: nowrap; padding-left: 6px; }
  td.center { text-align: center; } td.muted { color: #333; }
  tr.total-row td { font-size: 15px; font-weight: 800; padding: 4px 0; }
  tr.payment-row td { font-size: 13px; font-weight: 800; padding: 4px 0; }
  tr.balance-row td { font-size: 14px; font-weight: 800; padding: 4px 0; border-top: 1px solid #000; border-bottom: 1px solid #000; }
  .footer { text-align: center; font-size: 11px; font-weight: 800; margin-top: 10px; line-height: 1.6; }
  .print-btn { display: block; width: 100%; margin-top: 14px; padding: 10px; background: #000; color: #fff; border: none; cursor: pointer; font-size: 13px; font-weight: 800; font-family: 'Courier New', Courier, monospace; border-radius: 4px; letter-spacing: 2px; text-transform: uppercase; }
  .print-btn:hover { background: #333; } .tear-feed { height: 120px; }
  @media print { body { background: #fff; padding: 0; } .receipt { width: 100%; padding: 2mm 2mm 0; } .print-btn { display: none; } .tear-feed { height: 40mm; } }`;
function buildHtml(d) {
  const type=String(d.type??'sale');
  let body='';
  if(type==='sale')body=renderSale(d); else if(type==='topup')body=renderTopup(d); else if(type==='new_member')body=renderNewMember(d);
  const logoHtml=LOGO_DATA_URL?`<img src="${LOGO_DATA_URL}" alt="PS Vibe" class="logo">`:`<div class="shop-name-text">PS VIBE</div>`;
  return `<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Receipt ${esc(d.voucher_id)}</title><style>${RCSS}</style></head><body><div class="receipt">
  <div class="header">${logoHtml}<div class="subtitle">PS5 Gaming Lounge</div><div class="slogan">Play The Game. Share The VIBE!</div></div>
  <div class="divider"></div>
  <div class="receipt-type">${esc(typeLabel(type))}</div><div class="voucher-id">${esc(d.voucher_id)}</div><div class="receipt-date">${esc(d.date)}</div>
  <div class="divider"></div>${body}<div class="divider"></div>
  <div class="footer">Play The Game. Share The VIBE!</div>
  <button class="print-btn" onclick="window.print()">🖨️ Print Receipt</button>
  <div class="tear-feed"><br><br><br><br><br><br><br><br><br><br></div>
</div></body></html>`;
}

// ── Receipt persistence via Sheets ────────────────────────────────────────────
async function persistReceiptToSheet(id, data) {
  try { await sheetAppend("Receipts!A:B", [[id, JSON.stringify(data)]]); } catch(e) { console.error("persistReceipt:", e.message); }
}
async function fetchReceiptFromSheet(id) {
  try {
    const rows = await sheetGet("Receipts!A:B");
    for (const r of rows) { if ((r[0]??"").trim()===id) return JSON.parse(r[1]); }
  } catch(e) { console.error("fetchReceipt:", e.message); }
  return null;
}

// ── Sheets data fetchers ──────────────────────────────────────────────────────
async function fetchConfigData() {
  return withCache("config", 5*60*1000, async () => {
    const rows = await sheetGet("Setting!A1:T30");
    const base_rate = parseNum(rows[1]?.[1]??'0');
    const food_prices={}, food_costs={}, console_multipliers={};
    const staff_list=[];
    for (const r of rows.slice(1)) {
      const fname=(r[3]??'').trim(); if(fname){ food_prices[fname]=parseNum(r[4]??'0'); food_costs[fname]=parseNum(r[5]??'0'); }
      const cname=(r[7]??'').trim(); if(cname) console_multipliers[cname]=parseFloat((r[9]??'1').replace(',',''))||1.0;
      const sname=(r[18]??'').trim(); if(sname) staff_list.push({name:sname,base_salary:parseNum(r[19]??'0')});
    }
    const master_threshold=parseNum(rows[2]?.[12]??'0');
    const immortal_threshold=parseNum(rows[3]?.[12]??'0');
    const bonus_table=[];
    for (const r of rows.slice(1)) {
      const thr=parseNum(r[14]??'0'),w=parseNum(r[15]??'0'),m=parseNum(r[16]??'0'),i=parseNum(r[17]??'0');
      if(thr>0||w||m||i) bonus_table.push([thr,w,m,i]);
    }
    const new_member_card_price=parseNum(rows[19]?.[1]??'0');
    const new_member_base_mins=parseNum(rows[20]?.[1]??'0');
    return {base_rate,food_prices,food_costs,console_multipliers,master_threshold,immortal_threshold,bonus_table,new_member_card_price,new_member_base_mins,staff_list,cached_at:new Date().toISOString()};
  });
}

async function fetchInventoryData() {
  return withCache("inventory", 5*60*1000, async () => {
    const [invRows, siRows, soRows] = await sheetsGetBatch(["Inventory!A:H","Stock_In!A:H","Stock_Out!A:H"]);
    const qtyInMap=new Map(), fifoMap=new Map(), qtyOutMap=new Map();
    for (const r of siRows.slice(1)) {
      const name=(r[1]??'').trim(); if(!name)continue;
      const qty=parseNum(r[2]); qtyInMap.set(name,(qtyInMap.get(name)??0)+qty);
      if(!fifoMap.has(name))fifoMap.set(name,[]);
      fifoMap.get(name).push({date:(r[0]??'').trim(),qty,unit_cost:parseNum(r[3])});
    }
    for (const lots of fifoMap.values()) lots.sort((a,b)=>new Date(a.date)-new Date(b.date));
    for (const r of soRows.slice(1)) { const n=(r[2]??'').trim(); if(n)qtyOutMap.set(n,(qtyOutMap.get(n)??0)+parseNum(r[3])); }
    function calcFifoValue(name,totalOut,currentStock) {
      if(currentStock<=0)return 0;
      const lots=(fifoMap.get(name)??[]).map(l=>({...l}));
      let consumed=totalOut;
      for(const lot of lots){if(consumed<=0)break;const take=Math.min(consumed,lot.qty);lot.qty-=take;consumed-=take;}
      return lots.reduce((s,l)=>s+l.qty*l.unit_cost,0);
    }
    const seen=new Set();
    const items=invRows.slice(1).filter(r=>(r[0]??'').trim()).map(r=>{
      const name=(r[0]??'').trim();seen.add(name);
      const totalIn=qtyInMap.get(name)??parseNum(r[1]);
      const totalOut=qtyOutMap.get(name)??parseNum(r[4]);
      const currentStock=Math.max(0,totalIn-totalOut);
      const inv_value=calcFifoValue(name,totalOut,currentStock);
      const status=currentStock===0&&totalOut>0?'Out of Stock':currentStock===0?'No Stock':currentStock<=3?'Low Stock':'In Stock';
      return{name,total_in:totalIn,total_out:totalOut,current_stock:currentStock,inv_value,status};
    });
    for(const[name,totalIn]of qtyInMap.entries()){
      if(seen.has(name))continue;
      const totalOut=qtyOutMap.get(name)??0,currentStock=Math.max(0,totalIn-totalOut);
      const inv_value=calcFifoValue(name,totalOut,currentStock);
      const status=currentStock===0&&totalOut>0?'Out of Stock':currentStock===0?'No Stock':currentStock<=3?'Low Stock':'In Stock';
      items.push({name,total_in:totalIn,total_out:totalOut,current_stock:currentStock,inv_value,status});
    }
    return{items};
  });
}

async function fetchSummaryData() {
  return withCache("summary", 3*60*1000, async () => {
    const rows = await sheetGet("Sales_Daily!A:K");
    const today=todayStr();
    let todayCount=0,todayNet=0,todayKpay=0,todayCash=0;
    for(const r of rows.slice(1)){if((r[0]??'').trim()===today){todayCount++;todayNet+=parseNum(r[8]);todayKpay+=parseNum(r[9]);todayCash+=parseNum(r[10]);}}
    return{today_count:todayCount,today_net:todayNet,today_kpay:todayKpay,today_cash:todayCash,total_count:rows.length-1};
  });
}

async function fetchStockTodayData() {
  return withCache("stock-today", 3*60*1000, async () => {
    const rows = await sheetGet("Stock_Out!A:H");
    const today=todayStr(), map=new Map();
    for(const r of rows.slice(1)){
      if((r[0]??'').trim()!==today)continue;
      const name=(r[2]??'').trim(); if(!name)continue;
      const prev=map.get(name)??{qty:0,value:0,cogs:0};
      map.set(name,{qty:prev.qty+parseNum(r[3]),value:prev.value+parseNum(r[5]),cogs:prev.cogs+parseNum(r[7])});
    }
    const items=Array.from(map.entries()).map(([name,d])=>({name,...d})).sort((a,b)=>b.qty-a.qty);
    return{date:today,items};
  });
}

async function fetchStaffBreakdownData() {
  return withCache("staff-breakdown", 3*60*1000, async () => {
    const rows = await sheetGet("Sales_Daily!A:O");
    const today=todayStr(), staff={};
    for(const r of rows.slice(1)){
      if((r[0]??'').trim()!==today)continue;
      const name=(r[14]??'').trim()||'—';
      if(!staff[name])staff[name]={sessions:0,revenue:0,mins:0,kpay:0,cash:0};
      staff[name].sessions++;staff[name].revenue+=parseNum(r[8]??'0');staff[name].mins+=parseNum(r[4]??'0');
      staff[name].kpay+=parseNum(r[9]??'0');staff[name].cash+=parseNum(r[10]??'0');
    }
    return{date:today,staff};
  });
}

async function fetchConsolesData() {
  return withCache("consoles", 60*1000, async () => {
    const today=todayStr();
    const [settingRows, bookRows] = await sheetsGetBatch(["Setting!H:K","Console_Booking!A:I"]);
    const consoles=settingRows.slice(1).filter(r=>(r[0]??'').trim()).map(r=>({
      id:(r[0]??'').trim(),type:(r[1]??'').trim(),multiplier:parseFloat((r[2]??'1').replace(',',''))||1.0,notes:(r[3]??'').trim()
    }));
    const activeMap=new Map();
    for(const r of (bookRows||[]).slice(1)){
      if((r[1]??'').trim()===today&&['Active','Scheduled'].includes((r[6]??'').trim())){
        activeMap.set((r[2]??'').trim(),{bookingId:(r[0]??'').trim(),member:(r[3]??'').trim(),startTime:(r[4]??'').trim(),status:(r[6]??'').trim(),staff:(r[7]??'').trim(),notes:(r[8]??'').trim()});
      }
    }
    // Overlay confirmed JSON bookings as Reserved
    const nowMmt=new Date(Date.now()+6.5*60*60*1000);
    const minus30Ts=new Date(nowMmt.getTime()-30*60*1000);
    const minus30HHMM=`${String(minus30Ts.getUTCHours()).padStart(2,'0')}:${String(minus30Ts.getUTCMinutes()).padStart(2,'0')}`;
    const confirmedBks=_bkRead().rows.filter(b=>b.date===today&&b.status==='confirmed');
    const reservedMap=new Map();
    for(const b of confirmedBks){if(b.console_id&&(b.time_slot||'')>=minus30HHMM)reservedMap.set(b.console_id,b);}
    const result=consoles.map(c=>{
      const live=activeMap.get(c.id), rsv=reservedMap.get(c.id);
      if(live)return{...c,liveStatus:live.status,member:live.member,startTime:live.startTime,staff:live.staff,bookingId:live.bookingId,bookNotes:live.notes,reservedFor:null,reservedAt:null,reservedBkId:null};
      if(rsv)return{...c,liveStatus:'Reserved',member:rsv.customer_name,startTime:rsv.time_slot,staff:null,bookingId:null,bookNotes:null,reservedFor:rsv.customer_name,reservedAt:rsv.time_slot,reservedBkId:rsv.id};
      return{...c,liveStatus:'Free',member:null,startTime:null,staff:null,bookingId:null,bookNotes:null,reservedFor:null,reservedAt:null,reservedBkId:null};
    });
    return{consoles:result};
  });
}

async function calcPnlData(year, month) {
  return withCache(`pnl-${year}-${month}`, 10*60*1000, async () => {
    const [salesRows,topupRows,siRows,soRows,walletRows,advRows] = await sheetsGetBatch([
      "Sales_Daily!A:O","TopUp_Log!A:I","Stock_In!A:F","Stock_Out!A:H","Card_Wallet!B:L","Salary_Advance!A:E"
    ]);
    const rateDict={};
    for(const r of walletRows.slice(1)){const mId=(r[0]??'').trim(),rv=parseFloat((r[10]??'').trim());if(mId&&rv>0)rateDict[mId]=rv;}
    let _tp=0,_tm=0;
    for(const r of topupRows.slice(1)){const a=parseNum(r[4]??'0'),m=parseNum(r[7]??'0');if(a>0&&m>0){_tp+=a;_tm+=m;}}
    const alltimeRate=_tm>0?Math.round((_tp/_tm)*100)/100:150;
    let guestGameRev=0,foodRev=0,discountTotal=0,salesKpay=0,salesCash=0,walletDeductMins=0;
    const memberDeduct={};
    for(const r of salesRows.slice(1)){
      if(!inMonth(r[0]??'',year,month))continue;
      const memberId=(r[2]??'').trim(),isGuest=!memberId||memberId==='0 (Guest)';
      foodRev+=parseNum(r[6]??'0');discountTotal+=parseNum(r[7]??'0');salesKpay+=parseNum(r[9]??'0');salesCash+=parseNum(r[10]??'0');
      if(isGuest)guestGameRev+=parseNum(r[5]??'0');
      else{const wd=parseNum(r[13]??'0');walletDeductMins+=wd;memberDeduct[memberId]=(memberDeduct[memberId]??0)+wd;}
    }
    let memberGameRev=0;for(const[mId,mins]of Object.entries(memberDeduct))memberGameRev+=Math.floor(mins*(rateDict[mId]??alltimeRate));
    let topupAmount=0,topupKpay=0,topupCash=0,topupMins=0;
    for(const r of topupRows.slice(1)){if(!inMonth(r[0]??'',year,month))continue;topupAmount+=parseNum(r[4]??'0');topupKpay+=parseNum(r[5]??'0');topupCash+=parseNum(r[6]??'0');topupMins+=parseNum(r[7]??'0');}
    let stockInTotal=0,stockInCash=0,stockInKpay=0;
    for(const r of siRows.slice(1)){
      if(!inMonth(r[0]??'',year,month))continue;
      const total=parseNum(r[4]??'0'),payment=(r[5]??'').trim();stockInTotal+=total;
      if(payment.includes('/'))for(const p of payment.split('/')){const pt=p.trim();if(pt.toLowerCase().startsWith('cash'))stockInCash+=parseNum(pt.replace(/[^\d.]/g,''));if(pt.toLowerCase().startsWith('kpay'))stockInKpay+=parseNum(pt.replace(/[^\d.]/g,''));}
      else if(payment.toLowerCase()==='cash')stockInCash+=total;else if(payment.toLowerCase()==='kpay')stockInKpay+=total;
    }
    let stockOutCogs=0;for(const r of soRows.slice(1)){if(inMonth(r[0]??'',year,month))stockOutCogs+=parseNum(r[7]??'0');}
    let advTotal=0,advCash=0,advKpay=0;
    for(const r of advRows.slice(1)){if(!inMonth(r[0]??'',year,month))continue;const amt=parseNum(r[2]??'0'),pmt=(r[3]??'').trim().toLowerCase();advTotal+=amt;if(pmt.includes('kpay'))advKpay+=amt;else advCash+=amt;}
    return{year,month,guest_game_rev:guestGameRev,member_game_rev:memberGameRev,food_rev:foodRev,discount_total:discountTotal,sales_kpay:salesKpay,sales_cash:salesCash,wallet_deduct_mins:walletDeductMins,topup_amount:topupAmount,topup_kpay:topupKpay,topup_cash:topupCash,topup_mins:topupMins,stock_in_total:stockInTotal,stock_in_cash:stockInCash,stock_in_kpay:stockInKpay,stock_out_cogs:stockOutCogs,payroll_total:0,payroll_advance:advTotal,payroll_advance_cash:advCash,payroll_advance_kpay:advKpay,payroll_net_pay:0,effective_rate:alltimeRate,alltime_rate:alltimeRate};
  });
}

async function calcLiabilityData() {
  return withCache("liability", 10*60*1000, async () => {
    const [walletRows,topupRows] = await sheetsGetBatch(["Card_Wallet!B:L","TopUp_Log!E:H"]);
    let tp=0,tm=0;for(const r of topupRows.slice(1)){const a=parseNum(r[0]??'0'),m=parseNum(r[3]??'0');if(a>0&&m>0){tp+=a;tm+=m;}}
    const alltimeRate=tm>0?Math.round((tp/tm)*100)/100:150;
    const rateDict={};for(const r of walletRows.slice(1)){const mId=(r[0]??'').trim(),rv=parseFloat((r[10]??'').trim());if(mId&&rv>0)rateDict[mId]=rv;}
    let totalMinsBalance=0,totalLiability=0,activeCount=0;const topMembers=[];
    for(const r of walletRows.slice(1)){
      const mId=(r[0]??'').trim(),mName=(r[1]??'').trim(),mins=parseNum(r[6]??'0');
      if(!mId||mins<=0)continue;
      const rate=rateDict[mId]??alltimeRate,liabKs=Math.floor(mins*rate);
      activeCount++;totalMinsBalance+=mins;totalLiability+=liabKs;
      topMembers.push({id:mId,name:mName,mins,liability:liabKs,rate});
    }
    topMembers.sort((a,b)=>b.liability-a.liability);
    return{active_count:activeCount,total_mins:totalMinsBalance,total_liability:totalLiability,alltime_rate:alltimeRate,stored_rate_count:Object.keys(rateDict).length,top_members:topMembers.slice(0,5)};
  });
}

// ── Express app ───────────────────────────────────────────────────────────────
const app = express();
app.use(express.json({ limit: "2mb" }));

// ── Security: API Key, CORS, Headers ─────────────────────────────────────────
const API_KEY = process.env.API_KEY || "";

// Security headers for all responses
app.use((_req, res, next) => {
  res.setHeader("X-Content-Type-Options", "nosniff");
  res.setHeader("X-Frame-Options", "DENY");
  res.setHeader("X-XSS-Protection", "1; mode=block");
  next();
});

// CORS configuration
app.use((req, res, next) => {
  const allowedOrigins = ["https://ps-vibe.com", "http://ps-vibe.com", "http://localhost", "http://localhost:3000", "http://127.0.0.1", "http://127.0.0.1:3000"];
  const origin = req.headers.origin;
  if (origin && allowedOrigins.some(o => origin.startsWith(o))) {
    res.setHeader("Access-Control-Allow-Origin", origin);
  } else if (!origin) {
    // Allow requests with no Origin header (server-to-server, curl, bots)
    res.setHeader("Access-Control-Allow-Origin", "https://ps-vibe.com");
  }
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, X-API-Key, X-Receipt-Secret");
  if (req.method === "OPTIONS") return res.sendStatus(204);
  next();
});

// API Key authentication middleware
// Public endpoints (no key required): GET /api/receipt/:id, GET /api/healthz, OPTIONS
// All other endpoints require valid X-API-Key header
function requireApiKey(req, res, next) {
  // Skip if no API_KEY configured (backward compat during rollout)
  if (!API_KEY) return next();
  // Public endpoints - no auth needed
  const publicPaths = [
    /^\/api\/receipt\/[^/]+$/,   // GET receipt view
    /^\/api\/healthz$/,          // health check
  ];
  if (req.method === "GET" && publicPaths.some(p => p.test(req.path))) return next();
  // Check API key
  const provided = req.headers["x-api-key"];
  if (provided === API_KEY) return next();
  return res.status(401).json({ error: "Unauthorized: invalid or missing API key" });
}
app.use(requireApiKey);


// ── In-memory receipt store ───────────────────────────────────────────────────
const memoryStore = new Map();

// POST /api/receipt
app.post("/api/receipt", async (req, res) => {
  if (RECEIPT_SECRET && req.headers["x-receipt-secret"] !== RECEIPT_SECRET) return res.status(401).json({ error: "Unauthorized" });
  const data = req.body;
  const voucherId = String(data?.voucher_id ?? "").replace(/[^a-zA-Z0-9_\-]/g, "");
  if (!voucherId) return res.status(400).json({ error: "Missing voucher_id" });
  memoryStore.set(voucherId, data);
  if (!fs.existsSync(RECEIPTS_DIR)) fs.mkdirSync(RECEIPTS_DIR, { recursive: true });
  try { fs.writeFileSync(path.join(RECEIPTS_DIR, `${voucherId}.json`), JSON.stringify(data, null, 2), "utf-8"); } catch {}
  persistReceiptToSheet(voucherId, data).catch(() => {});
  res.json({ ok: true, voucher_id: voucherId });
});

// GET /api/receipt/:id
app.get("/api/receipt/:voucherId", async (req, res) => {
  const safe = (req.params.voucherId ?? "").replace(/[^a-zA-Z0-9_\-]/g, "");
  if (!safe) return res.status(400).send("Invalid voucher ID");
  if (memoryStore.has(safe)) { res.setHeader("Content-Type","text/html; charset=utf-8"); return res.send(buildHtml(memoryStore.get(safe))); }
  const fp = path.join(RECEIPTS_DIR, `${safe}.json`);
  if (fs.existsSync(fp)) { try { const d=JSON.parse(fs.readFileSync(fp,"utf-8")); memoryStore.set(safe,d); res.setHeader("Content-Type","text/html; charset=utf-8"); return res.send(buildHtml(d)); } catch {} }
  const sd = await fetchReceiptFromSheet(safe);
  if (sd) { memoryStore.set(safe,sd); res.setHeader("Content-Type","text/html; charset=utf-8"); return res.send(buildHtml(sd)); }
  res.status(404).send(`<html><body style="font-family:monospace;text-align:center;padding:40px"><h2>Receipt Not Found</h2><p><strong>${esc(safe)}</strong></p></body></html>`);
});

// ── Sheets endpoints ──────────────────────────────────────────────────────────
function sheetsRoute(key, fn) {
  return async (req, res) => {
    if (!SHEET_ID) return res.status(500).json({ error: "SHEET_ID not configured" });
    try { res.json(await fn(req)); } catch (e) { console.error(`${key}:`, e.message); res.status(500).json({ error: String(e) }); }
  };
}

app.get("/api/sheets/config",         sheetsRoute("config",         () => fetchConfigData()));
app.get("/api/sheets/summary",        sheetsRoute("summary",        () => fetchSummaryData()));
app.get("/api/sheets/members",        sheetsRoute("members",        async () => {
  return withCache("members", 3*60*1000, async () => {
    const rows = await sheetGet("Card_Wallet!B:B");
    return { member_count: rows.slice(1).filter(r=>(r[0]??'').trim()).length };
  });
}));
app.get("/api/sheets/inventory",      sheetsRoute("inventory",      async (req) => {
  if (req.query.nocache==="1") { _cache.delete("inventory"); _cache.delete("stock-today"); _cache.delete("staff-breakdown"); }
  return fetchInventoryData();
}));
app.get("/api/sheets/stock-today",    sheetsRoute("stock-today",    () => fetchStockTodayData()));
app.get("/api/sheets/report-data",    sheetsRoute("report-data",    async () => {
  const [summary,stockToday,inventory] = await Promise.all([fetchSummaryData(),fetchStockTodayData(),fetchInventoryData()]);
  return { summary, stock_today: stockToday, inventory };
}));
app.get("/api/sheets/staff-breakdown",sheetsRoute("staff-breakdown",() => fetchStaffBreakdownData()));
app.get("/api/sheets/consoles",       sheetsRoute("consoles",       async (req) => {
  if (req.query.nocache==="1") _cache.delete("consoles");
  return fetchConsolesData();
}));
app.get("/api/sheets/pnl",            sheetsRoute("pnl",            async (req) => {
  const mp=(req.query.m??'').trim(),now=nowMmt();
  const year=mp?parseInt(mp.slice(0,4)):now.getUTCFullYear();
  const month=mp?parseInt(mp.slice(5,7)):now.getUTCMonth()+1;
  if (req.query.nocache==="1") _cache.delete(`pnl-${year}-${month}`);
  return calcPnlData(year,month);
}));
app.get("/api/sheets/liability",      sheetsRoute("liability",      async (req) => {
  if (req.query.nocache==="1") { _cache.delete("liability"); }
  return calcLiabilityData();
}));
app.get("/api/sheets/daily-summary",  sheetsRoute("daily-summary",  async (req) => {
  if (req.query.nocache==="1") _cache.delete("daily-summary");
  return withCache("daily-summary", 5*60*1000, async () => {
    const [salesRows,topupRows] = await sheetsGetBatch(["Sales_Daily!A:K","TopUp_Log!A:I"]);
    const today=todayStr();
    let sessions=0,gameRev=0,foodRev=0,netTotal=0,kpay=0,cash=0,memberSessions=0,guestSessions=0,totalPlayMins=0;
    const consolesUsed=new Set();
    for(const r of salesRows.slice(1)){
      if((r[0]??'').trim()!==today)continue; sessions++;
      const mId=(r[2]??'').trim();if(mId==='0 (Guest)'||mId===''||mId==='-')guestSessions++;else memberSessions++;
      const cId=(r[3]??'').trim();if(cId)consolesUsed.add(cId);
      totalPlayMins+=parseNum(r[4]??'0');gameRev+=parseNum(r[5]??'0');foodRev+=parseNum(r[6]??'0');
      netTotal+=parseNum(r[8]??'0');kpay+=parseNum(r[9]??'0');cash+=parseNum(r[10]??'0');
    }
    let topupCount=0,newMemberCount=0,topupAmt=0;
    for(const r of topupRows.slice(1)){
      if((r[0]??'').trim()!==today)continue;
      const type=(r[8]??'').trim();
      if(type==='First Purchase'){newMemberCount++;topupCount++;}else if(type==='Top Up')topupCount++;else continue;
      topupAmt+=parseNum(r[4]??'0');
    }
    return{date:today,sessions,member_sessions:memberSessions,guest_sessions:guestSessions,consoles_used:consolesUsed.size,total_play_mins:totalPlayMins,game_rev:gameRev,food_rev:foodRev,net_total:netTotal,kpay,cash,topup_count:topupCount,new_member_count:newMemberCount,topup_amt:topupAmt};
  });
}));
// GET /api/sheets/members-list — full member list from Card_Wallet (5 min cache)
app.get("/api/sheets/members-list", sheetsRoute("members-list", async () => {
  return withCache("members-list", 5*60*1000, async () => {
    const rows = await sheetGet("Card_Wallet!A:O");
    const members = rows.slice(1)
      .filter(r => (r[1]??'').trim())
      .map(r => ({
        member_id:   (r[1]  ?? '').trim(),
        name:        (r[2]  ?? '').trim(),
        phone:       (r[3]  ?? '').trim(),
        net_spend:   parseNum(r[9]  ?? '0'),   // col J
        wallet_mins: parseNum(r[7]  ?? '0'),   // col H (balance)
        reg_staff:   (r[10] ?? '').trim(),     // col K
        email:       (r[12] ?? '').trim(),     // col M
      }));
    return { members, total: members.length };
  });
}));

// GET /api/sheets/game-library — games from Game_Library!A:S (10 min cache)
app.get("/api/sheets/game-library", sheetsRoute("game-library", async () => {
  return withCache("game-library", 10*60*1000, async () => {
    const rows = await sheetGet("Game_Library!A:S");
    if (rows.length <= 1) return { games: [] };
    const headers = rows[0].map(h => h.trim().toLowerCase());
    const ci = name => { const i = headers.indexOf(name.toLowerCase()); return i >= 0 ? i : -1; };
    const iTitle  = ci("game name")      >= 0 ? ci("game name")      : 1;
    const iStatus = ci("final status")   >= 0 ? ci("final status")   : 2;
    const iAvail  = ci("available discs")>= 0 ? ci("available discs"): 3;
    const iTotal  = ci("total copies")   >= 0 ? ci("total copies")   : 4;
    const iInUse  = ci("in use")         >= 0 ? ci("in use")         : 5;
    const games = rows.slice(1)
      .filter(r => (r[iTitle] ?? '').trim())
      .map(r => ({
        title:          (r[iTitle]  ?? '').trim(),
        status:         (r[iStatus] ?? '').trim(),
        availableDiscs: parseNum(r[iAvail] ?? '0'),
        totalCopies:    parseNum(r[iTotal] ?? '0'),
        inUse:          parseNum(r[iInUse] ?? '0'),
      }));
    return { games };
  });
}));

// GET /api/sheets/promotions — active promotions (Setting!X:Z or empty)
app.get("/api/sheets/promotions", sheetsRoute("promotions", async () => {
  return withCache("promotions", 5*60*1000, async () => {
    try {
      const rows = await sheetGet("Setting!X:Z");
      const promotions = rows.slice(1)
        .filter(r => (r[0]??'').trim())
        .map(r => ({
          title:       (r[0] ?? '').trim(),
          description: (r[1] ?? '').trim(),
          valid_until: (r[2] ?? '').trim(),
        }));
      return { promotions };
    } catch { return { promotions: [] }; }
  });
}));

// GET /api/sheets/settings/contacts — admin contacts from Setting!U:W
app.get("/api/sheets/settings/contacts", sheetsRoute("settings/contacts", async () => {
  return withCache("settings-contacts", 5*60*1000, async () => {
    const rows = await sheetGet("Setting!U:W");
    const contacts = [];
    for (const r of rows.slice(1)) {
      const label    = (r[0] ?? "").trim();  // U = display name / label
      const username = (r[1] ?? "").trim().replace(/^@/, "");  // V = Telegram handle
      const role     = (r[2] ?? "").trim();  // W = role / title
      if (label || username) contacts.push({ name: label, label, username, role });
    }
    return { contacts };
  });
}));

app.get("/api/sheets/stock-alert",    sheetsRoute("stock-alert",    async () => {
  const [invRows,siRows,soRows] = await sheetsGetBatch(["Inventory!A:H","Stock_In!A:H","Stock_Out!A:H"]);
  const qtyInMap=new Map(),qtyOutMap=new Map();
  for(const r of siRows.slice(1)){const n=(r[1]??'').trim();if(n)qtyInMap.set(n,(qtyInMap.get(n)??0)+parseNum(r[2]));}
  for(const r of soRows.slice(1)){const n=(r[2]??'').trim();if(n)qtyOutMap.set(n,(qtyOutMap.get(n)??0)+parseNum(r[3]));}
  const lowItems=[],outItems=[];
  for(const r of invRows.slice(1)){
    const name=(r[0]??'').trim();if(!name)continue;
    const stock=Math.max(0,(qtyInMap.get(name)??parseNum(r[1]))-(qtyOutMap.get(name)??parseNum(r[4])));
    if(stock===0)outItems.push({name,stock});else if(stock<=3)lowItems.push({name,stock});
  }
  const hasAlerts=outItems.length>0||lowItems.length>0;
  const d=nowMmt();const dateStr=`${d.getUTCMonth()+1}/${d.getUTCDate()}/${d.getUTCFullYear()}`;
  const outLines=outItems.map(i=>`  🔴 ${i.name} — <b>Out of Stock</b>`).join('\n');
  const lowLines=lowItems.map(i=>`  🟡 ${i.name} — <b>${i.stock} ခဲကျန်</b>`).join('\n');
  const message=hasAlerts?[`📦 <b>Stock Alert — ${dateStr}</b>`,`━━━━━━━━━━━━━━━━━━`,outItems.length?`🔴 <b>Out of Stock (${outItems.length})</b>\n${outLines}`:null,lowItems.length?`🟡 <b>Low Stock (${lowItems.length})</b>\n${lowLines}`:null,`━━━━━━━━━━━━━━━━━━`,`⚠️ Stock ဖြည့်ရန် လိုအပ်သည်`].filter(Boolean).join('\n'):'✅ Stock အားလုံး ပုံမှန်ရှိသည်';
  return{has_alerts:hasAlerts,out_of_stock_count:outItems.length,low_stock_count:lowItems.length,out_of_stock:outItems,low_stock:lowItems,telegram_message:message};
}));

// GET /api/sheets/low-wallet?threshold=N — members with wallet below threshold
app.get("/api/sheets/low-wallet", sheetsRoute("low-wallet", async (req) => {
  const threshold = parseInt(req.query.threshold || "60");
  const rows = await sheetGet("Card_Wallet!A:O");
  const d = nowMmt(); const dateStr = `${d.getUTCMonth()+1}/${d.getUTCDate()}/${d.getUTCFullYear()}`;
  const members = rows.slice(1)
    .filter(r => (r[1]??'').trim())
    .map(r => ({
      id:          (r[1]  ?? '').trim(),
      name:        (r[2]  ?? '').trim(),
      phone:       (r[3]  ?? '').trim(),
      email:       (r[12] ?? '').trim(),
      wallet_mins: parseNum(r[7]  ?? '0'),   // col H (balance)
    }))
    .filter(m => m.wallet_mins >= 0 && m.wallet_mins <= threshold);
  const count = members.length;
  const has_alerts = count > 0;
  const lines = members.map(m => `  👤 ${m.name} (<code>${m.id}</code>) — <b>${m.wallet_mins} mins</b>`).join('\n');
  const telegram_message = has_alerts
    ? `⚠️ <b>Low Wallet Alert — ${dateStr}</b>\n━━━━━━━━━━━━━━━━━━\n${lines}\n━━━━━━━━━━━━━━━━━━\n💡 Top Up လုပ်ရန် ဆက်သွယ်ပေးပါ`
    : `✅ Low wallet member မရှိပါ`;
  return { count, has_alerts, members, telegram_message };
}));

// GET /api/sheets/weekly-report — this week's (Mon-Sun MMT) summary
app.get("/api/sheets/weekly-report", sheetsRoute("weekly-report", async () => {
  const [salesRows, topupRows] = await sheetsGetBatch(["Sales_Daily!A:K", "TopUp_Log!A:I"]);
  // Build MMT week dates (Mon–Sun). MMT = UTC+6:30
  const nowUtc = new Date();
  const mmtMs  = nowUtc.getTime() + (6*60+30)*60*1000;
  const mmt    = new Date(mmtMs);
  const dow    = mmt.getUTCDay();
  const monOff = dow === 0 ? -6 : 1 - dow;
  const weekDates = new Set();
  let monDate, sunDate;
  for (let i = 0; i < 7; i++) {
    const d = new Date(mmt); d.setUTCDate(mmt.getUTCDate() + monOff + i);
    const s = `${d.getUTCMonth()+1}/${d.getUTCDate()}/${d.getUTCFullYear()}`;
    weekDates.add(s);
    if (i === 0) monDate = s;
    if (i === 6) sunDate = s;
  }
  function fmt(n) { return Math.round(n||0).toLocaleString(); }
  let sessions=0,gameRev=0,foodRev=0,netTotal=0,kpay=0,cash=0,memSess=0,guestSess=0,totalMins=0;
  for (const r of salesRows.slice(1)) {
    if (!weekDates.has((r[0]??'').trim())) continue;
    sessions++;
    const mId=(r[2]??'').trim();
    if (!mId||mId==='0 (Guest)'||mId==='-') guestSess++; else memSess++;
    totalMins+=parseNum(r[4]??'0'); gameRev+=parseNum(r[5]??'0'); foodRev+=parseNum(r[6]??'0');
    netTotal+=parseNum(r[8]??'0'); kpay+=parseNum(r[9]??'0'); cash+=parseNum(r[10]??'0');
  }
  let topupCount=0,newMembers=0,topupAmt=0;
  for (const r of topupRows.slice(1)) {
    if (!weekDates.has((r[0]??'').trim())) continue;
    const type=(r[8]??'').trim();
    if (type==='First Purchase'){newMembers++;topupCount++;}
    else if (type==='Top Up') topupCount++;
    else continue;
    topupAmt+=parseNum(r[4]??'0');
  }
  const hrs=Math.floor(totalMins/60), mins=totalMins%60;
  const telegram_message =
    `📊 <b>Weekly Report</b>\n` +
    `📅 ${monDate} – ${sunDate}\n` +
    `━━━━━━━━━━━━━━━━━━\n` +
    `🎮 Sessions : <b>${sessions}</b>  (👤 ${memSess} member / 👥 ${guestSess} guest)\n` +
    `⏱️ Play Time : <b>${hrs}h ${mins}m</b>\n` +
    `━━━━━━━━━━━━━━━━━━\n` +
    `💰 Game Rev  : <b>${fmt(gameRev)} Ks</b>\n` +
    `🍔 Food Rev  : <b>${fmt(foodRev)} Ks</b>\n` +
    `🏦 Top-Up    : <b>${fmt(topupAmt)} Ks</b>  (${topupCount} top-ups)\n` +
    `━━━━━━━━━━━━━━━━━━\n` +
    `💵 Net Total : <b>${fmt(netTotal)} Ks</b>\n` +
    `📲 KPay : <b>${fmt(kpay)} Ks</b>  |  💵 Cash : <b>${fmt(cash)} Ks</b>\n` +
    `━━━━━━━━━━━━━━━━━━\n` +
    `🆕 New Members : <b>${newMembers}</b>`;
  return { week_start:monDate, week_end:sunDate, sessions, member_sessions:memSess, guest_sessions:guestSess,
    total_play_mins:totalMins, game_rev:gameRev, food_rev:foodRev, net_total:netTotal, kpay, cash,
    topup_count:topupCount, new_members:newMembers, topup_amt:topupAmt, telegram_message };
}));

// GET /api/sheets/member-report — monthly member summary (1st of month)
app.get("/api/sheets/member-report", sheetsRoute("member-report", async () => {
  const [walletRows, topupRows] = await sheetsGetBatch(["Card_Wallet!A:O", "TopUp_Log!A:I"]);
  function fmt(n) { return Math.round(n||0).toLocaleString(); }
  const d = new Date(); const mmtMs = d.getTime()+(6*60+30)*60*1000; const mmt=new Date(mmtMs);
  const mo = mmt.getUTCMonth()+1, yr = mmt.getUTCFullYear();
  const monthStr = `${yr}-${String(mo).padStart(2,'0')}`;
  const totalMembers = walletRows.slice(1).filter(r=>(r[1]??'').trim()).length;
  const activeMembers = walletRows.slice(1).filter(r=>(r[1]??'').trim()&&parseNum(r[7]??'0')>0).length;
  // New members this month from TopUp_Log (First Purchase)
  let newThisMonth=0, topupThisMonth=0, topupAmtThisMonth=0;
  for (const r of topupRows.slice(1)) {
    if (!inMonth(r[0]??'', yr, mo)) continue;
    const type=(r[8]??'').trim();
    if (type==='First Purchase') newThisMonth++;
    if (type==='Top Up'||type==='First Purchase'){topupThisMonth++;topupAmtThisMonth+=parseNum(r[4]??'0');}
  }
  // Top 5 members by wallet_mins
  const top5 = walletRows.slice(1)
    .filter(r=>(r[1]??'').trim())
    .map(r=>({name:(r[2]??'').trim(),id:(r[1]??'').trim(),mins:parseNum(r[7]??'0')}))
    .sort((a,b)=>b.mins-a.mins).slice(0,5);
  const top5Lines = top5.map((m,i)=>`  ${i+1}. ${m.name} (${m.id}) — <b>${fmt(m.mins)} mins</b>`).join('\n');
  const telegram_message =
    `👥 <b>Member Report — ${monthStr}</b>\n` +
    `━━━━━━━━━━━━━━━━━━\n` +
    `📋 Total Members  : <b>${totalMembers}</b>\n` +
    `✅ Active (>0 min): <b>${activeMembers}</b>\n` +
    `🆕 New This Month : <b>${newThisMonth}</b>\n` +
    `━━━━━━━━━━━━━━━━━━\n` +
    `🏦 Top-Ups        : <b>${topupThisMonth}</b>  |  <b>${fmt(topupAmtThisMonth)} Ks</b>\n` +
    `━━━━━━━━━━━━━━━━━━\n` +
    `🏆 <b>Top Wallet Balances</b>\n${top5Lines}`;
  return { month:monthStr, total_members:totalMembers, active_members:activeMembers,
    new_this_month:newThisMonth, topup_count:topupThisMonth, topup_amt:topupAmtThisMonth,
    top_members:top5, telegram_message };
}));

// ── Email log (7-day cooldown for low-wallet emails) ──────────────────────────
const EMAIL_LOG_FILE = path.join(__dirname, "email_log.json");
function _emailLogRead() {
  try { return JSON.parse(fs.readFileSync(EMAIL_LOG_FILE, "utf-8")); } catch { return []; }
}
function _emailLogWrite(rows) {
  fs.writeFileSync(EMAIL_LOG_FILE, JSON.stringify(rows, null, 2), "utf-8");
}
// GET /api/email-log/check?memberId=X&type=Y&days=N
app.get("/api/email-log/check", (req, res) => {
  const { memberId, type, days } = req.query;
  if (!memberId || !type) return res.status(400).json({ error: "memberId and type required" });
  const lookback = parseInt(days || "7") * 24 * 60 * 60 * 1000;
  const rows = _emailLogRead();
  const cutoff = Date.now() - lookback;
  const recently_sent = rows.some(r =>
    r.memberId === memberId && r.emailType === type && new Date(r.sentAt).getTime() >= cutoff
  );
  res.json({ recently_sent, memberId, emailType: type });
});
// POST /api/email-log
app.post("/api/email-log", (req, res) => {
  const { memberId, emailType } = req.body || {};
  if (!memberId || !emailType) return res.status(400).json({ error: "memberId and emailType required" });
  const rows = _emailLogRead();
  rows.push({ memberId, emailType, sentAt: new Date().toISOString() });
  // Keep last 1000 entries
  if (rows.length > 1000) rows.splice(0, rows.length - 1000);
  _emailLogWrite(rows);
  res.json({ ok: true });
});

// Google Sheets bookings (Console_Booking sheet)
app.get("/api/sheets/bookings", sheetsRoute("sheets/bookings", async (req) => {
  const filterDate=(req.query.date??todayStr());
  const rows = await sheetGet("Console_Booking!A:I");
  const bookings=rows.slice(1).filter(r=>(r[0]??'').trim()&&(!filterDate||(r[1]??'').trim()===filterDate))
    .map((r,i)=>({row:i+2,bookingId:(r[0]??'').trim(),date:(r[1]??'').trim(),consoleId:(r[2]??'').trim(),memberId:(r[3]??'').trim(),startTime:(r[4]??'').trim(),endTime:(r[5]??'').trim(),status:(r[6]??'').trim(),staff:(r[7]??'').trim(),notes:(r[8]??'').trim()}))
    .sort((a,b)=>a.startTime.localeCompare(b.startTime));
  return{bookings,date:filterDate};
}));

app.post("/api/sheets/bookings", sheetsRoute("sheets/bookings/post", async (req) => {
  const {consoleId,memberId,startTime,staff,notes}=req.body;
  if(!consoleId)throw new Error("consoleId required");
  const now=nowMmt(),date=todayStr(),time=startTime??`${String(now.getUTCHours()).padStart(2,'0')}:${String(now.getUTCMinutes()).padStart(2,'0')}`;
  const seq=String(now.getUTCHours()*100+now.getUTCMinutes()).padStart(4,'0');
  const bkId=`BK-${date.replace(/\//g,'')}${consoleId.replace(/\s/g,'')}-${seq}`;
  await sheetAppend("Console_Booking!A:I",[[bkId,date,consoleId,memberId??'Guest',time,'','Active',staff??'',notes??'']]);
  _cache.delete("consoles");
  return{ok:true,bookingId:bkId,consoleId,date,startTime:time};
}));

app.patch("/api/sheets/bookings/:bookingId", sheetsRoute("sheets/bookings/patch", async (req) => {
  const {bookingId}=req.params;const{status,endTime}=req.body;
  const rows=await sheetGet("Console_Booking!A:I");
  const idx=rows.findIndex((r,i)=>i>0&&(r[0]??'').trim()===bookingId);
  if(idx<0)throw Object.assign(new Error("Booking not found"),{status:404});
  const rowNum=idx+1;const now=nowMmt();const auto=`${String(now.getUTCHours()).padStart(2,'0')}:${String(now.getUTCMinutes()).padStart(2,'0')}`;
  const updates=[];
  if(status)updates.push({range:`Console_Booking!G${rowNum}`,values:[[status]]});
  if(endTime!==undefined||status==='Done')updates.push({range:`Console_Booking!F${rowNum}`,values:[[endTime??auto]]});
  if(updates.length)await sheetBatchUpdate(updates);
  _cache.delete("consoles");
  return{ok:true,bookingId,status:status??rows[idx][6],endTime:endTime??auto};
}));

// ── JSON bookings (customer-facing) ──────────────────────────────────────────
// GET /api/bookings
app.get("/api/bookings", (req, res) => {
  try {
    const {date,status,telegramChatId,memberId}=req.query;
    let rows=_bkRead().rows;
    if(date)rows=rows.filter(r=>r.date===date);
    if(status)rows=rows.filter(r=>r.status===status);
    if(telegramChatId)rows=rows.filter(r=>r.telegram_chat_id===telegramChatId);
    if(memberId)rows=rows.filter(r=>r.member_id===memberId);
    rows.sort((a,b)=>b.created_at.localeCompare(a.created_at));
    res.json(rows.map(_normalizeBk));
  } catch(e){res.status(500).json({error:String(e)});}
});

// GET /api/bookings/broadcast-targets — unique Telegram IDs of customers with a booking
// MUST be declared before /:id so Express doesn't match "broadcast-targets" as an id param
app.get("/api/bookings/broadcast-targets", (req, res) => {
  try {
    const store = _bkRead();
    const seen = new Set();
    for (const r of store.rows) {
      if (r.telegram_chat_id && r.status !== 'cancelled') seen.add(String(r.telegram_chat_id));
    }
    const telegram_ids = [...seen];
    res.json({ count: telegram_ids.length, telegram_ids });
  } catch(e) { res.status(500).json({ error: String(e) }); }
});

// GET /api/bookings/:id
app.get("/api/bookings/:id", (req, res) => {
  try {
    const id=parseInt(req.params.id);if(isNaN(id))return res.status(400).json({error:"Invalid id"});
    const row=_bkRead().rows.find(r=>r.id===id);
    if(!row)return res.status(404).json({error:"Booking not found"});
    res.json(_normalizeBk(row));
  } catch(e){res.status(500).json({error:String(e)});}
});

// POST /api/bookings
app.post("/api/bookings", (req, res) => {
  const b=req.body;
  const cName=b.customer_name||b.customerName;const cDate=b.date;
  if(!cName||!cDate)return res.status(400).json({error:"customer_name and date required"});
  _withBkLock(() => {
    const cPhone=b.phone||'';
    const cSlot=b.time_slot||b.timeSlot||'';const cDur=b.duration_mins||b.durationMins||60;
    const cCon=b.console_id||b.consoleId||null;const cType=b.console_type||b.consoleType||null;
    const cTg=b.telegram_chat_id||b.telegramChatId||null;
    const cGame=b.gameName||b.game||null;const cMember=b.memberId||b.member_id||null;
    const cPref=b.consolePref||b.console_pref||null;
    const store=_bkRead();
    if(cCon&&cDate&&cSlot){
      const existing=store.rows.filter(r=>r.date===cDate&&r.console_id===cCon&&r.status==='confirmed');
      for(const r of existing){
        if(overlaps(cSlot,cDur,r.time_slot||'00:00',r.duration_mins||60)){
          res.status(409).json({error:"console_conflict",message:`${cCon} is already booked at ${r.time_slot} by ${r.customer_name}`,conflictingBookingId:r.id});
          return;
        }
      }
    }
    const now=new Date().toISOString();
    const newRow={id:store.next_id++,customer_name:cName,phone:cPhone,date:cDate,time_slot:cSlot,duration_mins:cDur,console_id:cCon,console_type:cType,console_pref:cPref,game:cGame,notes:b.notes||null,status:b.status||'pending',staff_note:null,member_id:cMember,telegram_chat_id:cTg,source:b.source||null,created_at:now,updated_at:now};
    store.rows.push(newRow);_bkWrite(store);
    res.status(201).json(_normalizeBk(newRow));
  }).catch(e=>res.status(500).json({error:String(e)}));
});

// PATCH /api/bookings/:id/status
app.patch("/api/bookings/:id/status", (req, res) => {
  const id=parseInt(req.params.id);if(isNaN(id))return res.status(400).json({error:"Invalid id"});
  const {status,staffNote,consoleId}=req.body;
  if(!status)return res.status(400).json({error:"status required"});
  _withBkLock(() => {
    const store=_bkRead();
    const idx=store.rows.findIndex(r=>r.id===id);
    if(idx<0){res.status(404).json({error:"Booking not found"});return;}
    const booking=store.rows[idx];
    if(status==='confirmed'&&(consoleId||booking.console_id)){
      const cCon=consoleId||booking.console_id;
      const existing=store.rows.filter(r=>r.date===booking.date&&r.console_id===cCon&&r.status==='confirmed'&&r.id!==id);
      for(const r of existing){
        if(overlaps(booking.time_slot||'00:00',booking.duration_mins||60,r.time_slot||'00:00',r.duration_mins||60)){
          res.status(409).json({error:"console_conflict",message:`${cCon} is already booked at ${r.time_slot} (${r.customer_name} — Booking #${r.id})`,conflictingBookingId:r.id});
          return;
        }
      }
    }
    booking.status=status;booking.updated_at=new Date().toISOString();
    if(staffNote!==undefined)booking.staff_note=staffNote;
    if(consoleId!==undefined)booking.console_id=consoleId;
    _bkWrite(store);_cache.delete("consoles");
    res.json(_normalizeBk(booking));
  }).catch(e=>res.status(500).json({error:String(e)}));
});

// DELETE /api/bookings/:id
app.delete("/api/bookings/:id", (req, res) => {
  const id=parseInt(req.params.id);if(isNaN(id))return res.status(400).json({error:"Invalid id"});
  _withBkLock(() => {
    const store=_bkRead();const idx=store.rows.findIndex(r=>r.id===id);
    if(idx<0){res.status(404).json({error:"Booking not found"});return;}
    store.rows.splice(idx,1);_bkWrite(store);
    res.json({ok:true});
  }).catch(e=>res.status(500).json({error:String(e)}));
});

// GET /api/sheets/sales-all — full Sales_Daily history
app.get("/api/sheets/sales-all", sheetsRoute("sales-all", async () => {
  const rows = await sheetGet("Sales_Daily!A:O");
  const records = rows.slice(1).filter(r=>(r[0]??'').trim()).map(r=>({
    date:      (r[0]??'').trim(),
    voucher:   (r[1]??'').trim(),
    member:    (r[2]??'').trim()||'0 (Guest)',
    console_id:(r[3]??'').trim(),
    play_mins: parseNum(r[4]??'0'),
    game_amt:  parseNum(r[5]??'0'),
    food_total:parseNum(r[6]??'0'),
    discount:  parseNum(r[7]??'0'),
    net_total: parseNum(r[8]??'0'),
    kpay:      parseNum(r[9]??'0'),
    cash:      parseNum(r[10]??'0'),
    staff:     (r[14]??'').trim(),
  })).reverse();
  return { records };
}));

// GET /api/sheets/stock-in — Stock_In restock history
app.get("/api/sheets/stock-in", sheetsRoute("stock-in", async () => {
  const rows = await sheetGet("Stock_In!A:H");
  const entries = rows.slice(1).filter(r=>(r[0]??'').trim()&&(r[1]??'').trim()).map(r=>({
    date:      (r[0]??'').trim(),
    item_name: (r[1]??'').trim(),
    qty_in:    parseNum(r[2]??'0'),
    unit_cost: parseNum(r[3]??'0'),
    total_cost:parseNum(r[4]??'0'),
    payment:   (r[5]??'').trim(),
    remark:    (r[6]??'').trim()||'',
  })).reverse();
  return { entries };
}));

// GET /api/sheets/payroll?month=YYYY-MM — staff payroll calculation
app.get("/api/sheets/payroll", sheetsRoute("payroll", async (req) => {
  const now = new Date(Date.now() + 6.5*60*60*1000);
  const monthStr = String(req.query.month ?? `${now.getUTCFullYear()}-${String(now.getUTCMonth()+1).padStart(2,'0')}`);
  const [yr, mo] = monthStr.split('-').map(Number);
  if (!yr||!mo) return { month: monthStr, staff: [] };

  const [salesRows, walletRows, topupRows, settingRows] = await sheetsGetBatch([
    "Sales_Daily!A:O", "Card_Wallet!A:O", "TopUp_Log!A:I", "Setting!A1:W30"
  ]);

  // Staff list with base salary from Setting col S/T (idx 18/19)
  const staffCfg = [];
  for (const r of settingRows.slice(1)) {
    const sname = (r[18]??'').trim();
    if (sname) staffCfg.push({ name: sname, base_salary: parseNum(r[19]??'0') });
  }
  if (!staffCfg.length) return { month: monthStr, staff: [] };

  const staffNames = new Set(staffCfg.map(s=>s.name));
  const staffMins = {}, staffFoodComm = {}, staffFoodDays = {}, staffNm = {};
  for (const s of staffCfg) { staffMins[s.name]=0; staffFoodComm[s.name]=0; staffFoodDays[s.name]=0; staffNm[s.name]=0; }

  // Monthly sales → play mins per staff + food per day
  const monthSales = salesRows.slice(1).filter(r=>inMonth(r[0]??'', yr, mo));
  const dailyFood = {}, dailyStaff = {};
  for (const r of monthSales) {
    const sname=(r[14]??'').trim(), date=(r[0]??'').trim(), food=parseNum(r[6]??'0');
    if (sname && staffNames.has(sname)) staffMins[sname] += parseNum(r[4]??'0');
    if (date) {
      dailyFood[date]=(dailyFood[date]??0)+food;
      if (!dailyStaff[date]) dailyStaff[date]=new Set();
      if (sname && staffNames.has(sname)) dailyStaff[date].add(sname);
    }
  }
  // Food commission: 5% of food on days ≥50,000, split equally among present staff
  for (const [date, food] of Object.entries(dailyFood)) {
    if (food < 50000) continue;
    const present = [...(dailyStaff[date]??new Set())];
    const perStaff = present.length ? (food*0.05)/present.length : 0;
    for (const sname of present) { staffFoodComm[sname]+=perStaff; staffFoodDays[sname]++; }
  }

  // NM count per staff from TopUp_Log First Purchase + Card_Wallet reg_staff
  const walletRegStaff = new Map();
  for (const r of walletRows.slice(1)) {
    const mId=(r[1]??'').trim(); if (mId) walletRegStaff.set(mId,(r[10]??'').trim());
  }
  for (const r of topupRows.slice(1)) {
    if (!inMonth(r[0]??'',yr,mo)) continue;
    if ((r[8]??'').trim()!=='First Purchase') continue;
    const mId=(r[1]??'').trim(), sname=walletRegStaff.get(mId)??'';
    if (staffNm[sname]!==undefined) staffNm[sname]++;
  }

  // Game bonus thresholds (Setting col U/V idx 20/21 if set, otherwise defaults)
  const bonus1500 = parseNum(settingRows[1]?.[20]??'0')||50000;
  const bonus2000 = parseNum(settingRows[1]?.[21]??'0')||100000;

  const staff = staffCfg.map(s => {
    const play_mins = staffMins[s.name]??0;
    const play_hrs  = Math.round((play_mins/60)*10)/10;
    const game_bonus     = play_hrs>=2000 ? bonus2000 : play_hrs>=1500 ? bonus1500 : 0;
    const nm_count       = staffNm[s.name]??0;
    const nm_commission  = nm_count*1500;
    const food_commission= Math.round(staffFoodComm[s.name]??0);
    const food_days      = staffFoodDays[s.name]??0;
    const total_commission = game_bonus+nm_commission+food_commission;
    const grand_total      = s.base_salary+total_commission;
    return { name:s.name, base_salary:s.base_salary, play_hrs, play_mins, game_bonus,
             nm_count, nm_commission, food_commission, food_days, total_commission, grand_total };
  });
  return { month: monthStr, staff };
}));

// POST /api/sheets/payroll/export?month=YYYY-MM — write payroll to Salary_Payroll sheet
app.post("/api/sheets/payroll/export", async (req, res) => {
  try {
    const now = new Date(Date.now()+6.5*60*60*1000);
    const monthStr = String(req.query.month ?? `${now.getUTCFullYear()}-${String(now.getUTCMonth()+1).padStart(2,'0')}`);
    const [yr, mo] = monthStr.split('-').map(Number);
    if (!yr||!mo) return res.status(400).json({ error: "Invalid month" });

    const [salesRows, walletRows, topupRows, settingRows] = await sheetsGetBatch([
      "Sales_Daily!A:O","Card_Wallet!A:O","TopUp_Log!A:I","Setting!A1:W30"
    ]);
    const staffCfg = [];
    for (const r of settingRows.slice(1)) {
      const sname=(r[18]??'').trim(); if(sname) staffCfg.push({name:sname,base_salary:parseNum(r[19]??'0')});
    }
    if (!staffCfg.length) return res.status(400).json({ error: "No staff configured in Setting!S2:S3" });

    const staffNames=new Set(staffCfg.map(s=>s.name));
    const staffMins={},staffFoodComm={},staffFoodDays={},staffNm={};
    for(const s of staffCfg){staffMins[s.name]=0;staffFoodComm[s.name]=0;staffFoodDays[s.name]=0;staffNm[s.name]=0;}
    const monthSales=salesRows.slice(1).filter(r=>inMonth(r[0]??'',yr,mo));
    const dailyFood={},dailyStaff={};
    for(const r of monthSales){
      const sname=(r[14]??'').trim(),date=(r[0]??'').trim(),food=parseNum(r[6]??'0');
      if(sname&&staffNames.has(sname))staffMins[sname]+=parseNum(r[4]??'0');
      if(date){dailyFood[date]=(dailyFood[date]??0)+food;if(!dailyStaff[date])dailyStaff[date]=new Set();if(sname&&staffNames.has(sname))dailyStaff[date].add(sname);}
    }
    for(const[date,food]of Object.entries(dailyFood)){
      if(food<50000)continue;
      const present=[...(dailyStaff[date]??new Set())],perStaff=present.length?(food*0.05)/present.length:0;
      for(const sn of present){staffFoodComm[sn]+=perStaff;staffFoodDays[sn]++;}
    }
    const walletRegStaff=new Map();
    for(const r of walletRows.slice(1)){const mId=(r[1]??'').trim();if(mId)walletRegStaff.set(mId,(r[10]??'').trim());}
    for(const r of topupRows.slice(1)){
      if(!inMonth(r[0]??'',yr,mo)||(r[8]??'').trim()!=='First Purchase')continue;
      const mId=(r[1]??'').trim(),sname=walletRegStaff.get(mId)??'';
      if(staffNm[sname]!==undefined)staffNm[sname]++;
    }
    const bonus1500=parseNum(settingRows[1]?.[20]??'0')||50000;
    const bonus2000=parseNum(settingRows[1]?.[21]??'0')||100000;

    const header=["Month","Staff","Base Salary","Play Hrs","Game Bonus","NM Count","NM Commission","Food Days","Food Commission","Total Commission","Grand Total"];
    const rows=[header];
    for(const s of staffCfg){
      const play_mins=staffMins[s.name]??0,play_hrs=Math.round((play_mins/60)*10)/10;
      const game_bonus=play_hrs>=2000?bonus2000:play_hrs>=1500?bonus1500:0;
      const nm_count=staffNm[s.name]??0,nm_commission=nm_count*1500;
      const food_commission=Math.round(staffFoodComm[s.name]??0),food_days=staffFoodDays[s.name]??0;
      const total_commission=game_bonus+nm_commission+food_commission;
      const grand_total=s.base_salary+total_commission;
      rows.push([monthStr,s.name,s.base_salary,play_hrs,game_bonus,nm_count,nm_commission,food_days,food_commission,total_commission,grand_total]);
    }
    await sheetAppend("Salary_Payroll!A:K", rows);
    res.json({ ok: true, month: monthStr, staff_count: staffCfg.length });
  } catch(e) { res.status(500).json({ error: String(e.message) }); }
});

// ═══════════════════════════════════════════════════════════════════════════════
// FINANCE MODULE
// Sheets used:
//   Capital_Setup     A=Shareholder, B=Role, C=Capital, D=Ownership%
//   Assets_Register   A=Name, B=Category, C=PurchaseDate, D=Cost, E=UsefulYrs,
//                     F=SalvageVal, G=Status(Active/Disposed), H=Notes
//   OPEX_Log          A=Date, B=Category, C=Description, D=Amount, E=Account,
//                     F=PaymentType(Cash/KBZ/AYA/MMQR), G=Reference, H=Notes
//   Accounts          A=AccountName, B=Type, C=OpeningBalance, D=Notes
//   Account_Transfers A=Date, B=FromAccount, C=ToAccount, D=Amount, E=Notes, F=Ref
//   Payables          A=Date, B=Vendor, C=Description, D=Amount, E=DueDate,
//                     F=Status(Pending/Paid), G=PaidDate, H=Account, I=Notes
//   Receivables       A=Date, B=Customer, C=Description, D=Amount, E=DueDate,
//                     F=Status(Pending/Received), G=ReceivedDate, H=Account, I=Notes
//   Advance_Staff     A=Date, B=Staff, C=Amount, D=PaymentType, E=Notes, F=Deducted(Y/N)
// ── Finance helpers ───────────────────────────────────────────────────────────

const BUSINESS_START = new Date('2026-06-01'); // June 1 2026

function monthsSinceStart(year, month) {
  const ms = (year - 2026) * 12 + (month - 6);
  return Math.max(0, ms);
}

// Straight-line depreciation per month for one asset
function depreciationPerMonth(cost, salvage, usefulYrs) {
  if (!usefulYrs || usefulYrs <= 0) return 0;
  return (cost - salvage) / (usefulYrs * 12);
}

// Accumulated depreciation up to end of given month
function accumulatedDep(cost, salvage, usefulYrs, purchaseDateStr, year, month) {
  const purchase = new Date(purchaseDateStr);
  if (isNaN(purchase.getTime())) return 0;
  // Only start depreciating from BUSINESS_START or purchase date, whichever is later
  const start = purchase < BUSINESS_START ? BUSINESS_START : purchase;
  const startYr = start.getFullYear(), startMo = start.getMonth() + 1;
  // months elapsed from start to end of target month
  const elapsed = (year - startYr) * 12 + (month - startMo) + 1;
  if (elapsed <= 0) return 0;
  const totalMonths = usefulYrs * 12;
  const months = Math.min(elapsed, totalMonths);
  return Math.round(((cost - salvage) / totalMonths) * months);
}

function inMonthDate(dateStr, yr, mo) {
  // accepts M/D/YYYY or YYYY-MM-DD
  if (!dateStr) return false;
  if (dateStr.includes('-')) {
    const [y,m] = dateStr.split('-').map(Number);
    return y === yr && m === mo;
  }
  const p = dateStr.split('/');
  if (p.length === 3) return parseInt(p[2]) === yr && parseInt(p[0]) === mo;
  return false;
}

// Returns true if dateStr (M/D/YYYY or YYYY-MM-DD) is strictly before BUSINESS_START
function isBeforeBusinessStart(dateStr) {
  if (!dateStr) return false;
  let y, m;
  if (dateStr.includes('-')) {
    [y, m] = dateStr.split('-').map(Number);
  } else {
    const p = dateStr.split('/');
    if (p.length < 3) return false;
    m = parseInt(p[0]); y = parseInt(p[2]);
  }
  if (!y || !m) return false;
  const BYR = BUSINESS_START.getUTCFullYear(), BMO = BUSINESS_START.getUTCMonth() + 1;
  return y < BYR || (y === BYR && m < BMO);
}

// ── Prepaid Expenses amortization ─────────────────────────────────────────────
// Reads Prepaid_Expenses rows (already fetched) and returns {category: monthlyKs}
// for the target year/month if that month falls within the prepaid period.
function calcPrepaidAmort(prepaidRows, yr, mo) {
  const amort = {};
  for (const r of (prepaidRows ?? []).slice(1)) {
    if (!(r[2] ?? '').trim()) continue;          // no total paid
    const cat       = (r[1] ?? 'Rent').trim() || 'Rent';
    const totalPaid = parseNum(r[2] ?? '0');
    const startStr  = (r[3] ?? '').trim();
    const endStr    = (r[4] ?? '').trim();
    if (!startStr || !endStr) continue;
    const start = new Date(startStr);
    const end   = new Date(endStr);
    if (isNaN(start.getTime()) || isNaN(end.getTime())) continue;
    // Check target month overlaps prepaid period
    const mStart = new Date(yr, mo - 1, 1);
    const mEnd   = new Date(yr, mo, 0);
    if (mStart > end || mEnd < start) continue;
    const totalMonths = Math.max(1,
      (end.getFullYear() - start.getFullYear()) * 12 + (end.getMonth() - start.getMonth())
    );
    const monthlyAmt = Math.round(totalPaid / totalMonths);
    amort[cat] = (amort[cat] ?? 0) + monthlyAmt;
  }
  return amort;
}

// GET /api/finance/shareholders
app.get("/api/finance/shareholders", async (_req, res) => {
  try {
    const rows = await sheetGet("Capital_Setup!A:D");
    const shareholders = rows.slice(1).filter(r => (r[0]??'').trim()).map(r => ({
      name:      (r[0]??'').trim(),
      role:      (r[1]??'').trim()||'Silent Partner',
      capital:   parseNum(r[2]??'0'),
      ownership: parseNum(r[3]??'0'),
    }));
    const totalCapital = shareholders.reduce((s,r) => s + r.capital, 0);
    const opPartner = shareholders.find(s => s.role.toLowerCase().includes('operation'));
    res.json({ shareholders, total_capital: totalCapital, op_partner: opPartner?.name ?? null });
  } catch(e) { res.status(500).json({ error: String(e.message) }); }
});

// Parse M/D/YYYY or YYYY-MM-DD → Date
function parseAssetDate(s) {
  if (!s) return null;
  if (s.includes('/')) { const [m,d,y]=s.split('/'); return new Date(`${y}-${m.padStart(2,'0')}-${d.padStart(2,'0')}`); }
  return new Date(s);
}

// NBV per unit at a given date
function nbvPerUnitAtDate(unitCost, salvagePerUnit, usefulYrs, purchaseDateStr, asOfDate) {
  const purchase = parseAssetDate(purchaseDateStr);
  if (!purchase || isNaN(purchase.getTime()) || !usefulYrs) return unitCost;
  const start = purchase < BUSINESS_START ? BUSINESS_START : purchase;
  const elapsed = (asOfDate.getFullYear()-start.getFullYear())*12+(asOfDate.getMonth()-start.getMonth())+1;
  const totalMonths = usefulYrs*12;
  const months = Math.max(0, Math.min(elapsed, totalMonths));
  const accDep = totalMonths > 0 ? Math.round(((unitCost-salvagePerUnit)/totalMonths)*months) : 0;
  return Math.max(salvagePerUnit, unitCost-accDep);
}

// GET /api/finance/assets
app.get("/api/finance/assets", async (_req, res) => {
  try {
    const now = new Date(Date.now() + 6.5*60*60*1000);
    const yr = now.getUTCFullYear(), mo = now.getUTCMonth() + 1;
    const rows = await sheetGet("Assets_Register!A:L");
    const assets = rows.slice(1).filter(r => (r[0]??'').trim()).map(r => {
      const unitCost   = parseNum(r[3]??'0');
      const qty        = Math.max(1, parseNum(r[4]??'1') || 1);
      const usefulYrs  = parseNum(r[5]??'0');
      const salvagePU  = parseNum(r[6]??'0');
      const purchaseDate = (r[2]??'').trim();
      const status     = (r[7]??'Active').trim();
      const disposalDate = (r[8]??'').trim();
      const disposedQty  = parseNum(r[9]??'0');
      const disposalProceeds = parseNum(r[10]??'0');
      const notes      = (r[11]??'').trim();

      const totalCost  = unitCost * qty;
      const totalSalvage = salvagePU * qty;
      const activeQty  = Math.max(0, qty - disposedQty);

      // Depreciation on active (remaining) units only
      const activeCost    = unitCost * activeQty;
      const activeSalvage = salvagePU * activeQty;
      const depMonth = status !== 'Disposed'
        ? depreciationPerMonth(activeCost, activeSalvage, usefulYrs) : 0;
      const accDep = (status === 'Active' || status === 'Partially Disposed')
        ? accumulatedDep(activeCost, activeSalvage, usefulYrs, purchaseDate, yr, mo) : 0;
      const bookValue = status === 'Disposed' ? 0 : Math.max(activeSalvage, activeCost - accDep);

      // Disposal gain/loss
      let disposal_gain_loss = null;
      if (disposedQty > 0 && disposalDate) {
        const asOf = parseAssetDate(disposalDate) ?? now;
        const nbvDisposed = nbvPerUnitAtDate(unitCost, salvagePU, usefulYrs, purchaseDate, asOf) * disposedQty;
        disposal_gain_loss = disposalProceeds - nbvDisposed;
      }

      return {
        name: (r[0]??'').trim(), category: (r[1]??'').trim(),
        purchase_date: purchaseDate,
        unit_cost: unitCost, qty, total_cost: totalCost,
        salvage_per_unit: salvagePU, total_salvage: totalSalvage,
        useful_yrs: usefulYrs, active_qty: activeQty,
        dep_per_month: Math.round(depMonth),
        acc_depreciation: accDep, book_value: bookValue,
        status, disposal_date: disposalDate,
        disposed_qty: disposedQty, disposal_proceeds: disposalProceeds,
        disposal_gain_loss, notes,
      };
    });
    const totalCost = assets.reduce((s,a) => s + a.total_cost, 0);
    const totalAccDep = assets.reduce((s,a) => s + a.acc_depreciation, 0);
    const totalBookValue = assets.reduce((s,a) => s + a.book_value, 0);
    const totalDepMonth = assets.filter(a=>a.status!=='Disposed').reduce((s,a) => s + a.dep_per_month, 0);
    const totalGainLoss = assets.reduce((s,a) => s + (a.disposal_gain_loss ?? 0), 0);
    res.json({ assets, total_cost: totalCost, total_acc_dep: totalAccDep, total_book_value: totalBookValue, total_dep_per_month: Math.round(totalDepMonth), total_disposal_gain_loss: totalGainLoss });
  } catch(e) { res.status(500).json({ error: String(e.message) }); }
});

// GET /api/finance/depreciation?year=YYYY — full yearly depreciation schedule
app.get("/api/finance/depreciation", async (req, res) => {
  try {
    const yr = parseInt(req.query.year ?? new Date().getFullYear());
    const rows = await sheetGet("Assets_Register!A:L");
    const assets = rows.slice(1).filter(r => (r[0]??'').trim() && (r[7]??'Active').trim() !== 'Disposed').map(r => {
      const unitCost = parseNum(r[3]??'0');
      const qty      = Math.max(1, parseNum(r[4]??'1') || 1);
      const usefulYrs = parseNum(r[5]??'0');
      const salvagePU = parseNum(r[6]??'0');
      const disposedQty = parseNum(r[9]??'0');
      const activeQty = Math.max(0, qty - disposedQty);
      return {
        name: (r[0]??'').trim(), category: (r[1]??'').trim(),
        purchase_date: (r[2]??'').trim(),
        cost: unitCost * activeQty, salvage: salvagePU * activeQty, useful_yrs: usefulYrs,
      };
    });
    const months = Array.from({length:12},(_,i)=>i+1);
    const schedule = assets.map(a => {
      const monthly = months.map(mo => {
        const dep = accumulatedDep(a.cost, a.salvage, a.useful_yrs, a.purchase_date, yr, mo);
        const prev = mo > 1 ? accumulatedDep(a.cost, a.salvage, a.useful_yrs, a.purchase_date, yr, mo-1) : accumulatedDep(a.cost, a.salvage, a.useful_yrs, a.purchase_date, yr-1, 12);
        return Math.max(0, dep - prev);
      });
      const yearTotal = monthly.reduce((s,v)=>s+v,0);
      const accToYearEnd = accumulatedDep(a.cost, a.salvage, a.useful_yrs, a.purchase_date, yr, 12);
      const bookEnd = Math.max(a.salvage, a.cost - accToYearEnd);
      return { ...a, monthly, year_total: yearTotal, acc_to_year_end: accToYearEnd, book_value_end: bookEnd };
    });
    const monthlyTotals = months.map((_,i) => schedule.reduce((s,a)=>s+a.monthly[i],0));
    res.json({ year: yr, schedule, monthly_totals: monthlyTotals, year_total: monthlyTotals.reduce((s,v)=>s+v,0) });
  } catch(e) { res.status(500).json({ error: String(e.message) }); }
});

// GET /api/finance/accounts — account balances (opening + all transactions)
app.get("/api/finance/accounts", async (_req, res) => {
  try {
    const [accRows, txRows, opexRows, salesRows, topupRows, payRows, recRows, prepaidRows, advpayRows, assetAccRows] = await sheetsGetBatch([
      "Accounts!A:D", "Account_Transfers!A:F",
      "OPEX_Log!A:H", "Sales_Daily!A:K", "TopUp_Log!A:I",
      "Payables!A:I", "Receivables!A:I", "Prepaid_Expenses!A:F", "Advance_Payments!A:H",
      "Assets_Register!A:M"
    ]);
    const accounts = accRows.slice(1).filter(r=>(r[0]??'').trim()).map(r=>({
      name: (r[0]??'').trim(), type: (r[1]??'').trim(),
      opening: parseNum(r[2]??'0'), notes: (r[3]??'').trim(),
    }));

    // Build balance map starting from opening
    const bal = {};
    for (const a of accounts) bal[a.name] = a.opening;

    // Sales_Daily inflows → KPay=MMQR, Cash=Cash Box
    for (const r of salesRows.slice(1)) {
      const kpay = parseNum(r[9]??'0'), cash = parseNum(r[10]??'0');
      if (bal['MMQR'] !== undefined) bal['MMQR'] += kpay; else if (bal['KPay'] !== undefined) bal['KPay'] += kpay;
      if (bal['Cash Box'] !== undefined) bal['Cash Box'] += cash;
    }
    // TopUp inflows
    for (const r of topupRows.slice(1)) {
      const kpay = parseNum(r[5]??'0'), cash = parseNum(r[6]??'0');
      if (bal['MMQR'] !== undefined) bal['MMQR'] += kpay; else if (bal['KPay'] !== undefined) bal['KPay'] += kpay;
      if (bal['Cash Box'] !== undefined) bal['Cash Box'] += cash;
    }
    // OPEX_Log outflows (col E = Account)
    for (const r of opexRows.slice(1)) {
      const amt = parseNum(r[3]??'0'), acct = (r[4]??'').trim();
      if (bal[acct] !== undefined) bal[acct] -= amt;
    }
    // Transfers
    const transfers = txRows.slice(1).filter(r=>(r[0]??'').trim());
    for (const r of transfers) {
      const from=(r[1]??'').trim(),to=(r[2]??'').trim(),amt=parseNum(r[3]??'0');
      if (bal[from] !== undefined) bal[from] -= amt;
      if (bal[to]   !== undefined) bal[to]   += amt;
    }
    // Receivables (Pending) — money lent/given out, expecting return → deduct from account
    // Receivables (Received) — money came back → add back (net = no change from original deduction)
    for (const r of (recRows??[]).slice(1)) {
      if (!(r[0]??'').trim()) continue;
      const amt = parseNum(r[3]??'0'), status = (r[5]??'').trim().toLowerCase(), acct = (r[7]??'').trim();
      if (!acct || !amt) continue;
      if (bal[acct] !== undefined) {
        bal[acct] -= amt;                        // always deduct (money went out)
        if (status === 'received') bal[acct] += amt; // restore if already received
      }
    }
    // Payables (Paid) — money actually paid out → deduct from account
    for (const r of (payRows??[]).slice(1)) {
      if (!(r[0]??'').trim()) continue;
      const amt = parseNum(r[3]??'0'), status = (r[5]??'').trim().toLowerCase(), acct = (r[7]??'').trim();
      if (!acct || !amt) continue;
      if (status === 'paid' && bal[acct] !== undefined) bal[acct] -= amt;
    }
    // Prepaid_Expenses — full payment went out of account on entry date
    // col A=desc, B=cat, C=totalPaid, D=start, E=end, F=account
    for (const r of (prepaidRows??[]).slice(1)) {
      if (!(r[0]??'').trim()) continue;
      const amt = parseNum(r[2]??'0'), acct = (r[5]??'').trim();
      if (!acct || !amt) continue;
      if (bal[acct] !== undefined) bal[acct] -= amt;
    }
    // Advance_Payments — cash paid out immediately regardless of status
    // col A=date,B=party,C=desc,D=amount,E=account,F=due,G=status,H=notes
    for (const r of (advpayRows??[]).slice(1)) {
      if (!(r[0]??'').trim()) continue;
      const amt = parseNum(r[3]??'0'), acct = (r[4]??'').trim();
      if (!acct || !amt) continue;
      if (bal[acct] !== undefined) bal[acct] -= amt;
    }
    // Assets_Register — purchase cost paid from account (col D=unitCost, E=qty, M=account)
    // Skip disposed assets; deduct full cost (unit_cost × qty) from the paying account
    for (const r of (assetAccRows??[]).slice(1)) {
      if (!(r[0]??'').trim()) continue;
      const status = (r[7]??'Active').trim();
      if (status === 'Disposed') continue;
      const unitCost = parseNum(r[3]??'0'), qty = Math.max(1, parseNum(r[4]??'1') || 1);
      const acct = (r[12]??'').trim();  // col M = account
      if (!acct || !unitCost) continue;
      if (bal[acct] !== undefined) bal[acct] -= unitCost * qty;
    }

    const result = accounts.map(a => ({ ...a, balance: Math.round(bal[a.name] ?? a.opening) }));
    const totalBalance = result.reduce((s,a) => s + a.balance, 0);
    res.json({ accounts: result, total_balance: totalBalance, transfers: transfers.slice(-20).reverse() });
  } catch(e) { res.status(500).json({ error: String(e.message) }); }
});

// POST /api/finance/transfer — account transfer
app.post("/api/finance/transfer", async (req, res) => {
  try {
    const { from_account, to_account, amount, notes, ref } = req.body || {};
    if (!from_account || !to_account || !amount) return res.status(400).json({ error: 'from_account, to_account, amount required' });
    const now = new Date(Date.now() + 6.5*60*60*1000);
    const date = `${now.getUTCMonth()+1}/${now.getUTCDate()}/${now.getUTCFullYear()}`;
    await sheetAppend("Account_Transfers!A:F", [[date, from_account, to_account, amount, notes||'', ref||'']]);
    res.json({ ok: true, date, from_account, to_account, amount });
  } catch(e) { res.status(500).json({ error: String(e.message) }); }
});

// GET /api/finance/opex?month=YYYY-MM
app.get("/api/finance/opex", async (req, res) => {
  try {
    const now = new Date(Date.now() + 6.5*60*60*1000);
    const monthStr = String(req.query.month ?? `${now.getUTCFullYear()}-${String(now.getUTCMonth()+1).padStart(2,'0')}`);
    const [yr, mo] = monthStr.split('-').map(Number);
    const rows = await sheetGet("OPEX_Log!A:I");
    const all = rows.slice(1).filter(r => (r[0]??'').trim());
    const monthly = all.filter(r => inMonthDate(r[0], yr, mo));
    const byCategory = {};
    for (const r of monthly) {
      const cat = (r[1]??'Uncategorized').trim();
      if (!byCategory[cat]) byCategory[cat] = { category: cat, total: 0, items: [] };
      const amt = parseNum(r[3]??'0');
      byCategory[cat].total += amt;
      byCategory[cat].items.push({
        date: (r[0]??'').trim(), description: (r[2]??'').trim(),
        amount: amt, account: (r[4]??'').trim(), payment_type: (r[5]??'').trim(),
        reference: (r[6]??'').trim(), notes: (r[7]??'').trim(),
      });
    }
    const categories = Object.values(byCategory).sort((a,b) => b.total - a.total);
    const totalOpex = categories.reduce((s,c) => s + c.total, 0);
    res.json({ month: monthStr, categories, total_opex: totalOpex });
  } catch(e) { res.status(500).json({ error: String(e.message) }); }
});

// POST /api/finance/opex — add OPEX entry
app.post("/api/finance/opex", async (req, res) => {
  try {
    const { date, category, description, amount, account, payment_type, reference, notes } = req.body || {};
    if (!category || !amount) return res.status(400).json({ error: 'category and amount required' });
    const now = new Date(Date.now() + 6.5*60*60*1000);
    const d = date || `${now.getUTCMonth()+1}/${now.getUTCDate()}/${now.getUTCFullYear()}`;
    await sheetAppend("OPEX_Log!A:H", [[d, category, description||'', amount, account||'', payment_type||'Cash', reference||'', notes||'']]);
    res.json({ ok: true, date: d, category, amount });
  } catch(e) { res.status(500).json({ error: String(e.message) }); }
});

// GET /api/finance/pnl?month=YYYY-MM (or ?m=YYYY-MM) — Monthly P&L with OPEX + Depreciation
app.get("/api/finance/pnl", async (req, res) => {
  try {
    const now = new Date(Date.now() + 6.5*60*60*1000);
    const monthStr = String(req.query.month ?? req.query.m ?? `${now.getUTCFullYear()}-${String(now.getUTCMonth()+1).padStart(2,'0')}`);
    const [yr, mo] = monthStr.split('-').map(Number);

    const [salesRows, topupRows, soRows, opexRows, assetRows, advRows, prepaidRows] = await sheetsGetBatch([
      "Sales_Daily!A:O", "TopUp_Log!A:I", "Stock_Out!A:H",
      "OPEX_Log!A:H", "Assets_Register!A:L", "Advance_Staff!A:F", "Prepaid_Expenses!A:E"
    ]);

    // Revenue
    let gameRev=0, foodRev=0, discounts=0, salesKpay=0, salesCash=0;
    for (const r of salesRows.slice(1)) {
      if (!inMonth(r[0]??'', yr, mo)) continue;
      gameRev   += parseNum(r[5]??'0');
      foodRev   += parseNum(r[6]??'0');
      discounts += parseNum(r[7]??'0');
      salesKpay += parseNum(r[9]??'0');
      salesCash += parseNum(r[10]??'0');
    }
    let topupRev=0, topupKpay=0, topupCash=0;
    for (const r of topupRows.slice(1)) {
      if (!inMonth(r[0]??'', yr, mo)) continue;
      topupRev  += parseNum(r[4]??'0');
      topupKpay += parseNum(r[5]??'0');
      topupCash += parseNum(r[6]??'0');
    }
    const totalRevenue = gameRev + foodRev - discounts;

    // COGS (stock out cost)
    let cogs = 0;
    for (const r of soRows.slice(1)) { if (inMonth(r[0]??'', yr, mo)) cogs += parseNum(r[7]??'0'); }
    const grossProfit = totalRevenue - cogs;

    // OPEX by category (from OPEX_Log)
    // Pre-opening entries (date < BUSINESS_START) are bucketed into the first business month (June 2026)
    const BIZ_YR = BUSINESS_START.getUTCFullYear(), BIZ_MO = BUSINESS_START.getUTCMonth() + 1;
    const isFirstBizMonth = yr === BIZ_YR && mo === BIZ_MO;
    const opexByCat = {};
    let totalOpex = 0;
    for (const r of opexRows.slice(1)) {
      const dateStr = r[0]??'';
      const isThisMonth = inMonthDate(dateStr, yr, mo);
      const isPreOpening = !isThisMonth && isFirstBizMonth && isBeforeBusinessStart(dateStr);
      if (!isThisMonth && !isPreOpening) continue;
      const cat = (r[1]??'Other').trim();
      const amt = parseNum(r[3]??'0');
      opexByCat[cat] = (opexByCat[cat]??0) + amt;
      totalOpex += amt;
    }
    // Prepaid amortization — auto-spread prepaid rent/expenses monthly
    const prepaidAmort = calcPrepaidAmort(prepaidRows, yr, mo);
    for (const [cat, amt] of Object.entries(prepaidAmort)) {
      opexByCat[cat] = (opexByCat[cat] ?? 0) + amt;
      totalOpex += amt;
    }

    // Depreciation + Disposal Gain/Loss this month
    let totalDep = 0;
    let disposalGainLoss = 0;
    for (const r of assetRows.slice(1)) {
      const status        = (r[7]??'Active').trim();
      const unitCost      = parseNum(r[3]??'0');
      const qty           = Math.max(1, parseNum(r[4]??'1') || 1);
      const usefulYrs     = parseNum(r[5]??'0');
      const salvagePU     = parseNum(r[6]??'0');
      const purchDate     = (r[2]??'').trim();
      const disposalDateStr = (r[8]??'').trim();
      const disposedQty   = parseNum(r[9]??'0');
      const proceeds      = parseNum(r[10]??'0');

      // Depreciation for remaining active units (skip fully disposed)
      if (status !== 'Disposed') {
        const activeQty = Math.max(0, qty - disposedQty);
        if (activeQty > 0) {
          const cost = unitCost * activeQty, salvage = salvagePU * activeQty;
          const dep = accumulatedDep(cost, salvage, usefulYrs, purchDate, yr, mo)
                    - accumulatedDep(cost, salvage, usefulYrs, purchDate, mo>1?yr:yr-1, mo>1?mo-1:12);
          totalDep += Math.max(0, dep);
        }
      }

      // Disposal gain/loss — only in the exact month the disposal occurred
      if ((status === 'Disposed' || status === 'Partially Disposed') && disposedQty > 0 && disposalDateStr) {
        if (inMonthDate(disposalDateStr, yr, mo)) {
          // For fully disposed assets, still include depreciation for those units up to disposal month
          if (status === 'Disposed') {
            const cost = unitCost * qty, salvage = salvagePU * qty;
            const dep = accumulatedDep(cost, salvage, usefulYrs, purchDate, yr, mo)
                      - accumulatedDep(cost, salvage, usefulYrs, purchDate, mo>1?yr:yr-1, mo>1?mo-1:12);
            totalDep += Math.max(0, dep);
          }
          // NBV of disposed units at the disposal month
          const costDisp    = unitCost * disposedQty;
          const salvageDisp = salvagePU * disposedQty;
          const accDep      = accumulatedDep(costDisp, salvageDisp, usefulYrs, purchDate, yr, mo);
          const nbv         = Math.max(0, costDisp - accDep);
          disposalGainLoss += proceeds - nbv;
        }
      }
    }

    // Staff advance (payroll OPEX — only if not already in OPEX_Log)
    let advTotal = 0;
    for (const r of advRows.slice(1)) { if (inMonthDate(r[0]??'', yr, mo)) advTotal += parseNum(r[2]??'0'); }

    const ebitda = grossProfit - totalOpex;
    const ebit   = ebitda - totalDep + disposalGainLoss;

    // OM bonus = 10% of EBIT (after depreciation + disposal G/L), only if EBIT > 0
    const omBonus = ebit > 0 ? Math.round(ebit * 0.10) : 0;
    const netProfit = ebit - omBonus;

    res.json({
      month: monthStr,
      revenue: { game: gameRev, food: foodRev, discounts, total: totalRevenue },
      topup: { total: topupRev, kpay: topupKpay, cash: topupCash },
      cogs, gross_profit: grossProfit,
      opex: opexByCat, total_opex: totalOpex,
      depreciation: Math.round(totalDep),
      disposal_gain_loss: Math.round(disposalGainLoss),
      ebitda, ebit,
      om_bonus: omBonus, net_profit: netProfit,
      payment: { kpay: salesKpay + topupKpay, cash: salesCash + topupCash },
    });
  } catch(e) { res.status(500).json({ error: String(e.message) }); }
});

// GET /api/finance/balance-sheet — assets, liabilities, equity snapshot
app.get("/api/finance/balance-sheet", async (_req, res) => {
  try {
    const now = new Date(Date.now() + 6.5*60*60*1000);
    const yr = now.getUTCFullYear(), mo = now.getUTCMonth() + 1;

    const [assetRows, accRows, payRows, recRows, shRows, salesRows, topupRows, opexRows, txRows, cwRows, tuRows2, bsPrepaidRows, bsAdvpayRows] = await sheetsGetBatch([
      "Assets_Register!A:M", "Accounts!A:D", "Payables!A:I", "Receivables!A:I",
      "Capital_Setup!A:D", "Sales_Daily!A:K", "TopUp_Log!A:I", "OPEX_Log!A:H", "Account_Transfers!A:F",
      "Card_Wallet!B:L", "TopUp_Log!E:H", "Prepaid_Expenses!A:F", "Advance_Payments!A:H"
    ]);

    // Fixed assets
    const fixedAssets = assetRows.slice(1).filter(r=>(r[0]??'').trim()).map(r => {
      const unitCost=parseNum(r[3]??'0'), qty=Math.max(1,parseNum(r[4]??'1')||1), usefulYrs=parseNum(r[5]??'0'), salvagePU=parseNum(r[6]??'0');
      const status=(r[7]??'Active').trim(), disposedQty=parseNum(r[9]??'0');
      const activeQty=Math.max(0,qty-disposedQty);
      const activeCost=unitCost*activeQty, activeSalvage=salvagePU*activeQty;
      const accDep=(status==='Active'||status==='Partially Disposed')?accumulatedDep(activeCost,activeSalvage,usefulYrs,(r[2]??'').trim(),yr,mo):0;
      return { name:(r[0]??'').trim(), cost:unitCost*qty, acc_dep:accDep, book_value: status==='Disposed'?0:Math.max(activeSalvage,activeCost-accDep) };
    });
    const totalFixedAssets = fixedAssets.reduce((s,a)=>s+a.book_value,0);

    // Current assets (account balances)
    const bal = {};
    const accounts = accRows.slice(1).filter(r=>(r[0]??'').trim());
    for (const a of accounts) bal[(a[0]??'').trim()] = parseNum(a[2]??'0');
    for (const r of salesRows.slice(1)) {
      const kpay=parseNum(r[9]??'0'), cash=parseNum(r[10]??'0');
      if(bal['MMQR']!==undefined)bal['MMQR']+=kpay; else if(bal['KPay']!==undefined)bal['KPay']+=kpay;
      if(bal['Cash Box']!==undefined)bal['Cash Box']+=cash;
    }
    for (const r of topupRows.slice(1)) {
      const kpay=parseNum(r[5]??'0'), cash=parseNum(r[6]??'0');
      if(bal['MMQR']!==undefined)bal['MMQR']+=kpay; else if(bal['KPay']!==undefined)bal['KPay']+=kpay;
      if(bal['Cash Box']!==undefined)bal['Cash Box']+=cash;
    }
    for (const r of opexRows.slice(1)) { const acct=(r[4]??'').trim(); if(bal[acct]!==undefined) bal[acct]-=parseNum(r[3]??'0'); }
    for (const r of txRows.slice(1)) {
      const from=(r[1]??'').trim(),to=(r[2]??'').trim(),amt=parseNum(r[3]??'0');
      if(bal[from]!==undefined)bal[from]-=amt; if(bal[to]!==undefined)bal[to]+=amt;
    }
    // Prepaid_Expenses payments → deduct from account (col F = account paid from)
    for (const r of (bsPrepaidRows??[]).slice(1)) {
      if(!(r[0]??'').trim()) continue;
      const amt=parseNum(r[2]??'0'), acct=(r[5]??'').trim();
      if(acct && amt && bal[acct]!==undefined) bal[acct]-=amt;
    }
    // Advance_Payments — cash paid out at entry (col E=account, col D=amount)
    for (const r of (bsAdvpayRows??[]).slice(1)) {
      if(!(r[0]??'').trim()) continue;
      const amt=parseNum(r[3]??'0'), acct=(r[4]??'').trim();
      if(acct && amt && bal[acct]!==undefined) bal[acct]-=amt;
    }
    // Assets_Register — purchase cost paid from account (col D=unitCost, E=qty, M=account)
    for (const r of assetRows.slice(1)) {
      if(!(r[0]??'').trim()) continue;
      const status=(r[7]??'Active').trim();
      if(status==='Disposed') continue;
      const unitCost=parseNum(r[3]??'0'), qty=Math.max(1,parseNum(r[4]??'1')||1);
      const acct=(r[12]??'').trim();  // col M = account
      if(!acct || !unitCost) continue;
      if(bal[acct]!==undefined) bal[acct]-=unitCost*qty;
    }
    // Receivables — cash paid out (col H=account, col D=amount); restore if Received
    for (const r of (recRows??[]).slice(1)) {
      if(!(r[0]??'').trim()) continue;
      const amt=parseNum(r[3]??'0'), status=(r[5]??'').trim().toLowerCase(), acct=(r[7]??'').trim();
      if(!acct || !amt) continue;
      if(bal[acct]!==undefined) {
        bal[acct]-=amt;
        if(status==='received') bal[acct]+=amt;
      }
    }
    // Payables (Paid) — cash actually paid out → deduct from account (col H=account)
    for (const r of (payRows??[]).slice(1)) {
      if(!(r[0]??'').trim()) continue;
      const amt=parseNum(r[3]??'0'), status=(r[5]??'').trim().toLowerCase(), acct=(r[7]??'').trim();
      if(!acct || !amt) continue;
      if(status==='paid' && bal[acct]!==undefined) bal[acct]-=amt;
    }
    const currentAssets = accounts.map(a=>({ name:(a[0]??'').trim(), balance:Math.round(bal[(a[0]??'').trim()]??0) }));
    const totalCurrentAssets = currentAssets.reduce((s,a)=>s+a.balance,0);

    // Receivables (Pending only) — shown as separate asset line (cash already deducted above)
    const totalReceivables = recRows.slice(1).filter(r=>(r[5]??'').trim()==='Pending').reduce((s,r)=>s+parseNum(r[3]??'0'),0);
    // Pending advance payments = current asset (cash went out but goods/service not yet received)
    const totalAdvancesPending = (bsAdvpayRows??[]).slice(1)
      .filter(r=>(r[0]??'').trim() && (r[6]??'').trim().toLowerCase()==='pending')
      .reduce((s,r)=>s+parseNum(r[3]??'0'),0);

    // Liabilities — payables
    const totalPayables = payRows.slice(1).filter(r=>(r[5]??'').trim()==='Pending').reduce((s,r)=>s+parseNum(r[3]??'0'),0);

    // Member wallet liability — same logic as /api/sheets/liability
    // cwRows = Card_Wallet!B:L  → r[0]=MemberID, r[6]=balance_mins(col H), r[10]=member_rate(col L)
    // tuRows2 = TopUp_Log!E:H   → r[0]=amount, r[3]=mins (for alltime rate fallback)
    let _tp=0,_tm=0;
    for(const r of tuRows2.slice(1)){const a=parseNum(r[0]??'0'),m=parseNum(r[3]??'0');if(a>0&&m>0){_tp+=a;_tm+=m;}}
    const alltimeRate=_tm>0?Math.round((_tp/_tm)*100)/100:150;
    const rateDict2={};
    for(const r of cwRows.slice(1)){const mId=(r[0]??'').trim(),rv=parseFloat((r[10]??'').trim());if(mId&&rv>0)rateDict2[mId]=rv;}
    let memberLiability=0;
    for(const r of cwRows.slice(1)){
      const mId=(r[0]??'').trim(), mins=parseNum(r[6]??'0');
      if(!mId||mins<=0)continue;
      const rate=rateDict2[mId]??alltimeRate;
      memberLiability+=Math.floor(mins*rate);
    }
    memberLiability=Math.round(memberLiability);

    // Equity
    const totalCapital = shRows.slice(1).filter(r=>(r[0]??'').trim()).reduce((s,r)=>s+parseNum(r[2]??'0'),0);
    const totalAssets = totalFixedAssets + totalCurrentAssets + totalReceivables + totalAdvancesPending;
    const totalLiabilities = totalPayables + memberLiability;
    const retainedEarnings = totalAssets - totalLiabilities - totalCapital;

    res.json({
      as_of: `${yr}-${String(mo).padStart(2,'0')}`,
      assets: { fixed: fixedAssets, fixed_total: Math.round(totalFixedAssets), current: currentAssets, current_total: Math.round(totalCurrentAssets), receivables: Math.round(totalReceivables), advances_pending: Math.round(totalAdvancesPending), total: Math.round(totalAssets) },
      liabilities: { payables: Math.round(totalPayables), member_liability: memberLiability, total: Math.round(totalLiabilities) },
      equity: { paid_in_capital: Math.round(totalCapital), retained_earnings: Math.round(retainedEarnings), total: Math.round(totalCapital + retainedEarnings) },
    });
  } catch(e) { res.status(500).json({ error: String(e.message) }); }
});

// GET /api/finance/profit-sharing?month=YYYY-MM
app.get("/api/finance/profit-sharing", async (req, res) => {
  try {
    const now = new Date(Date.now() + 6.5*60*60*1000);
    const monthStr = String(req.query.month ?? `${now.getUTCFullYear()}-${String(now.getUTCMonth()+1).padStart(2,'0')}`);
    const [yr, mo] = monthStr.split('-').map(Number);

    // Reuse PNL data
    const pnlRes = await (async () => {
      const [salesRows, topupRows, soRows, opexRows, assetRows, prepaidRows] = await sheetsGetBatch([
        "Sales_Daily!A:O","TopUp_Log!A:I","Stock_Out!A:H","OPEX_Log!A:H","Assets_Register!A:L","Prepaid_Expenses!A:E"
      ]);
      let gameRev=0,foodRev=0,discounts=0; let cogs=0; let totalOpex=0; let totalDep=0;
      for(const r of salesRows.slice(1)){if(!inMonth(r[0]??'',yr,mo))continue;gameRev+=parseNum(r[5]??'0');foodRev+=parseNum(r[6]??'0');discounts+=parseNum(r[7]??'0');}
      for(const r of soRows.slice(1)){if(inMonth(r[0]??'',yr,mo))cogs+=parseNum(r[7]??'0');}
      for(const r of opexRows.slice(1)){if(inMonthDate(r[0]??'',yr,mo))totalOpex+=parseNum(r[3]??'0');}
      // Prepaid amortization
      const prepaidAmort=calcPrepaidAmort(prepaidRows,yr,mo);
      for(const amt of Object.values(prepaidAmort))totalOpex+=amt;
      for(const r of assetRows.slice(1)){
        const st=(r[7]??'Active').trim(); if(st==='Disposed')continue;
        const uc=parseNum(r[3]??'0'),aq=Math.max(0,Math.max(1,parseNum(r[4]??'1')||1)-parseNum(r[9]??'0')),uyr=parseNum(r[5]??'0'),spu=parseNum(r[6]??'0');
        const cost=uc*aq,salvage=spu*aq,purchDate=(r[2]??'').trim();
        const dep=accumulatedDep(cost,salvage,uyr,purchDate,yr,mo)-accumulatedDep(cost,salvage,uyr,purchDate,mo>1?yr:yr-1,mo>1?mo-1:12);
        totalDep+=Math.max(0,dep);
      }
      const totalRevenue=gameRev+foodRev-discounts;
      const grossProfit=totalRevenue-cogs;
      const ebitda=grossProfit-totalOpex;
      const ebit=ebitda-Math.round(totalDep);
      return { ebit };
    })();

    const shRows = await sheetGet("Capital_Setup!A:D");
    const shareholders = shRows.slice(1).filter(r=>(r[0]??'').trim()).map(r=>({
      name:(r[0]??'').trim(), role:(r[1]??'').trim()||'Silent Partner',
      capital:parseNum(r[2]??'0'), ownership:parseNum(r[3]??'0'),
    }));
    const opPartner = shareholders.find(s=>s.role.toLowerCase().includes('operation'));

    const ebit = pnlRes.ebit;
    const omBonus = ebit > 0 && opPartner ? Math.round(ebit * 0.10) : 0;
    const distributable = Math.max(0, ebit - omBonus);

    const sharing = shareholders.map(s => {
      const isOM = s.name === opPartner?.name;
      const shareholderDividend = Math.round(distributable * s.ownership / 100);
      const omBonusPart = isOM ? omBonus : 0;
      return { ...s, om_bonus: omBonusPart, dividend: shareholderDividend, total_income: shareholderDividend + omBonusPart };
    });

    res.json({
      month: monthStr, ebit, om_bonus: omBonus, distributable_profit: distributable,
      shareholders: sharing, op_partner: opPartner?.name ?? null,
    });
  } catch(e) { res.status(500).json({ error: String(e.message) }); }
});

// GET /api/finance/payables
app.get("/api/finance/payables", async (_req, res) => {
  try {
    const rows = await sheetGet("Payables!A:I");
    const items = rows.slice(1).filter(r=>(r[0]??'').trim()).map(r=>({
      date:(r[0]??'').trim(), vendor:(r[1]??'').trim(), description:(r[2]??'').trim(),
      amount:parseNum(r[3]??'0'), due_date:(r[4]??'').trim(), status:(r[5]??'Pending').trim(),
      paid_date:(r[6]??'').trim(), account:(r[7]??'').trim(), notes:(r[8]??'').trim(),
    }));
    const pending = items.filter(i=>i.status==='Pending');
    const totalPending = pending.reduce((s,i)=>s+i.amount,0);
    res.json({ payables: items, pending_count: pending.length, total_pending: totalPending });
  } catch(e) { res.status(500).json({ error: String(e.message) }); }
});

// POST /api/finance/payables
app.post("/api/finance/payables", async (req, res) => {
  try {
    const { vendor, description, amount, due_date, account, notes } = req.body || {};
    if (!vendor || !amount) return res.status(400).json({ error: 'vendor and amount required' });
    const now = new Date(Date.now() + 6.5*60*60*1000);
    const date = `${now.getUTCMonth()+1}/${now.getUTCDate()}/${now.getUTCFullYear()}`;
    await sheetAppend("Payables!A:I", [[date, vendor, description||'', amount, due_date||'', 'Pending', '', account||'', notes||'']]);
    res.json({ ok: true });
  } catch(e) { res.status(500).json({ error: String(e.message) }); }
});

// GET /api/finance/receivables
app.get("/api/finance/receivables", async (_req, res) => {
  try {
    const rows = await sheetGet("Receivables!A:I");
    const items = rows.slice(1).filter(r=>(r[0]??'').trim()).map(r=>({
      date:(r[0]??'').trim(), customer:(r[1]??'').trim(), description:(r[2]??'').trim(),
      amount:parseNum(r[3]??'0'), due_date:(r[4]??'').trim(), status:(r[5]??'Pending').trim(),
      received_date:(r[6]??'').trim(), account:(r[7]??'').trim(), notes:(r[8]??'').trim(),
    }));
    const pending = items.filter(i=>i.status==='Pending');
    const totalPending = pending.reduce((s,i)=>s+i.amount,0);
    res.json({ receivables: items, pending_count: pending.length, total_pending: totalPending });
  } catch(e) { res.status(500).json({ error: String(e.message) }); }
});

// GET /api/finance/cashflow?month=YYYY-MM
app.get("/api/finance/cashflow", async (req, res) => {
  try {
    const now = new Date(Date.now() + 6.5*60*60*1000);
    const monthStr = String(req.query.month ?? `${now.getUTCFullYear()}-${String(now.getUTCMonth()+1).padStart(2,'0')}`);
    const [yr, mo] = monthStr.split('-').map(Number);

    const [salesRows, topupRows, opexRows, txRows, assetRows] = await sheetsGetBatch([
      "Sales_Daily!A:K","TopUp_Log!A:I","OPEX_Log!A:H","Account_Transfers!A:F","Assets_Register!A:L"
    ]);

    // Operating inflows
    let salesInflow=0, topupInflow=0;
    for(const r of salesRows.slice(1)){if(inMonth(r[0]??'',yr,mo))salesInflow+=parseNum(r[8]??'0');}
    for(const r of topupRows.slice(1)){if(inMonth(r[0]??'',yr,mo))topupInflow+=parseNum(r[4]??'0');}

    // Operating outflows (OPEX)
    let opexOutflow=0;
    for(const r of opexRows.slice(1)){if(inMonthDate(r[0]??'',yr,mo))opexOutflow+=parseNum(r[3]??'0');}

    // Investing outflows (asset purchases this month)
    let investOutflow=0;
    for(const r of assetRows.slice(1)){if(inMonthDate(r[2]??'',yr,mo)){const uc=parseNum(r[3]??'0'),q=Math.max(1,parseNum(r[4]??'1')||1);investOutflow+=uc*q;}}

    const operatingCF = salesInflow + topupInflow - opexOutflow;
    const investingCF = -investOutflow;
    const netCF = operatingCF + investingCF;

    res.json({
      month: monthStr,
      operating: { sales_inflow: salesInflow, topup_inflow: topupInflow, opex_outflow: opexOutflow, net: operatingCF },
      investing: { asset_purchases: investOutflow, net: investingCF },
      net_cashflow: netCF,
    });
  } catch(e) { res.status(500).json({ error: String(e.message) }); }
});

// GET /api/finance/annual?year=YYYY — yearly P&L summary
app.get("/api/finance/annual", async (req, res) => {
  try {
    const yr = parseInt(req.query.year ?? new Date().getFullYear());
    const [salesRows, soRows, opexRows, assetRows, prepaidRows] = await sheetsGetBatch([
      "Sales_Daily!A:O","Stock_Out!A:H","OPEX_Log!A:H","Assets_Register!A:L","Prepaid_Expenses!A:E"
    ]);
    const months = Array.from({length:12},(_,i)=>i+1);
    const monthly = months.map(mo => {
      let gameRev=0,foodRev=0,discounts=0,cogs=0,opex=0,dep=0;
      for(const r of salesRows.slice(1)){if(!inMonth(r[0]??'',yr,mo))continue;gameRev+=parseNum(r[5]??'0');foodRev+=parseNum(r[6]??'0');discounts+=parseNum(r[7]??'0');}
      for(const r of soRows.slice(1)){if(inMonth(r[0]??'',yr,mo))cogs+=parseNum(r[7]??'0');}
      for(const r of opexRows.slice(1)){if(inMonthDate(r[0]??'',yr,mo))opex+=parseNum(r[3]??'0');}
      // Prepaid amortization per month
      const pa=calcPrepaidAmort(prepaidRows,yr,mo);
      for(const amt of Object.values(pa))opex+=amt;
      for(const r of assetRows.slice(1)){
        const st=(r[7]??'Active').trim(); if(st==='Disposed')continue;
        const uc=parseNum(r[3]??'0'),aq=Math.max(0,Math.max(1,parseNum(r[4]??'1')||1)-parseNum(r[9]??'0')),uyr=parseNum(r[5]??'0'),spu=parseNum(r[6]??'0');
        const cost=uc*aq,salvage=spu*aq,purchDate=(r[2]??'').trim();
        const d=accumulatedDep(cost,salvage,uyr,purchDate,yr,mo)-accumulatedDep(cost,salvage,uyr,purchDate,mo>1?yr:yr-1,mo>1?mo-1:12);
        dep+=Math.max(0,d);
      }
      const revenue=gameRev+foodRev-discounts, gp=revenue-cogs, ebitda=gp-opex, ebit=ebitda-Math.round(dep);
      const omBonus=ebit>0?Math.round(ebit*0.10):0;
      return { month:mo, revenue, cogs, gross_profit:gp, opex, depreciation:Math.round(dep), ebitda, ebit, om_bonus:omBonus, net_profit:ebit-omBonus };
    });
    const total = monthly.reduce((acc,m) => {
      for(const k of Object.keys(m)) if(k!=='month') acc[k]=(acc[k]??0)+m[k];
      return acc;
    }, {});
    res.json({ year: yr, monthly, annual_total: total });
  } catch(e) { res.status(500).json({ error: String(e.message) }); }
});

// GET /api/finance/prepaid — prepaid expenses summary
app.get("/api/finance/prepaid", async (_req, res) => {
  try {
    const rows = await sheetGet("Prepaid_Expenses!A:J");
    const items = rows.slice(1).filter(r => (r[0]??'').trim()).map(r => {
      const totalPaid   = parseNum(r[2]??'0');
      const startStr    = (r[3]??'').trim();
      const endStr      = (r[4]??'').trim();
      const start       = new Date(startStr), end = new Date(endStr);
      const totalMonths = (!isNaN(start.getTime())&&!isNaN(end.getTime()))
        ? Math.max(1,(end.getFullYear()-start.getFullYear())*12+(end.getMonth()-start.getMonth())) : 0;
      const monthlyAmt  = totalMonths > 0 ? Math.round(totalPaid/totalMonths) : 0;
      // Months used from business start
      const effStart = start < BUSINESS_START ? BUSINESS_START : start;
      const now = new Date(Date.now()+6.5*60*60*1000);
      const monthsUsed = totalMonths > 0
        ? Math.min(totalMonths, Math.max(0, (now.getUTCFullYear()-effStart.getFullYear())*12+(now.getUTCMonth()-effStart.getMonth())+1)) : 0;
      const recognized = monthlyAmt * monthsUsed;
      const remaining  = Math.max(0, totalPaid - recognized);
      return {
        description: (r[0]??'').trim(),
        category:    (r[1]??'Rent').trim()||'Rent',
        total_paid:  totalPaid,
        start_date:  startStr,
        end_date:    endStr,
        total_months: totalMonths,
        monthly_amt: monthlyAmt,
        months_used: monthsUsed,
        recognized,
        remaining,
      };
    });
    res.json({ prepaid: items, total_remaining: items.reduce((s,i)=>s+i.remaining,0) });
  } catch(e) { res.status(500).json({ error: String(e.message) }); }
});

// GET /api/finance/advance-payments — vendor advances paid in advance (current asset until settled)
// Sheet: Advance_Payments!A:H  A=Date,B=Party,C=Desc,D=Amount,E=Account,F=ExpectedDate,G=Status,H=Notes
app.get("/api/finance/advance-payments", async (_req, res) => {
  try {
    const rows = await sheetsGet("Advance_Payments!A:H");
    const items = rows.slice(1).filter(r=>(r[0]??'').trim()).map(r=>({
      date:     (r[0]??'').trim(),
      party:    (r[1]??'').trim(),
      desc:     (r[2]??'').trim(),
      amount:   parseNum(r[3]??'0'),
      account:  (r[4]??'').trim(),
      due_date: (r[5]??'').trim(),
      status:   (r[6]??'Pending').trim(),
      notes:    (r[7]??'').trim(),
    }));
    const pending  = items.filter(i=>i.status.toLowerCase()==='pending');
    const settled  = items.filter(i=>i.status.toLowerCase()==='settled');
    res.json({
      advances: items,
      total_pending:  pending.reduce((s,i)=>s+i.amount,0),
      total_settled:  settled.reduce((s,i)=>s+i.amount,0),
    });
  } catch(e) { res.status(500).json({ error: String(e.message) }); }
});

// POST /api/finance/setup-sheets — create all Finance module sheets with headers + formulas
app.post("/api/finance/setup-sheets", async (_req, res) => {
  try {
    const sheets = getSheetsRw();

    // 1. Check existing sheets
    const meta = await sheets.spreadsheets.get({ spreadsheetId: SHEET_ID, fields: 'sheets.properties' });
    const existing = new Set((meta.data.sheets ?? []).map(s => s.properties.title));

    const sheetDefs = [
      'Capital_Setup','Assets_Register','OPEX_Log','Accounts',
      'Account_Transfers','Payables','Receivables','Advance_Staff','Prepaid_Expenses','Advance_Payments'
    ];
    const toCreate = sheetDefs.filter(n => !existing.has(n));

    // 2. Create missing sheets
    if (toCreate.length > 0) {
      await sheets.spreadsheets.batchUpdate({
        spreadsheetId: SHEET_ID,
        requestBody: { requests: toCreate.map(title => ({ addSheet: { properties: { title, gridProperties: { rowCount: 500, columnCount: 16 } } } })) }
      });
    }

    // 3. Populate headers & initial data
    const batchData = [];

    // Capital_Setup
    if (!existing.has('Capital_Setup')) {
      batchData.push({ range:'Capital_Setup!A1:D4', values:[
        ['Shareholder','Role','Capital (Ks)','Ownership %'],
        ['Aung Chan Myint','Operation Partner',102000000,34],
        ['Ye Myat','Silent Partner',99000000,33],
        ['Wai Yan Htet','Silent Partner',99000000,33],
      ]});
    }

    // Assets_Register — A:M (all data, no formula columns)
    if (!existing.has('Assets_Register')) {
      batchData.push({ range:'Assets_Register!A1:M1', values:[
        ['Name','Category','Purchase Date (M/D/YYYY)','Unit Cost (Ks)','Qty','Useful Life (Yrs)','Salvage/Unit (Ks)','Status','Disposal Date','Disposed Qty','Disposal Proceeds (Ks)','Notes','Payment Type']
      ]});
    }

    // OPEX_Log
    if (!existing.has('OPEX_Log')) {
      batchData.push({ range:'OPEX_Log!A1:H1', values:[
        ['Date (M/D/YYYY)','Category','Description','Amount (Ks)','Account','Payment Type (Cash/KBZ/AYA/MMQR)','Reference','Notes']
      ]});
    }

    // Accounts
    if (!existing.has('Accounts')) {
      batchData.push({ range:'Accounts!A1:D5', values:[
        ['Account Name','Type','Opening Balance (Ks)','Notes'],
        ['Cash Box','Cash',0,'ဆိုင်တွင်း ငွေသား'],
        ['KBZ Bank','Bank',0,''],
        ['MMQR','Digital',0,'KPay / MMQR'],
        ['AYA Bank','Bank',0,''],
      ]});
    }

    // Account_Transfers
    if (!existing.has('Account_Transfers')) {
      batchData.push({ range:'Account_Transfers!A1:F1', values:[
        ['Date','From Account','To Account','Amount (Ks)','Notes','Reference']
      ]});
    }

    // Payables
    if (!existing.has('Payables')) {
      batchData.push({ range:'Payables!A1:I1', values:[
        ['Date','Vendor','Description','Amount (Ks)','Due Date','Status (Pending/Paid)','Paid Date','Account','Notes']
      ]});
    }

    // Receivables
    if (!existing.has('Receivables')) {
      batchData.push({ range:'Receivables!A1:I1', values:[
        ['Date','Customer','Description','Amount (Ks)','Due Date','Status (Pending/Received)','Received Date','Account','Notes']
      ]});
    }

    // Advance_Staff
    if (!existing.has('Advance_Staff')) {
      batchData.push({ range:'Advance_Staff!A1:F1', values:[
        ['Date','Staff Name','Amount (Ks)','Payment Type','Notes','Deducted (Y/N)']
      ]});
    }

    // Prepaid_Expenses — columns A-E user fills; F-J auto-formula
    // Advance_Payments — A=Date, B=Party, C=Description, D=Amount, E=Account, F=Expected Date, G=Status, H=Notes
    if (!existing.has('Advance_Payments')) {
      batchData.push({ range:'Advance_Payments!A1:H1', values:[
        ['Date','Party','Description','Amount (Ks)','Account','Expected Date','Status (Pending/Settled)','Notes']
      ]});
    }

    if (!existing.has('Prepaid_Expenses')) {
      batchData.push({ range:'Prepaid_Expenses!A1:J1', values:[
        ['Description','Category','Total Paid (Ks)','Start Date (YYYY-MM-DD)','End Date (YYYY-MM-DD)','Total Months','Monthly Amt (Ks)','Months Used','Total Recognized (Ks)','Remaining Balance (Ks)']
      ]});
      const prepaidFormulas = Array.from({length:49},(_,idx)=>{
        const i = idx+2;
        return [
          '','Rent','','','',
          `=IFERROR(DATEDIF(DATEVALUE(D${i}),DATEVALUE(E${i}),"M"),0)`,
          `=IF(OR(C${i}="",F${i}=0),"",ROUND(C${i}/F${i},0))`,
          `=IF(D${i}="","",MIN(F${i},MAX(0,DATEDIF(MAX(DATEVALUE(D${i}),DATE(2026,6,1)),TODAY(),"M")+1)))`,
          `=IF(G${i}="","",G${i}*H${i})`,
          `=IF(C${i}="","",MAX(0,C${i}-I${i}))`,
        ];
      });
      batchData.push({ range:'Prepaid_Expenses!A2:J50', values: prepaidFormulas });
    }

    if (batchData.length > 0) {
      await sheets.spreadsheets.values.batchUpdate({
        spreadsheetId: SHEET_ID,
        requestBody: { valueInputOption:'USER_ENTERED', data: batchData }
      });
    }

    res.json({ ok: true, created: toCreate, skipped: sheetDefs.filter(n=>existing.has(n)), total_sheets: sheetDefs.length });
  } catch(e) { res.status(500).json({ error: String(e.message) }); }
});

// POST /api/sheets/log  — append one AI interaction row to 'Logs' sheet
app.post("/api/sheets/log", async (req, res) => {
  try {
    const { user_name = "", query = "", response = "", sentiment = "neutral" } = req.body ?? {};
    const now = new Date();
    const mmt = new Date(now.getTime() + 6.5 * 60 * 60 * 1000);
    const dateStr = `${mmt.getMonth()+1}/${mmt.getDate()}/${mmt.getFullYear()}`;
    const timeStr = mmt.toTimeString().slice(0,8);
    await sheetAppend("Logs!A:F", [[dateStr, timeStr, user_name, query.slice(0,300), response.slice(0,500), sentiment]]);
    res.json({ ok: true });
  } catch(e) { res.status(500).json({ error: String(e.message) }); }
});

// ══════════════════════════════════════════════════════════════════════════════
//  WAITLIST ROUTES
// ══════════════════════════════════════════════════════════════════════════════

// GET /api/waitlist — all active entries (staff view)
app.get("/api/waitlist", (req, res) => {
  try {
    const store = _wlRead();
    const { status } = req.query;
    let rows = status ? store.rows.filter(r => r.status === status) : store.rows;
    rows = rows.sort((a, b) => a.joined_at.localeCompare(b.joined_at));
    res.json({ count: rows.length, rows });
  } catch(e) { res.status(500).json({ error: String(e) }); }
});

// GET /api/waitlist/my/:chatId — check if user is on waitlist
// MUST be declared before /:id to avoid Express matching "my" as an id param
app.get("/api/waitlist/my/:chatId", (req, res) => {
  try {
    const chatId = String(req.params.chatId);
    const store = _wlRead();
    const entry = store.rows
      .filter(r => String(r.telegram_chat_id) === chatId && r.status === "waiting")
      .sort((a, b) => a.joined_at.localeCompare(b.joined_at))[0] || null;

    if (!entry) return res.json({ on_waitlist: false });

    // Calculate queue position (1-indexed among waiting entries with same/any pref)
    const waiting = store.rows
      .filter(r => r.status === "waiting" &&
        (r.console_pref === entry.console_pref || r.console_pref === "Any" || entry.console_pref === "Any"))
      .sort((a, b) => a.joined_at.localeCompare(b.joined_at));
    const position = waiting.findIndex(r => r.id === entry.id) + 1;

    res.json({ on_waitlist: true, entry, position });
  } catch(e) { res.status(500).json({ error: String(e) }); }
});

// GET /api/waitlist/:id — single entry
app.get("/api/waitlist/:id", (req, res) => {
  try {
    const id = parseInt(req.params.id);
    if (isNaN(id)) return res.status(400).json({ error: "Invalid id" });
    const row = _wlRead().rows.find(r => r.id === id);
    if (!row) return res.status(404).json({ error: "Not found" });
    res.json(row);
  } catch(e) { res.status(500).json({ error: String(e) }); }
});

// POST /api/waitlist — join waitlist
app.post("/api/waitlist", (req, res) => {
  const { telegram_chat_id, customer_name, phone, console_pref } = req.body || {};
  if (!telegram_chat_id || !customer_name)
    return res.status(400).json({ error: "telegram_chat_id and customer_name required" });

  _withWlLock(() => {
    const store = _wlRead();
    // Prevent duplicate active entries for same chat_id
    const existing = store.rows.find(
      r => String(r.telegram_chat_id) === String(telegram_chat_id) && r.status === "waiting"
    );
    if (existing) {
      res.status(409).json({ error: "already_waiting", entry: existing });
      return;
    }
    const now = new Date().toISOString();
    const entry = {
      id: store.next_id++,
      telegram_chat_id: String(telegram_chat_id),
      customer_name,
      phone: phone || "",
      console_pref: console_pref || "Any",
      joined_at: now,
      status: "waiting",
      notified_at: null,
    };
    store.rows.push(entry);
    _wlWrite(store);
    res.status(201).json(entry);
  }).catch(e => res.status(500).json({ error: String(e) }));
});

// DELETE /api/waitlist/:id — cancel/leave waitlist
app.delete("/api/waitlist/:id", (req, res) => {
  const id = parseInt(req.params.id);
  if (isNaN(id)) return res.status(400).json({ error: "Invalid id" });
  _withWlLock(() => {
    const store = _wlRead();
    const row = store.rows.find(r => r.id === id);
    if (!row) { res.status(404).json({ error: "Not found" }); return; }
    row.status = "cancelled";
    _wlWrite(store);
    res.json({ ok: true });
  }).catch(e => res.status(500).json({ error: String(e) }));
});

// POST /api/waitlist/notify — FIFO trigger called by staff bot after session end
// body: { console_id: "C - 01" }  →  derives type from cached consoles data
app.post("/api/waitlist/notify", async (req, res) => {
  try {
    const { console_id } = req.body || {};

    // Derive console_type from consoles data (cached)
    let console_type = null;
    try {
      const cfg = await fetchConfigData();
      const consolesRaw = cfg.console_multipliers || {};
      // fetchConfigData returns console_multipliers as {id: mult} — need type
      // Use consoles endpoint data which has .type field
      const consolesData = await (async () => {
        const key = "consoles";
        if (_cache.has(key) && Date.now() - (_cacheTs.get(key)||0) < 5*60*1000)
          return _cache.get(key);
        return null; // fallback: skip type filter
      })();
      if (consolesData && Array.isArray(consolesData.consoles)) {
        const match = consolesData.consoles.find(c => c.id === console_id);
        console_type = match ? match.type : null;
      }
    } catch(e) { console.error("[waitlist/notify] console lookup error:", e.message); }

    // Null guard: if console_id provided but type can't be resolved, still notify (Any fallback)
    if (console_id && !console_type) {
      console.warn(`[waitlist/notify] console_id "${console_id}" not found in cache — notifying Any`);
    }

    const notified = await _wlNotifyNext(console_type);
    if (!notified) return res.json({ notified: false, reason: "empty_queue" });
    res.json({ notified: true, entry: notified });
  } catch(e) { res.status(500).json({ error: String(e) }); }
});

// ══════════════════════════════════════════════════════════════════════════════
// REAL-TIME CACHE INVALIDATION — Customer Bot integration
// ══════════════════════════════════════════════════════════════════════════════

// In-memory queue for pending cache invalidations (customer bot polls this)
let _cacheInvalidationQueue = [];

// POST /api/cache-invalidate?keys=games,config,promotions
// Triggered by n8n or admin when Google Sheets data changes
app.post("/api/cache-invalidate", (req, res) => {
  try {
    const keysParam = req.query.keys || "";
    const keys = keysParam.split(",").map(k => k.trim()).filter(k => k);
    
    if (!keys.length) {
      return res.status(400).json({ error: "No keys specified" });
    }
    
    // Add to queue (customer bot will poll and clear this)
    _cacheInvalidationQueue.push({
      keys,
      timestamp: new Date().toISOString(),
    });
    
    console.log(`[cache-invalidate] Queued: ${keys.join(", ")}`);
    res.json({ success: true, keys, queued: _cacheInvalidationQueue.length });
  } catch(e) {
    res.status(500).json({ error: String(e) });
  }
});

// GET /api/cache-invalidations
// Customer bot polls this endpoint (every 5 sec) to get pending invalidations
app.get("/api/cache-invalidations", (req, res) => {
  try {
    if (_cacheInvalidationQueue.length === 0) {
      return res.json({ keys: [] });
    }
    
    // Return first item in queue
    const item = _cacheInvalidationQueue[0];
    res.json({ keys: item.keys, timestamp: item.timestamp });
  } catch(e) {
    res.status(500).json({ error: String(e) });
  }
});

// POST /api/cache-invalidations/ack
// Customer bot acknowledges after processing invalidation
app.post("/api/cache-invalidations/ack", (req, res) => {
  try {
    if (_cacheInvalidationQueue.length > 0) {
      const removed = _cacheInvalidationQueue.shift();
      console.log(`[cache-invalidations/ack] Processed: ${removed.keys.join(", ")}`);
    }
    res.json({ success: true, remaining: _cacheInvalidationQueue.length });
  } catch(e) {
    res.status(500).json({ error: String(e) });
  }
});

// GET /api/healthz
app.get("/api/healthz", (_req, res) => res.json({ ok: true, uptime: process.uptime() }));

// ── Startup ───────────────────────────────────────────────────────────────────
process.on("uncaughtException",  e => console.error("UncaughtException:", e.message));
process.on("unhandledRejection", r => console.error("UnhandledRejection:", r));

// ── Auto-trigger cache invalidation on Google Sheets changes ─────────────────
// This can be called by n8n workflows when data changes
// For now, we rely on manual POST /api/cache-invalidate calls from n8n

// ── Waitlist auto-expire timer (every 5 min) ─────────────────────────────────
// Find "notified" entries older than 15 min → mark expired → cascade FIFO notify.
// Groups by console_pref to avoid notifying multiple people for the same console.
setInterval(async () => {
  const EXPIRE_MS = 15 * 60 * 1000;
  const now = Date.now();
  try {
    const store = _wlRead();
    const expiredEntries = store.rows.filter(r =>
      r.status === "notified" &&
      r.notified_at &&
      (now - new Date(r.notified_at).getTime()) > EXPIRE_MS
    );
    if (expiredEntries.length === 0) return;

    // Step 1: mark ALL expired in one atomic write
    for (const r of expiredEntries) r.status = "expired";
    _wlWrite(store);
    console.log(`[waitlist] Expired ${expiredEntries.length} entries: ids=${expiredEntries.map(r=>r.id).join(",")}`);

    // Step 2: group by console_pref → call _wlNotifyNext ONCE per unique pref
    // This prevents double-notifying when multiple entries expire in the same tick
    const uniquePrefs = [...new Set(expiredEntries.map(r => r.console_pref))];
    for (const pref of uniquePrefs) {
      await _wlNotifyNext(pref);
    }
  } catch(e) { console.error("[waitlist] expire timer error:", e.message); }
}, 5 * 60 * 1000);

app.listen(PORT, "0.0.0.0", () => {
  console.log(`[PS Vibe API v2] port=${PORT} store=${BK_JSON_PATH} sheet=${SHEET_ID?'OK':'MISSING'}`);
});
