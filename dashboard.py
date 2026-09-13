"""Live read-only web dashboard for the Telegram trading bot."""
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


def _paper_balance() -> dict | None:
    """Read the demo/paper wallet balance straight off disk -- sync, free,
    no RPC calls (so this never adds to the rate-limit pressure the sniper
    already puts on the RPC/DexScreener/Jupiter endpoints). Live-wallet
    balance isn't wired up here yet since that needs an async RPC call
    made from the bot's own event loop, not this thread; returns None in
    that case and the dashboard shows the wallet's short address only.
    """
    settings = getattr(BOT, "settings", None)
    if not settings or not getattr(settings, "paper_trading", False):
        return None
    try:
        base, _ext = os.path.splitext(getattr(settings, "state_file", None) or "./state.json")
        with open(f"{base}.paper.json", "r", encoding="utf-8") as f:
            data = json.load(f)
        return {
            "sol_balance": float(data.get("sol_balance", 0.0)),
            "token_balances": data.get("token_balances") or {},
        }
    except Exception:
        return None


def _performance_stats(history: list[dict]) -> dict:
    """Win rate / PnL summary computed over the *full* closed-trade
    history (not just the 50 most recent shown in the table), so the
    number on screen matches what actually happened."""
    pnls = [float(h.get("pnl_pct") or 0.0) for h in history if h.get("pnl_pct") is not None]
    if not pnls:
        return {
            "total_trades": len(history), "wins": 0, "losses": 0, "win_rate_pct": None,
            "total_pnl_pct": 0.0, "avg_win_pct": None, "avg_loss_pct": None,
            "best_trade_pct": None, "worst_trade_pct": None,
        }
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    return {
        "total_trades": len(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": (len(wins) / len(pnls)) * 100.0,
        "total_pnl_pct": sum(pnls),
        "avg_win_pct": (sum(wins) / len(wins)) if wins else None,
        "avg_loss_pct": (sum(losses) / len(losses)) if losses else None,
        "best_trade_pct": max(pnls),
        "worst_trade_pct": min(pnls),
    }


def _status() -> dict:
    state = _read_state()
    positions = state.get("positions", {})
    history = state.get("history", [])
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
    return {
        "service": "online", "bot_loaded": bool(BOT), "wallet_connected": wallet_connected,
        "paper_trading": paper_trading,
        "wallet_address": address, "auto_sniper_enabled": bool(state.get("auto_sniper_enabled", False)),
        "auto_sniper_allocation_pct": state.get("auto_sniper_allocation_pct"),
        "slippage_bps": state.get("slippage_bps", getattr(settings, "default_slippage_bps", 500)),
        "positions": [{"mint": m, **p} for m, p in positions.items()],
        "history": history[:50], "uptime_seconds": int(time.time() - STARTED_AT),
        "sniper": _sniper_activity(),
        "logs": live_logs.get_recent(150),
        "balance": _paper_balance(),
        "stats": _performance_stats(history),
    }


_STYLE = """
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&display=swap');
:root{
  --bg:#08090d; --surface:#10121a; --surface-2:#151822; --line:#1f232f; --line-soft:#181b25;
  --ink:#e7e9f0; --ink-dim:#828a9c; --ink-faint:#565d6e;
  --pos:#2fe6a6; --pos-dim:#123a2e; --neg:#ff5c6c; --neg-dim:#3a1620; --warn:#f2b84b; --warn-dim:#302411;
  --mono:'IBM Plex Mono',ui-monospace,Menlo,Consolas,monospace;
  --sans:-apple-system,'Segoe UI',system-ui,sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 var(--sans);-webkit-font-smoothing:antialiased}
main{max-width:1180px;margin:auto;padding:28px 20px 60px}
h2{margin:36px 0 4px;font-size:15px;font-weight:600;display:flex;align-items:center;gap:8px;letter-spacing:.01em}
.muted{color:var(--ink-dim)}
.faint{color:var(--ink-faint)}
.mono{font-family:var(--mono);font-variant-numeric:tabular-nums}

/* header */
.brand{display:flex;align-items:baseline;justify-content:space-between;flex-wrap:wrap;gap:8px 16px}
.brand h1{margin:0;font-size:20px;font-weight:600;letter-spacing:-.01em}
.livestat{font-size:12.5px;color:var(--ink-dim);display:flex;align-items:center;gap:6px}
.pulse{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--pos);animation:pulse 1.6s infinite}
@keyframes pulse{0%{opacity:1}50%{opacity:.3}100%{opacity:1}}

/* tab nav — underline style, not pill buttons */
nav.tabs{margin:22px 0 0;display:flex;gap:22px;border-bottom:1px solid var(--line)}
nav.tabs a{padding:0 0 11px;text-decoration:none;font-weight:600;font-size:13.5px;color:var(--ink-faint);border-bottom:2px solid transparent;margin-bottom:-1px}
nav.tabs a.active{color:var(--ink);border-bottom-color:var(--pos)}

/* hero: the number that matters, not a card */
.hero{display:flex;gap:0;margin:26px 0 6px;flex-wrap:wrap}
.hero-stat{flex:1 1 180px;padding:0 26px 0 0;border-right:1px solid var(--line)}
.hero-stat:last-child{border-right:none}
.hero-num{font-family:var(--mono);font-size:32px;font-weight:600;line-height:1.1;letter-spacing:-.01em}
.hero-label{font-size:12px;color:var(--ink-dim);margin-top:6px}
.hero-sub{font-size:11.5px;color:var(--ink-faint);margin-top:3px;font-family:var(--mono)}
.pos{color:var(--pos)}
.neg{color:var(--neg)}

/* win/loss bar */
.wlbar{height:6px;border-radius:3px;overflow:hidden;display:flex;background:var(--line-soft);margin-top:14px}
.wlbar .w{background:var(--pos)}
.wlbar .l{background:var(--neg)}

/* secondary status strip — tags, not repeated cards */
.tags{display:flex;flex-wrap:wrap;gap:8px;margin:18px 0 0}
.tag{display:flex;align-items:center;gap:7px;padding:7px 12px;border:1px solid var(--line);border-radius:8px;font-size:12.5px;background:var(--surface)}
.tag .dot{width:6px;height:6px;border-radius:50%;background:var(--ink-faint);flex:none}
.tag .dot.on{background:var(--pos)}
.tag .dot.off{background:var(--neg)}
.tag b{font-weight:600;color:var(--ink)}

table{width:100%;border-collapse:collapse;background:var(--surface);border-radius:10px;overflow:hidden;border:1px solid var(--line)}
th,td{padding:10px 11px;border-bottom:1px solid var(--line-soft);text-align:left;font-size:12px;vertical-align:top}
th{color:var(--ink-dim);font-weight:600;position:sticky;top:0;background:var(--surface-2)}
tbody tr:last-child td{border-bottom:none}
code{font-family:var(--mono);font-size:10px;word-break:break-all;color:var(--ink-dim)}
.note{background:var(--surface);border:1px solid var(--line);border-left:2px solid var(--warn);padding:13px 15px;border-radius:8px;margin-top:20px}
.note.demo{border-left-color:var(--pos)}
.small{font-size:12px;color:var(--ink-dim)}
.tablewrap{max-height:420px;overflow-y:auto;border-radius:10px}
.badge{display:inline-block;padding:2px 9px;border-radius:5px;font-size:11px;font-weight:600}
.badge-bought{background:var(--pos-dim);color:var(--pos)}
.badge-skipped{background:var(--warn-dim);color:var(--warn)}
.badge-error{background:var(--neg-dim);color:var(--neg)}
.badge-buyfailed{background:var(--neg-dim);color:var(--neg)}
.lvl-INFO{color:var(--ink-dim)}
.lvl-WARNING{color:var(--warn)}
.lvl-ERROR{color:var(--neg)}
.lvl-CRITICAL{color:var(--neg);font-weight:700}
.logmsg{font-family:var(--mono);font-size:11.5px;white-space:pre-wrap;word-break:break-word}
.checklist{display:flex;flex-direction:column;gap:3px;min-width:170px}
.chk{display:block;font-size:10.5px;padding:2px 6px;border-radius:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chk-pass{background:var(--pos-dim);color:var(--pos)}
.chk-fail{background:var(--neg-dim);color:var(--neg)}
@media (max-width:640px){
  .hero-stat{flex-basis:45%;border-right:none;padding-right:0;margin-bottom:14px}
  .hero-num{font-size:26px}
}
"""

_JS_COMMON = """
function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function fmtPnl(v){ return v==null ? '—' : (v>=0?'+':'') + v.toFixed(2) + '%'; }
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
const BADGE_CLASS = {BOUGHT:'badge-bought', SKIPPED:'badge-skipped', ERROR:'badge-error', BUY_FAILED:'badge-buyfailed'};
async function refresh(renderFn){
  try {
    const r = await fetch('/api/status', {cache:'no-store'});
    if(!r.ok) return;
    renderFn(await r.json());
  } catch(e) { /* transient network hiccup -- next poll will retry */ }
}
"""


def _nav(active: str) -> str:
    def cls(name):
        return "active" if name == active else ""
    return f"""<nav class="tabs">
<a class="{cls('tokens')}" href="/tokens">🎯 Tokens</a>
<a class="{cls('logs')}" href="/logs">📜 Logs</a>
</nav>"""


def _page_tokens() -> bytes:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Solana Bot — Tokens</title>
<style>{_STYLE}</style></head><body><main>

<div class="brand">
  <h1>Solana Trading Bot</h1>
  <div class="livestat"><span class="pulse"></span>updated <span id="t">just now</span></div>
</div>
{_nav('tokens')}
<div id="demoBanner"></div>

<div class="hero" id="heroStats"></div>
<div class="wlbar" id="wlBar" style="display:none"><div class="w" id="wlW"></div><div class="l" id="wlL"></div></div>

<div class="tags" id="statTags"></div>

<h2>Auto-Sniper activity <span class="small" id="sniperMeta"></span></h2>
<div class="small">Every token the sniper looks at is listed below with the exact reason it passed or was skipped — updated live. System/error messages are on the <a href="/logs" style="color:var(--ink)">Logs</a> page.</div><br>
<div class="tablewrap"><table><thead><tr><th>Time</th><th>Token</th><th>Mint</th><th>Result</th><th>Reason</th><th>Price</th><th>Liquidity</th><th>MC</th><th>5m Vol</th><th>5m Δ</th><th>Age</th><th>Risk</th><th>RugCheck</th><th>Checks</th></tr></thead><tbody id="activityBody"><tr><td colspan="14" class="muted">Waiting for first scan…</td></tr></tbody></table></div>

<h2>Open positions</h2><div class="small">No fixed TP. Positions use -30% hard protection, +5% break-even, then a ratcheting profit lock at 50% of peak profit.</div><br>
<table><thead><tr><th>Token</th><th>CA / Mint</th><th>Entry</th><th>Amount</th><th>TP</th><th>Hard SL</th><th>Smart SL</th><th>Peak PnL</th></tr></thead><tbody id="posBody"><tr><td colspan="8" class="muted">Loading…</td></tr></tbody></table>

<h2>Closed trade history</h2>
<table><thead><tr><th>Token</th><th>CA / Mint</th><th>Entry</th><th>Exit</th><th>PnL</th><th>Reason</th><th>Sell Tx</th></tr></thead><tbody id="histBody"><tr><td colspan="7" class="muted">Loading…</td></tr></tbody></table>

<div class="note"><b>Trade journal</b><br>Each completed trade keeps its entry snapshot, contract address, market/security data, entry and exit prices, close reason, peak/Smart-SL state, and buy/sell transaction signatures. The history is capped at 500 records and stored atomically in the bot state file.</div>
<p class="small">No private key or recovery phrase is displayed.</p>

<script>
{_JS_COMMON}
function num(v, digits, prefix, suffix){{
  if(v==null) return '—';
  return (prefix||'') + Number(v).toLocaleString(undefined,{{minimumFractionDigits:digits,maximumFractionDigits:digits}}) + (suffix||'');
}}
function renderChecks(checks){{
  if(!checks || !checks.length) return '—';
  return '<div class="checklist">' + checks.map(c => {{
    const cls = c.passed ? 'chk-pass' : 'chk-fail';
    const mark = c.passed ? '✓' : '✕';
    const title = esc(c.label + ': ' + c.detail);
    return `<span class="chk ${{cls}}" title="${{title}}">${{mark}} ${{esc(c.label)}}</span>`;
  }}).join('') + '</div>';
}}
function render(s){{
  document.getElementById('demoBanner').innerHTML = s.paper_trading
    ? '<div class="note demo"><b>Demo mode</b> — balances and trades below are simulated. No real funds, no real transactions.</div>' : '';

  // ---- hero: balance, win rate, total P&L ----
  const st = s.stats || {{}};
  const bal = s.balance;
  const balStr = bal ? Number(bal.sol_balance).toLocaleString(undefined,{{minimumFractionDigits:3,maximumFractionDigits:3}}) + ' SOL'
                      : (s.paper_trading ? '—' : 'live balance n/a');
  const wr = st.win_rate_pct;
  const wrStr = wr==null ? '—' : wr.toFixed(0) + '%';
  const wrCls = wr==null ? '' : (wr>=50 ? 'pos' : 'neg');
  const pnl = st.total_pnl_pct || 0;
  const pnlStr = st.total_trades ? fmtPnl(pnl) : '—';
  const pnlCls = pnl>0 ? 'pos' : (pnl<0 ? 'neg' : '');

  document.getElementById('heroStats').innerHTML = `
    <div class="hero-stat"><div class="hero-num">${{balStr}}</div><div class="hero-label">Wallet balance</div><div class="hero-sub">${{esc(shortAddr(s.wallet_address))}}</div></div>
    <div class="hero-stat"><div class="hero-num ${{wrCls}}">${{wrStr}}</div><div class="hero-label">Win rate</div><div class="hero-sub">${{st.wins||0}}W / ${{st.losses||0}}L of ${{st.total_trades||0}} closed</div></div>
    <div class="hero-stat"><div class="hero-num ${{pnlCls}}">${{pnlStr}}</div><div class="hero-label">Total P&amp;L (closed trades)</div><div class="hero-sub">best ${{fmtPnl(st.best_trade_pct)}} · worst ${{fmtPnl(st.worst_trade_pct)}}</div></div>
    <div class="hero-stat"><div class="hero-num">${{s.positions.length}}</div><div class="hero-label">Open positions</div><div class="hero-sub">${{(s.sniper&&s.sniper.last_tick_candidates_seen)||0}} candidates last scan</div></div>`;

  const wlBar = document.getElementById('wlBar');
  if(st.total_trades > 0){{
    wlBar.style.display = 'flex';
    document.getElementById('wlW').style.width = (st.win_rate_pct||0) + '%';
    document.getElementById('wlL').style.width = (100 - (st.win_rate_pct||0)) + '%';
  }} else {{ wlBar.style.display = 'none'; }}

  // ---- secondary status tags ----
  const sniperOn = s.auto_sniper_enabled ? 'ON' : 'OFF';
  const allocation = (s.auto_sniper_allocation_pct!=null) ? (s.auto_sniper_allocation_pct+'%') : '—';
  document.getElementById('statTags').innerHTML = `
    <div class="tag"><span class="dot on"></span>Service <b>online</b></div>
    <div class="tag"><span class="dot ${{s.bot_loaded?'on':'off'}}"></span>Bot <b>${{s.bot_loaded?'running':'offline'}}</b></div>
    <div class="tag"><span class="dot ${{s.wallet_connected?'on':'off'}}"></span>Wallet <b>${{s.wallet_connected?'connected':'not connected'}}</b></div>
    <div class="tag"><span class="dot ${{s.auto_sniper_enabled?'on':'off'}}"></span>Auto-sniper <b>${{sniperOn}}</b> · ${{esc(allocation)}} alloc</div>
    <div class="tag"><span class="dot on"></span>Closed history <b>${{s.history.length}}</b></div>`;

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

  document.getElementById('posBody').innerHTML = (s.positions && s.positions.length) ? s.positions.map(p => `
    <tr><td><b>${{esc(p.symbol||'?')}}</b></td><td><code>${{esc(p.mint||'')}}</code></td>
    <td>${{Number(p.entry_price_usd||0).toFixed(8)}}</td><td>${{Number(p.amount_tokens||0).toFixed(6)}}</td>
    <td>Disabled</td><td>-${{Number(p.stop_loss_pct||30)}}%</td><td>${{fmtSmartSl(p.smart_stop_profit_pct)}}</td>
    <td>+${{Number(p.peak_profit_pct||0).toFixed(1)}}%</td></tr>`
  ).join('') : '<tr><td colspan="8" class="muted">No open positions</td></tr>';

  document.getElementById('histBody').innerHTML = (s.history && s.history.length) ? s.history.map(h => `
    <tr><td><b>${{esc(h.symbol||'?')}}</b></td><td><code>${{esc(h.mint||'')}}</code></td>
    <td>${{Number(h.entry_price_usd||0).toFixed(8)}}</td><td>${{Number(h.exit_price_usd||0).toFixed(8)}}</td>
    <td><b class="${{(h.pnl_pct||0)>0?'pos':((h.pnl_pct||0)<0?'neg':'')}}">${{fmtPnl(h.pnl_pct)}}</b></td><td>${{esc(h.close_reason||'—')}}</td><td><code>${{esc(h.sell_signature||'')}}</code></td></tr>`
  ).join('') : '<tr><td colspan="7" class="muted">No closed trades yet</td></tr>';

  document.getElementById('t').textContent = new Date().toLocaleTimeString();
}}
refresh(render);
setInterval(() => refresh(render), 2000);
</script>
</main></body></html>""".encode("utf-8")


def _page_logs() -> bytes:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Solana Bot — Logs</title>
<style>{_STYLE}</style></head><body><main>

<div class="brand">
  <h1>Solana Trading Bot</h1>
  <div class="livestat"><span class="pulse"></span>updated <span id="t">just now</span></div>
</div>
{_nav('logs')}

<h2>Live logs <span class="small" id="logsMeta"></span></h2>
<div class="small">Same messages as the server/Railway logs, streamed here so you don't need to leave the dashboard. This page is system/error output only — token/candidate activity is on the <a href="/tokens" style="color:var(--ink)">Tokens</a> page.</div><br>
<div class="tablewrap" id="logsWrap"><table><thead><tr><th style="width:90px">Time</th><th style="width:70px">Level</th><th style="width:110px">Source</th><th>Message</th></tr></thead><tbody id="logsBody"><tr><td colspan="4" class="muted">No log records yet.</td></tr></tbody></table></div>


<script>
{_JS_COMMON}
function render(s){{
  const logs = s.logs || [];
  document.getElementById('logsMeta').textContent = logs.length ? `— ${{logs.length}} recent entries` : '';
  document.getElementById('logsBody').innerHTML = logs.length ? logs.map(l => `
    <tr><td>${{fmtClock(l.time)}}</td><td class="lvl-${{esc(l.level)}}">${{esc(l.level)}}</td>
    <td>${{esc(l.logger)}}</td><td class="logmsg">${{esc(l.message)}}</td></tr>`
  ).join('') : '<tr><td colspan="4" class="muted">No log records yet.</td></tr>';
  document.getElementById('t').textContent = new Date().toLocaleTimeString();
}}
refresh(render);
setInterval(() => refresh(render), 2000);
</script>
</main></body></html>""".encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return
    def _send(self, code, body, content_type="text/html; charset=utf-8"):
        self.send_response(code); self.send_header("Content-Type", content_type); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._send(200, b"ok", "text/plain; charset=utf-8"); return
        if not _auth_ok(self.headers.get("Authorization")):
            self.send_response(401); self.send_header("WWW-Authenticate", 'Basic realm="Solana Bot Dashboard"'); self.end_headers(); return
        if path == "/api/status":
            self._send(200, json.dumps(_status()).encode(), "application/json; charset=utf-8"); return
        if path == "/logs":
            self._send(200, _page_logs()); return
        if path in ("/", "/dashboard", "/tokens"):
            self._send(200, _page_tokens()); return
        self._send(404, b"not found", "text/plain; charset=utf-8")


def serve():
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Dashboard listening on 0.0.0.0:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    serve()
