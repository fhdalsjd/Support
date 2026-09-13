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
    }


_STYLE = """
body{margin:0;background:#0b0d12;color:#e9edf5;font:15px system-ui,-apple-system,sans-serif}
main{max-width:1400px;margin:auto;padding:24px}
h1{margin:0 0 6px}h2{margin-top:28px;display:flex;align-items:center;gap:10px}
.muted{color:#8993a5}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:20px 0}
.card{background:#141821;border:1px solid #252c3a;border-radius:14px;padding:16px}
.value{font-size:22px;font-weight:700;margin-top:8px}
.ok{color:#52d273}
table{width:100%;border-collapse:collapse;background:#141821;border-radius:14px;overflow:hidden}
th,td{padding:11px;border-bottom:1px solid #252c3a;text-align:left;font-size:12px;vertical-align:top}
th{color:#9da7b8;position:sticky;top:0;background:#141821}
code{font-size:10px;word-break:break-all}
.note{background:#17130a;border:1px solid #4d3b13;padding:14px;border-radius:12px;margin-top:18px}
.small{font-size:12px;color:#8993a5}
.tablewrap{max-height:420px;overflow-y:auto;border-radius:14px}
.badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11px;font-weight:700}
.badge-bought{background:#123a21;color:#52d273}
.badge-skipped{background:#241f0a;color:#e0b84d}
.badge-error{background:#3a1414;color:#ef6a6a}
.badge-buyfailed{background:#3a1414;color:#ef6a6a}
.lvl-INFO{color:#8993a5}
.lvl-WARNING{color:#e0b84d}
.lvl-ERROR{color:#ef6a6a}
.lvl-CRITICAL{color:#ef6a6a;font-weight:700}
.logmsg{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11.5px;white-space:pre-wrap;word-break:break-word}
.pulse{display:inline-block;width:9px;height:9px;border-radius:50%;background:#52d273;margin-right:6px;animation:pulse 1.4s infinite}
@keyframes pulse{0%{opacity:1}50%{opacity:.25}100%{opacity:1}}
nav.tabs{margin:14px 0 22px;display:flex;gap:8px}
nav.tabs a{padding:8px 16px;border-radius:10px;text-decoration:none;font-weight:700;font-size:13px;color:#8993a5;background:#141821;border:1px solid #252c3a}
nav.tabs a.active{color:#e9edf5;border-color:#3a4356;background:#1a2029}
.checklist{display:flex;flex-direction:column;gap:3px;min-width:170px}
.chk{display:block;font-size:10.5px;padding:2px 6px;border-radius:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chk-pass{background:#123a21;color:#52d273}
.chk-fail{background:#3a1414;color:#ef6a6a}
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

<h1>Solana Trading Bot</h1>
<div class="muted"><span class="pulse" id="livedot"></span>Live · updates automatically, no page reload · last update <span id="t">just now</span></div>
{_nav('tokens')}
<div id="demoBanner"></div>

<div class="grid" id="statCards"></div>

<h2>🎯 Auto-Sniper activity <span class="small" id="sniperMeta"></span></h2>
<div class="small">Every token the sniper looks at is listed below with the exact reason it passed or was skipped — updated live. System/error messages are on the <a href="/logs" style="color:#e9edf5">Logs</a> page.</div><br>
<div class="tablewrap"><table><thead><tr><th>Time</th><th>Token</th><th>Mint</th><th>Result</th><th>Reason</th><th>Price</th><th>Liquidity</th><th>MC</th><th>5m Vol</th><th>5m Δ</th><th>Age</th><th>Risk</th><th>RugCheck</th><th>Checks</th></tr></thead><tbody id="activityBody"><tr><td colspan="14" class="muted">Waiting for first scan…</td></tr></tbody></table></div>

<h2>🟢 Open positions</h2><div class="small">No fixed TP. Positions use -30% hard protection, +5% break-even, then a ratcheting profit lock at 50% of peak profit.</div><br>
<table><thead><tr><th>Token</th><th>CA / Mint</th><th>Entry</th><th>Amount</th><th>TP</th><th>Hard SL</th><th>Smart SL</th><th>Peak PnL</th></tr></thead><tbody id="posBody"><tr><td colspan="8" class="muted">Loading…</td></tr></tbody></table>

<h2>📚 Closed trade history</h2>
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
    ? '<div class="note" style="background:#0a1f14;border-color:#1f4d2f"><b>🧪 DEMO MODE</b> — balances and trades below are simulated. No real funds, no real transactions.</div>' : '';

  const walletText = s.wallet_connected ? 'Connected' : 'Not connected';
  const walletAddr = shortAddr(s.wallet_address);
  const sniperOn = s.auto_sniper_enabled ? 'ON' : 'OFF';
  const allocation = (s.auto_sniper_allocation_pct!=null) ? (s.auto_sniper_allocation_pct+'%') : '—';
  document.getElementById('statCards').innerHTML = `
    <div class="card"><div class="muted">Service</div><div class="value ok">ONLINE</div></div>
    <div class="card"><div class="muted">Bot</div><div class="value">${{s.bot_loaded?'RUNNING':'OFFLINE'}}</div></div>
    <div class="card"><div class="muted">Wallet</div><div class="value">${{walletText}}</div><div class="small">${{esc(walletAddr)}}</div></div>
    <div class="card"><div class="muted">Auto-Sniper</div><div class="value">${{sniperOn}}</div><div class="small">Allocation: ${{esc(allocation)}}</div></div>
    <div class="card"><div class="muted">Open positions</div><div class="value">${{s.positions.length}}</div></div>
    <div class="card"><div class="muted">Closed history</div><div class="value">${{s.history.length}}</div></div>`;

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
    <td><b>${{fmtPnl(h.pnl_pct)}}</b></td><td>${{esc(h.close_reason||'—')}}</td><td><code>${{esc(h.sell_signature||'')}}</code></td></tr>`
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

<h1>Solana Trading Bot</h1>
<div class="muted"><span class="pulse" id="livedot"></span>Live · updates automatically, no page reload · last update <span id="t">just now</span></div>
{_nav('logs')}

<h2>📜 Live logs <span class="small" id="logsMeta"></span></h2>
<div class="small">Same messages as the server/Railway logs, streamed here so you don't need to leave the dashboard. This page is system/error output only — token/candidate activity is on the <a href="/tokens" style="color:#e9edf5">Tokens</a> page.</div><br>
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
