"""Live web dashboard for the Telegram trading bot.

Read-only stats (positions, history, sniper activity, logs) come straight
from the local state file so they're always fast and available even if the
bot's RPC/price providers are briefly down. Wallet balance and live position
prices are fetched from the bot's own event loop (see run_dashboard_coro in
trading_bot.py) and cached for a few seconds so the 2s UI poll never hammers
the RPC/price APIs. Buy/close actions are marshalled onto that same loop so
they always run through the one wallet/session the bot already owns.
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import live_logs

BOT = None
STARTED_AT = time.time()

_WALLET_CACHE_TTL = 4.0
_POS_CACHE_TTL = 4.0
_wallet_cache = {"data": {"connected": False}, "ts": 0.0}
_pos_price_cache = {"data": {}, "ts": 0.0}


def set_bot(bot_module):
    global BOT
    BOT = bot_module


def _auth_ok(header: str | None) -> bool:
    user = os.getenv("DASHBOARD_USER", "admin")
    password = os.getenv("DASHBOARD_PASSWORD", "")
    if not password or not header or not header.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(header[6:]).decode("utf-8")
        supplied_user, supplied_password = raw.split(":", 1)
    except Exception:
        return False
    return hmac.compare_digest(supplied_user, user) and hmac.compare_digest(supplied_password, password)


def _read_state() -> dict:
    path = "./state.json"
    try:
        if BOT is not None:
            path = BOT.settings.state_file
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _sniper_activity() -> dict:
    sniper = getattr(BOT, "sniper", None)
    if sniper is None or not hasattr(sniper, "get_status"):
        return {"last_tick_at": None, "last_tick_candidates_seen": 0, "activity": []}
    try:
        return sniper.get_status()
    except Exception:
        return {"last_tick_at": None, "last_tick_candidates_seen": 0, "activity": []}


def _run_bot_coro(fn_name: str, *args, timeout: float = 30.0):
    if BOT is None or not hasattr(BOT, "run_dashboard_coro") or not hasattr(BOT, fn_name):
        return {"ok": False, "error": "Bot is not ready yet. Try again in a moment."}
    try:
        coro = getattr(BOT, fn_name)(*args)
        return BOT.run_dashboard_coro(coro, timeout=timeout)
    except Exception as exc:
        return {"ok": False, "error": f"Action failed: {type(exc).__name__}"}


def _cached_wallet_snapshot() -> dict:
    now = time.time()
    if now - _wallet_cache["ts"] < _WALLET_CACHE_TTL:
        return _wallet_cache["data"]
    if BOT is None or not hasattr(BOT, "dashboard_wallet_snapshot") or not hasattr(BOT, "run_dashboard_coro"):
        return _wallet_cache["data"]
    try:
        data = BOT.run_dashboard_coro(BOT.dashboard_wallet_snapshot(), timeout=8)
    except Exception:
        data = _wallet_cache["data"]
    _wallet_cache["data"] = data
    _wallet_cache["ts"] = now
    return data


def _cached_position_prices() -> dict:
    now = time.time()
    if now - _pos_price_cache["ts"] < _POS_CACHE_TTL:
        return _pos_price_cache["data"]
    if BOT is None or not hasattr(BOT, "dashboard_positions_snapshot") or not hasattr(BOT, "run_dashboard_coro"):
        return _pos_price_cache["data"]
    try:
        data = BOT.run_dashboard_coro(BOT.dashboard_positions_snapshot(), timeout=8)
    except Exception:
        data = _pos_price_cache["data"]
    _pos_price_cache["data"] = data
    _pos_price_cache["ts"] = now
    return data


def _win_stats(history: list[dict]) -> dict:
    closed = [h for h in history if h.get("pnl_pct") is not None]
    total = len(closed)
    wins = sum(1 for h in closed if h["pnl_pct"] > 0)
    losses = total - wins
    total_pnl_sol = sum((h.get("pnl_sol_estimate") or 0) for h in history)
    return {
        "total_closed": total,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": (wins / total * 100.0) if total else None,
        "total_pnl_sol_estimate": total_pnl_sol,
    }


def _status() -> dict:
    state = _read_state()
    positions_raw = state.get("positions", {})
    history_full = state.get("history", [])
    wallet = getattr(BOT, "wallet", None)
    settings = getattr(BOT, "settings", None)
    wallet_connected = bool(wallet and wallet.configured)
    paper_trading = bool(getattr(settings, "paper_trading", False))
    address = None
    if wallet_connected:
        try:
            address = wallet.short_address()
        except Exception:
            pass

    live_prices = _cached_position_prices() if wallet_connected else {}
    positions = []
    for mint, p in positions_raw.items():
        row = {"mint": mint, **p}
        live = live_prices.get(mint) or {}
        cur = live.get("price_usd")
        entry_price = row.get("entry_price_usd")
        if cur and entry_price:
            row["current_price_usd"] = cur
            row["pnl_pct"] = (cur - entry_price) / entry_price * 100.0
            row["value_usd"] = cur * (row.get("amount_tokens") or 0)
        else:
            row["current_price_usd"] = None
            row["pnl_pct"] = None
            row["value_usd"] = None
        positions.append(row)

    wallet_snapshot = _cached_wallet_snapshot() if wallet_connected else {"connected": False}

    return {
        "service": "online", "bot_loaded": bool(BOT), "wallet_connected": wallet_connected,
        "paper_trading": paper_trading,
        "wallet_address": address,
        "wallet_balance_sol": wallet_snapshot.get("balance_sol"),
        "wallet_balance_usd": wallet_snapshot.get("balance_usd"),
        "sol_usd_price": wallet_snapshot.get("sol_usd_price"),
        "auto_sniper_enabled": bool(state.get("auto_sniper_enabled", False)),
        "auto_sniper_allocation_pct": state.get("auto_sniper_allocation_pct"),
        "slippage_bps": state.get("slippage_bps", getattr(settings, "default_slippage_bps", 500)),
        "max_buy_sol": getattr(settings, "max_buy_sol", 2.0),
        "positions": positions,
        "history": history_full[:50],
        "win_stats": _win_stats(history_full),
        "uptime_seconds": int(time.time() - STARTED_AT),
        "sniper": _sniper_activity(),
        "logs": live_logs.get_recent(150),
    }


_STYLE = """
*{box-sizing:border-box}
:root{
  --bg0:#05060a; --bg1:#0a0d16; --panel:rgba(22,26,38,.72); --panel-solid:#141826;
  --border:rgba(255,255,255,.08); --border-strong:rgba(255,255,255,.16);
  --text:#eef1fa; --muted:#8b93a8; --muted2:#5f6780;
  --green:#3ddc84; --green-soft:rgba(61,220,132,.14);
  --red:#ff5c6c; --red-soft:rgba(255,92,108,.14);
  --amber:#ffc857; --amber-soft:rgba(255,200,87,.14);
  --accent:#7c8cff; --accent2:#c86bff;
  --radius:16px;
}
body{
  margin:0;color:var(--text);font:14.5px/1.5 "Inter",system-ui,-apple-system,"Segoe UI",sans-serif;
  background:
    radial-gradient(1100px 620px at 12% -8%, rgba(124,140,255,.16), transparent 60%),
    radial-gradient(900px 560px at 100% 0%, rgba(200,107,255,.12), transparent 55%),
    radial-gradient(1200px 800px at 50% 120%, rgba(61,220,132,.07), transparent 60%),
    var(--bg0);
  min-height:100vh;
}
main{max-width:1360px;margin:auto;padding:22px 22px 60px}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:#2a3049;border-radius:8px}
::-webkit-scrollbar-thumb:hover{background:#3a4267}

header.topbar{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:18px}
.brand{display:flex;align-items:center;gap:12px}
.brand .logo{
  width:38px;height:38px;border-radius:11px;display:flex;align-items:center;justify-content:center;font-size:18px;
  background:linear-gradient(135deg,var(--accent),var(--accent2)); box-shadow:0 6px 20px rgba(124,140,255,.35);
}
.brand h1{margin:0;font-size:18px;font-weight:800;letter-spacing:.2px}
.brand .sub{color:var(--muted);font-size:12px;display:flex;align-items:center;gap:6px}
.pulse{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 0 rgba(61,220,132,.6);animation:pulse 1.6s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(61,220,132,.55)}70%{box-shadow:0 0 0 8px rgba(61,220,132,0)}100%{box-shadow:0 0 0 0 rgba(61,220,132,0)}}
.demo-pill{font-size:11px;font-weight:800;padding:4px 10px;border-radius:999px;background:var(--amber-soft);color:var(--amber);border:1px solid rgba(255,200,87,.35)}

.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:12px;margin-bottom:20px}
.stat{
  background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);padding:16px 18px;
  backdrop-filter:blur(14px); position:relative; overflow:hidden; transition:transform .18s ease, border-color .18s ease;
}
.stat:hover{transform:translateY(-2px);border-color:var(--border-strong)}
.stat .label{color:var(--muted);font-size:11.5px;font-weight:700;text-transform:uppercase;letter-spacing:.6px}
.stat .big{font-size:22px;font-weight:800;margin-top:8px;letter-spacing:.2px}
.stat .sub{color:var(--muted);font-size:12px;margin-top:4px}
.stat.accent::before{content:"";position:absolute;inset:0;background:linear-gradient(135deg,rgba(124,140,255,.10),transparent 60%);pointer-events:none}
.good{color:var(--green)} .bad{color:var(--red)} .warn{color:var(--amber)}

nav.tabs{display:flex;gap:6px;margin:0 0 20px;padding:5px;background:var(--panel);border:1px solid var(--border);border-radius:14px;width:fit-content;flex-wrap:wrap}
nav.tabs button{
  appearance:none;border:none;cursor:pointer;padding:9px 16px;border-radius:10px;font-weight:700;font-size:13px;
  color:var(--muted);background:transparent;transition:all .15s ease;
}
nav.tabs button.active{color:#fff;background:linear-gradient(135deg,var(--accent),var(--accent2));box-shadow:0 4px 16px rgba(124,140,255,.35)}
nav.tabs button:not(.active):hover{color:var(--text);background:rgba(255,255,255,.05)}

.panel{background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);padding:20px;backdrop-filter:blur(14px);margin-bottom:18px}
.panel h2{margin:0 0 4px;font-size:15px;display:flex;align-items:center;gap:8px}
.panel .desc{color:var(--muted);font-size:12.5px;margin-bottom:14px}
.section{display:none}
.section.active{display:block;animation:fadein .25s ease}
@keyframes fadein{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}

.tablewrap{max-height:460px;overflow:auto;border-radius:12px;border:1px solid var(--border)}
table{width:100%;border-collapse:collapse;background:transparent}
th,td{padding:11px 12px;border-bottom:1px solid var(--border);text-align:left;font-size:12.3px;vertical-align:middle;white-space:nowrap}
th{color:var(--muted);position:sticky;top:0;background:#12162280;backdrop-filter:blur(8px);font-weight:700;text-transform:uppercase;font-size:10.5px;letter-spacing:.5px}
tbody tr{transition:background .12s}
tbody tr:hover{background:rgba(255,255,255,.03)}
code{font-size:10.5px;word-break:break-all;color:#a9b2c9}
.small{font-size:12px;color:var(--muted)}
.muted{color:var(--muted)}

.badge{display:inline-block;padding:3px 10px;border-radius:999px;font-size:10.5px;font-weight:800;letter-spacing:.2px}
.badge-bought{background:var(--green-soft);color:var(--green)}
.badge-skipped{background:var(--amber-soft);color:var(--amber)}
.badge-error,.badge-buyfailed{background:var(--red-soft);color:var(--red)}
.lvl-INFO{color:var(--muted)} .lvl-WARNING{color:var(--amber)} .lvl-ERROR,.lvl-CRITICAL{color:var(--red);font-weight:700}
.logmsg{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11.5px;white-space:pre-wrap;word-break:break-word}
.checklist{display:flex;flex-direction:column;gap:3px;min-width:170px}
.chk{display:block;font-size:10.5px;padding:2px 6px;border-radius:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chk-pass{background:var(--green-soft);color:var(--green)}
.chk-fail{background:var(--red-soft);color:var(--red)}

/* Trade tab */
.searchbox{display:flex;gap:10px;flex-wrap:wrap}
.searchbox input{
  flex:1;min-width:260px;background:#0d1120;border:1px solid var(--border);color:var(--text);
  padding:13px 16px;border-radius:12px;font-size:13.5px;font-family:ui-monospace,Menlo,Consolas,monospace;outline:none;transition:border-color .15s;
}
.searchbox input:focus{border-color:var(--accent)}
.btn{
  appearance:none;border:none;cursor:pointer;padding:12px 20px;border-radius:12px;font-weight:800;font-size:13px;color:#fff;
  background:linear-gradient(135deg,var(--accent),var(--accent2));box-shadow:0 6px 18px rgba(124,140,255,.3);transition:transform .12s ease, box-shadow .12s ease;
}
.btn:hover{transform:translateY(-1px);box-shadow:0 10px 24px rgba(124,140,255,.4)}
.btn:disabled{opacity:.5;cursor:not-allowed;transform:none}
.btn.ghost{background:rgba(255,255,255,.06);box-shadow:none;color:var(--text);border:1px solid var(--border)}
.btn.sell{background:linear-gradient(135deg,#ff5c6c,#ff8a5c)}
.btn.sm{padding:8px 14px;font-size:12px;border-radius:9px}

.preview{margin-top:18px;border:1px solid var(--border);border-radius:14px;padding:18px;background:rgba(255,255,255,.02);display:none}
.preview.show{display:block}
.preview .ptop{display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:10px}
.preview h3{margin:0;font-size:18px}
.preview .price{font-size:24px;font-weight:800;margin-top:2px}
.pgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-top:16px}
.pgrid .cell{background:rgba(255,255,255,.03);border:1px solid var(--border);border-radius:10px;padding:10px 12px}
.pgrid .cell .l{color:var(--muted);font-size:10.5px;text-transform:uppercase;letter-spacing:.4px}
.pgrid .cell .v{font-weight:700;margin-top:3px;font-size:13.5px}
.risk-LOW{color:var(--green)} .risk-MEDIUM{color:var(--amber)} .risk-HIGH{color:var(--red)} .risk-UNKNOWN{color:var(--muted)}
.notes{margin-top:12px;font-size:12px;color:var(--muted)}
.notes div{margin-top:3px}
.amtrow{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px;align-items:center}
.amt-chip{
  padding:9px 15px;border-radius:10px;background:rgba(255,255,255,.05);border:1px solid var(--border);color:var(--text);
  cursor:pointer;font-weight:700;font-size:12.5px;transition:all .12s;
}
.amt-chip:hover,.amt-chip.sel{border-color:var(--accent);background:rgba(124,140,255,.14)}
.amt-custom{width:110px;background:#0d1120;border:1px solid var(--border);color:var(--text);padding:9px 12px;border-radius:10px;font-size:12.5px}
.banner{margin-top:14px;padding:12px 16px;border-radius:12px;font-size:13px;font-weight:600;display:none}
.banner.show{display:block}
.banner.ok{background:var(--green-soft);color:var(--green);border:1px solid rgba(61,220,132,.3)}
.banner.err{background:var(--red-soft);color:var(--red);border:1px solid rgba(255,92,108,.3)}
.banner.info{background:rgba(124,140,255,.12);color:#aab3ff;border:1px solid rgba(124,140,255,.3)}

/* Positions tab */
.poscards{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
.poscard{background:rgba(255,255,255,.02);border:1px solid var(--border);border-radius:14px;padding:16px}
.poscard .head{display:flex;justify-content:space-between;align-items:center}
.poscard .sym{font-weight:800;font-size:15px}
.poscard .pnl{font-weight:800;font-size:15px}
.poscard .row{display:flex;justify-content:space-between;font-size:12px;color:var(--muted);margin-top:8px}
.poscard .closebtns{display:flex;gap:6px;margin-top:14px;flex-wrap:wrap}
.empty{padding:30px;text-align:center;color:var(--muted)}

@media(max-width:640px){ main{padding:14px} .brand h1{font-size:16px} }
"""

_JS_COMMON = """
function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function fmtPnl(v){ return v==null ? '—' : (v>=0?'+':'') + v.toFixed(2) + '%'; }
function pnlClass(v){ return v==null ? 'muted' : (v>=0?'good':'bad'); }
function fmtSmartSl(v){ if(v==null) return '—'; if(v===0) return 'BE'; return '+'+v.toFixed(1)+'%'; }
function shortAddr(a){ return (!a || a==='—') ? '—' : (a.length>14 ? a.slice(0,6)+'…'+a.slice(-6) : a); }
function timeAgo(sec){
  if(sec==null) return 'never';
  const d = Math.max(0, Math.floor(Date.now()/1000 - sec));
  if(d<2) return 'just now';
  if(d<60) return d+'s ago';
  if(d<3600) return Math.floor(d/60)+'m ago';
  return Math.floor(d/3600)+'h ago';
}
function fmtClock(sec){ return sec==null ? '—' : new Date(sec*1000).toLocaleTimeString(); }
function num(v, digits, prefix, suffix){
  if(v==null) return '—';
  return (prefix||'') + Number(v).toLocaleString(undefined,{minimumFractionDigits:digits,maximumFractionDigits:digits}) + (suffix||'');
}
const BADGE_CLASS = {BOUGHT:'badge-bought', SKIPPED:'badge-skipped', ERROR:'badge-error', BUY_FAILED:'badge-buyfailed'};
function renderChecks(checks){
  if(!checks || !checks.length) return '—';
  return '<div class="checklist">' + checks.map(c => {
    const cls = c.passed ? 'chk-pass' : 'chk-fail';
    const mark = c.passed ? '✓' : '✕';
    const title = esc(c.label + ': ' + c.detail);
    return `<span class="chk ${cls}" title="${title}">${mark} ${esc(c.label)}</span>`;
  }).join('') + '</div>';
}
async function refresh(renderFn){
  try {
    const r = await fetch('/api/status', {cache:'no-store'});
    if(!r.ok) return;
    renderFn(await r.json());
  } catch(e) { /* transient network hiccup -- next poll will retry */ }
}
async function postJSON(url, body){
  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body||{})});
  let data;
  try { data = await r.json(); } catch(e) { data = {ok:false, error:'Bad response from server'}; }
  return data;
}
function switchTab(name){
  document.querySelectorAll('nav.tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.section').forEach(s => s.classList.toggle('active', s.id === 'tab-' + name));
  try { localStorage.setItem('sb_tab', name); } catch(e) {}
}
"""


def _page() -> bytes:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Solana Trading Bot — Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<style>{_STYLE}</style></head><body><main>

<header class="topbar">
  <div class="brand">
    <div class="logo">◎</div>
    <div>
      <h1>Solana Trading Bot</h1>
      <div class="sub"><span class="pulse"></span>Live · auto-refreshing every 2s · last update <span id="t">—</span></div>
    </div>
  </div>
  <div id="demoPill"></div>
</header>

<div class="stats" id="statCards"></div>

<nav class="tabs">
  <button data-tab="overview" class="active" onclick="switchTab('overview')">🎯 Overview</button>
  <button data-tab="trade" onclick="switchTab('trade')">⚡ Trade</button>
  <button data-tab="positions" onclick="switchTab('positions')">🟢 Positions</button>
  <button data-tab="history" onclick="switchTab('history')">📚 History</button>
  <button data-tab="logs" onclick="switchTab('logs')">📜 Logs</button>
</nav>

<section id="tab-overview" class="section active">
  <div class="panel">
    <h2>Auto-Sniper activity <span class="small" id="sniperMeta"></span></h2>
    <div class="desc">Every token the sniper looks at, with the reason it passed or was skipped. System/error messages are on the Logs tab.</div>
    <div class="tablewrap"><table><thead><tr><th>Time</th><th>Token</th><th>Mint</th><th>Result</th><th>Reason</th><th>Price</th><th>Liquidity</th><th>MC</th><th>5m Vol</th><th>5m Δ</th><th>Age</th><th>Risk</th><th>RugCheck</th><th>Checks</th></tr></thead><tbody id="activityBody"><tr><td colspan="14" class="muted">Waiting for first scan…</td></tr></tbody></table></div>
  </div>
</section>

<section id="tab-trade" class="section">
  <div class="panel">
    <h2>⚡ Buy a token</h2>
    <div class="desc">Paste a Solana mint address (or any text containing one) to pull up its live market data, then choose an amount to buy.</div>
    <div class="searchbox">
      <input id="mintInput" placeholder="Paste token contract address (CA) / mint…" autocomplete="off" spellcheck="false">
      <button class="btn" id="lookupBtn" onclick="doLookup()">🔎 Search</button>
    </div>
    <div class="banner" id="tradeBanner"></div>
    <div class="preview" id="preview"></div>
  </div>
</section>

<section id="tab-positions" class="section">
  <div class="panel">
    <h2>🟢 Open positions</h2>
    <div class="desc">No fixed take-profit. Positions use −30% hard protection, +5% break-even, then a ratcheting profit lock at 50% of peak profit. Close any amount below.</div>
    <div class="banner" id="posBanner"></div>
    <div class="poscards" id="posCards"><div class="empty">Loading…</div></div>
  </div>
</section>

<section id="tab-history" class="section">
  <div class="panel">
    <h2>📚 Closed trade history</h2>
    <div class="desc">Every completed trade keeps its entry snapshot, contract address, entry/exit prices, close reason, and buy/sell transaction signatures. Capped at 500 records.</div>
    <div class="tablewrap"><table><thead><tr><th>Token</th><th>CA / Mint</th><th>Entry</th><th>Exit</th><th>PnL</th><th>Reason</th><th>Sell Tx</th></tr></thead><tbody id="histBody"><tr><td colspan="7" class="muted">Loading…</td></tr></tbody></table></div>
    <p class="small">No private key or recovery phrase is ever displayed.</p>
  </div>
</section>

<section id="tab-logs" class="section">
  <div class="panel">
    <h2>📜 Live logs <span class="small" id="logsMeta"></span></h2>
    <div class="desc">Same messages as the server logs, streamed here so you don't need to leave the dashboard.</div>
    <div class="tablewrap"><table><thead><tr><th style="width:90px">Time</th><th style="width:70px">Level</th><th style="width:110px">Source</th><th>Message</th></tr></thead><tbody id="logsBody"><tr><td colspan="4" class="muted">No log records yet.</td></tr></tbody></table></div>
  </div>
</section>

<script>
{_JS_COMMON}
let LAST_STATUS = null;
let SELECTED_AMOUNT = 0.05;
let CURRENT_MINT = null;

(function initTab(){{
  let saved = 'overview';
  try {{ saved = localStorage.getItem('sb_tab') || 'overview'; }} catch(e) {{}}
  switchTab(saved);
}})();

function renderStats(s){{
  const walletAddr = shortAddr(s.wallet_address);
  const sniperOn = s.auto_sniper_enabled ? 'ON' : 'OFF';
  const allocation = (s.auto_sniper_allocation_pct!=null) ? (s.auto_sniper_allocation_pct+'%') : '—';
  const bal = s.wallet_balance_sol;
  const balUsd = s.wallet_balance_usd;
  const win = s.win_stats || {{}};
  const winTxt = win.win_rate_pct==null ? '—' : win.win_rate_pct.toFixed(1)+'%';
  const winCls = win.win_rate_pct==null ? '' : (win.win_rate_pct>=50 ? 'good' : 'bad');

  document.getElementById('demoPill').innerHTML = s.paper_trading ? '<span class="demo-pill">🧪 DEMO MODE</span>' : '';

  document.getElementById('statCards').innerHTML = `
    <div class="stat accent"><div class="label">Balance</div><div class="big">${{bal!=null?bal.toFixed(4)+' SOL':'—'}}</div><div class="sub">${{balUsd!=null?'$'+balUsd.toLocaleString(undefined,{{maximumFractionDigits:2}}):(s.wallet_connected?'price unavailable':'wallet not connected')}}</div></div>
    <div class="stat"><div class="label">Win rate</div><div class="big ${{winCls}}">${{winTxt}}</div><div class="sub">${{win.wins||0}}W / ${{win.losses||0}}L · ${{win.total_closed||0}} closed</div></div>
    <div class="stat"><div class="label">Realized PnL</div><div class="big ${{pnlClass(win.total_pnl_sol_estimate)}}">${{win.total_pnl_sol_estimate!=null?(win.total_pnl_sol_estimate>=0?'+':'')+win.total_pnl_sol_estimate.toFixed(4)+' SOL':'—'}}</div><div class="sub">estimated, from closed trades</div></div>
    <div class="stat"><div class="label">Open positions</div><div class="big">${{s.positions.length}}</div><div class="sub">${{esc(walletAddr)}}</div></div>
    <div class="stat"><div class="label">Auto-Sniper</div><div class="big ${{s.auto_sniper_enabled?'good':''}}">${{sniperOn}}</div><div class="sub">Allocation: ${{esc(allocation)}}</div></div>
    <div class="stat"><div class="label">Service</div><div class="big good">ONLINE</div><div class="sub">${{s.bot_loaded?'Bot running':'Bot offline'}} · up ${{Math.floor(s.uptime_seconds/60)}}m</div></div>`;
}}

function renderOverview(s){{
  const sn = s.sniper || {{last_tick_at:null, last_tick_candidates_seen:0, activity:[]}};
  document.getElementById('sniperMeta').textContent =
    `— last scan ${{timeAgo(sn.last_tick_at)}} · ${{sn.last_tick_candidates_seen||0}} candidates in that pass`;
  document.getElementById('activityBody').innerHTML = (sn.activity && sn.activity.length) ? sn.activity.map(a => `
    <tr><td>${{timeAgo(a.time)}}</td><td><b>${{esc(a.symbol||'?')}}</b></td><td><code>${{esc(a.mint)}}</code></td>
    <td><span class="badge ${{BADGE_CLASS[a.verdict]||'badge-skipped'}}">${{esc(a.verdict)}}</span></td><td>${{esc(a.reason)}}</td>
    <td>${{a.price_usd!=null? '$'+Number(a.price_usd).toFixed(10):'—'}}</td>
    <td>${{num(a.liquidity_usd,0,'$')}}</td><td>${{num(a.market_cap_usd,0,'$')}}</td>
    <td>${{num(a.volume_5m_usd,0,'$')}}</td><td>${{a.change_5m_pct!=null? num(a.change_5m_pct,1,'',' %'):'—'}}</td>
    <td>${{a.age_minutes!=null? Math.round(a.age_minutes)+'m':'—'}}</td>
    <td>${{a.risk_score!=null? a.risk_score+'/100':'—'}}</td><td>${{esc(a.rug_level||'—')}}</td>
    <td>${{renderChecks(a.checks)}}</td></tr>`
  ).join('') : '<tr><td colspan="14" class="muted">No candidates scanned yet — sniper may be OFF, or still on its first pass.</td></tr>';
}}

function renderPositions(s){{
  const wrap = document.getElementById('posCards');
  if(!s.wallet_connected){{ wrap.innerHTML = '<div class="empty">🔌 Wallet not connected.</div>'; return; }}
  if(!s.positions || !s.positions.length){{ wrap.innerHTML = '<div class="empty">No open positions yet — buy a token from the Trade tab.</div>'; return; }}
  wrap.innerHTML = s.positions.map(p => {{
    const pnl = p.pnl_pct;
    return `<div class="poscard">
      <div class="head"><span class="sym">$${{esc(p.symbol||'?')}}</span><span class="pnl ${{pnlClass(pnl)}}">${{fmtPnl(pnl)}}</span></div>
      <div class="row"><span>Entry</span><span>$${{Number(p.entry_price_usd||0).toFixed(8)}}</span></div>
      <div class="row"><span>Now</span><span>${{p.current_price_usd!=null?'$'+Number(p.current_price_usd).toFixed(8):'—'}}</span></div>
      <div class="row"><span>Amount</span><span>${{Number(p.amount_tokens||0).toFixed(4)}}</span></div>
      <div class="row"><span>Value</span><span>${{p.value_usd!=null?'$'+Number(p.value_usd).toLocaleString(undefined,{{maximumFractionDigits:2}}):'—'}}</span></div>
      <div class="row"><span>Hard SL</span><span>-${{Number(p.stop_loss_pct||30)}}%</span></div>
      <div class="row"><span>Smart SL</span><span>${{fmtSmartSl(p.smart_stop_profit_pct)}}</span></div>
      <div class="row"><span>Mint</span><span><code>${{esc((p.mint||'').slice(0,6))}}…${{esc((p.mint||'').slice(-6))}}</code></span></div>
      <div class="closebtns">
        <button class="btn sell sm" onclick="doClose('${{p.mint}}',0.25,this)">Sell 25%</button>
        <button class="btn sell sm" onclick="doClose('${{p.mint}}',0.5,this)">Sell 50%</button>
        <button class="btn sell sm" onclick="doClose('${{p.mint}}',0.75,this)">Sell 75%</button>
        <button class="btn sell sm" onclick="doClose('${{p.mint}}',1,this)">Close 100%</button>
      </div>
    </div>`;
  }}).join('');
}}

function renderHistory(s){{
  document.getElementById('histBody').innerHTML = (s.history && s.history.length) ? s.history.map(h => `
    <tr><td><b>${{esc(h.symbol||'?')}}</b></td><td><code>${{esc(h.mint||'')}}</code></td>
    <td>${{Number(h.entry_price_usd||0).toFixed(8)}}</td><td>${{Number(h.exit_price_usd||0).toFixed(8)}}</td>
    <td class="${{pnlClass(h.pnl_pct)}}"><b>${{fmtPnl(h.pnl_pct)}}</b></td><td>${{esc(h.close_reason||'—')}}</td><td><code>${{esc(h.sell_signature||'')}}</code></td></tr>`
  ).join('') : '<tr><td colspan="7" class="muted">No closed trades yet</td></tr>';
}}

function renderLogs(s){{
  const logs = s.logs || [];
  document.getElementById('logsMeta').textContent = logs.length ? `— ${{logs.length}} recent entries` : '';
  document.getElementById('logsBody').innerHTML = logs.length ? logs.map(l => `
    <tr><td>${{fmtClock(l.time)}}</td><td class="lvl-${{esc(l.level)}}">${{esc(l.level)}}</td>
    <td>${{esc(l.logger)}}</td><td class="logmsg">${{esc(l.message)}}</td></tr>`
  ).join('') : '<tr><td colspan="4" class="muted">No log records yet.</td></tr>';
}}

function render(s){{
  LAST_STATUS = s;
  renderStats(s);
  renderOverview(s);
  renderPositions(s);
  renderHistory(s);
  renderLogs(s);
  document.getElementById('t').textContent = new Date().toLocaleTimeString();
}}
refresh(render);
setInterval(() => refresh(render), 2000);

// ---------------- Trade tab ----------------
function showBanner(id, kind, msg){{
  const el = document.getElementById(id);
  el.className = 'banner show ' + kind;
  el.textContent = msg;
}}
function hideBanner(id){{ document.getElementById(id).className = 'banner'; }}

async function doLookup(){{
  const text = document.getElementById('mintInput').value.trim();
  if(!text){{ showBanner('tradeBanner','err','Paste a token address first.'); return; }}
  const btn = document.getElementById('lookupBtn');
  btn.disabled = true; btn.textContent = '⏳ Loading…';
  hideBanner('tradeBanner');
  const res = await postJSON('/api/lookup', {{text}});
  btn.disabled = false; btn.textContent = '🔎 Search';
  if(!res.ok){{ showBanner('tradeBanner','err', res.error || 'Lookup failed.'); document.getElementById('preview').className='preview'; return; }}
  CURRENT_MINT = res.mint;
  renderPreview(res);
}}

function renderPreview(t){{
  const el = document.getElementById('preview');
  el.className = 'preview show';
  const riskCls = 'risk-' + (t.risk_level || 'UNKNOWN');
  el.innerHTML = `
    <div class="ptop">
      <div><h3>${{esc(t.name||'?')}} <span class="muted">$${{esc(t.symbol||'?')}}</span></h3><code>${{esc(t.mint)}}</code></div>
      <div style="text-align:right"><div class="price">$${{Number(t.price_usd||0).toFixed(10)}}</div><div class="small ${{riskCls}}">RugCheck: ${{esc(t.risk_level||'UNKNOWN')}}${{t.risk_score!=null?' ('+t.risk_score+'/100)':''}}</div></div>
    </div>
    <div class="pgrid">
      <div class="cell"><div class="l">Market cap</div><div class="v">${{num(t.market_cap_usd,0,'$')}}</div></div>
      <div class="cell"><div class="l">Liquidity</div><div class="v">${{num(t.liquidity_usd,0,'$')}}</div></div>
      <div class="cell"><div class="l">5m change</div><div class="v ${{pnlClass(t.change_5m_pct)}}">${{fmtPnl(t.change_5m_pct)}}</div></div>
      <div class="cell"><div class="l">1h change</div><div class="v ${{pnlClass(t.change_1h_pct)}}">${{fmtPnl(t.change_1h_pct)}}</div></div>
      <div class="cell"><div class="l">DEX</div><div class="v">${{esc(t.dex||'—')}}</div></div>
      <div class="cell"><div class="l">Your balance</div><div class="v">${{t.wallet_balance_tokens!=null?Number(t.wallet_balance_tokens).toFixed(4):'—'}}</div></div>
    </div>
    ${{(t.notes && t.notes.length) ? '<div class="notes">'+t.notes.map(n=>'<div>• '+esc(n)+'</div>').join('')+'</div>' : ''}}
    <div class="amtrow" id="amtRow"></div>
    <button class="btn" id="buyBtn" style="margin-top:16px" onclick="doBuy()">💰 Buy ${{SELECTED_AMOUNT}} SOL</button>
    ${{!t.wallet_connected ? '<div class="small" style="margin-top:10px">🔌 Connect a wallet from Telegram (/connect_wallet) before buying.</div>' : ''}}
  `;
  const chips = [0.01,0.05,0.1,0.25,0.5];
  const row = document.getElementById('amtRow');
  row.innerHTML = chips.map(a => `<div class="amt-chip ${{a===SELECTED_AMOUNT?'sel':''}}" onclick="pickAmount(${{a}})">${{a}} SOL</div>`).join('')
    + `<input class="amt-custom" type="number" min="0" step="0.001" placeholder="custom SOL" onchange="pickAmount(parseFloat(this.value)||0)">`;
}}

function pickAmount(a){{
  if(!a || a<=0) return;
  SELECTED_AMOUNT = a;
  document.querySelectorAll('.amt-chip').forEach(c => c.classList.toggle('sel', c.textContent.trim() === (a+' SOL')));
  const btn = document.getElementById('buyBtn');
  if(btn) btn.textContent = `💰 Buy ${{a}} SOL`;
}}

async function doBuy(){{
  if(!CURRENT_MINT){{ return; }}
  const btn = document.getElementById('buyBtn');
  btn.disabled = true; btn.textContent = '⏳ Buying…';
  hideBanner('tradeBanner');
  const res = await postJSON('/api/buy', {{mint: CURRENT_MINT, sol_amount: SELECTED_AMOUNT}});
  btn.disabled = false; btn.textContent = `💰 Buy ${{SELECTED_AMOUNT}} SOL`;
  if(res.ok){{
    showBanner('tradeBanner','ok', `✅ Bought $${{res.symbol||''}} — tx ${{(res.signature||'').slice(0,20)}}…`);
    refresh(render);
  }} else {{
    showBanner('tradeBanner','err', res.error || 'Buy failed.');
  }}
}}

// ---------------- Positions tab ----------------
async function doClose(mint, fraction, btnEl){{
  const label = btnEl.textContent;
  btnEl.disabled = true; btnEl.textContent = '⏳…';
  hideBanner('posBanner');
  const res = await postJSON('/api/sell', {{mint, fraction}});
  btnEl.disabled = false; btnEl.textContent = label;
  if(res.ok){{
    const pnlTxt = res.pnl_pct!=null ? ` — PnL ${{fmtPnl(res.pnl_pct)}}` : '';
    showBanner('posBanner','ok', `✅ Closed ${{Math.round(fraction*100)}}% of $${{res.symbol||''}}${{pnlTxt}}`);
    refresh(render);
  }} else {{
    showBanner('posBanner','err', res.error || 'Sell failed.');
  }}
}}

document.getElementById('mintInput').addEventListener('keydown', e => {{ if(e.key === 'Enter') doLookup(); }});
</script>
</main></body></html>""".encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _send(self, code, body, content_type="text/html; charset=utf-8"):
        self.send_response(code); self.send_header("Content-Type", content_type); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)

    def _authed(self) -> bool:
        if _auth_ok(self.headers.get("Authorization")):
            return True
        self.send_response(401); self.send_header("WWW-Authenticate", 'Basic realm="Solana Bot Dashboard"'); self.end_headers()
        return False

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._send(200, b"ok", "text/plain; charset=utf-8"); return
        if not self._authed():
            return
        if path == "/api/status":
            self._send(200, json.dumps(_status()).encode(), "application/json; charset=utf-8"); return
        if path in ("/", "/dashboard", "/tokens", "/logs", "/positions", "/history", "/trade"):
            self._send(200, _page()); return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._authed():
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except Exception:
            self._send(400, json.dumps({"ok": False, "error": "Invalid request body."}).encode(), "application/json; charset=utf-8"); return

        if path == "/api/lookup":
            text = str(payload.get("text") or payload.get("mint") or "")
            result = _run_bot_coro("dashboard_lookup", text, timeout=15)
        elif path == "/api/buy":
            result = _run_bot_coro("dashboard_buy", payload.get("mint"), payload.get("sol_amount"), timeout=40)
        elif path == "/api/sell":
            result = _run_bot_coro("dashboard_sell", payload.get("mint"), payload.get("fraction"), timeout=40)
        else:
            self._send(404, json.dumps({"ok": False, "error": "not found"}).encode(), "application/json; charset=utf-8"); return

        if not isinstance(result, dict):
            result = {"ok": False, "error": "Unexpected response from bot."}
        self._send(200, json.dumps(result).encode(), "application/json; charset=utf-8")


def serve():
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Dashboard listening on 0.0.0.0:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    serve()
