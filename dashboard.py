"""Live read-only web dashboard for the Telegram trading bot."""
from __future__ import annotations

import base64
import hmac
import html
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

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


def _smart_sl_text(value):
    if value is None:
        return "—"
    if value == 0:
        return "BE"
    return f"+{value:.1f}%"


def _status() -> dict:
    state = _read_state()
    positions = state.get("positions", {})
    history = state.get("history", [])
    wallet = getattr(BOT, "wallet", None)
    settings = getattr(BOT, "settings", None)
    wallet_connected = bool(wallet and wallet.configured)
    address = None
    if wallet_connected:
        try:
            address = str(wallet.pubkey)
        except Exception:
            pass
    return {
        "service": "online", "bot_loaded": bool(BOT), "wallet_connected": wallet_connected,
        "wallet_address": address, "auto_sniper_enabled": bool(state.get("auto_sniper_enabled", False)),
        "auto_sniper_allocation_pct": state.get("auto_sniper_allocation_pct"),
        "slippage_bps": state.get("slippage_bps", getattr(settings, "default_slippage_bps", 500)),
        "positions": [{"mint": m, **p} for m, p in positions.items()],
        "history": history[:50], "uptime_seconds": int(time.time() - STARTED_AT),
    }


def _fmt_pnl(value):
    if value is None:
        return "—"
    return f"{value:+.2f}%"


def _page() -> bytes:
    s = _status()
    wallet_text = "Connected" if s["wallet_connected"] else "Not connected"
    wallet_addr = s["wallet_address"] or "—"
    if wallet_addr != "—":
        wallet_addr = wallet_addr[:6] + "…" + wallet_addr[-6:]
    sniper = "ON" if s["auto_sniper_enabled"] else "OFF"
    allocation = f"{s['auto_sniper_allocation_pct']:g}%" if s["auto_sniper_allocation_pct"] is not None else "—"
    pos_rows = "".join(
        f"<tr><td><b>${html.escape(str(p.get('symbol','?')))}</b></td><td><code>{html.escape(str(p.get('mint','')))}</code></td>"
        f"<td>${float(p.get('entry_price_usd',0)):.8f}</td><td>{float(p.get('amount_tokens',0)):.6f}</td>"
        f"<td>Disabled</td><td>-{float(p.get('stop_loss_pct',30) or 30):g}%</td>"
        f"<td>{_smart_sl_text(p.get('smart_stop_profit_pct'))}</td><td>+{float(p.get('peak_profit_pct',0)):.1f}%</td></tr>"
        for p in s["positions"]
    ) or '<tr><td colspan="8" class="muted">No open positions</td></tr>'
    hist_rows = "".join(
        f"<tr><td><b>${html.escape(str(h.get('symbol','?')))}</b></td><td><code>{html.escape(str(h.get('mint','')))}</code></td>"
        f"<td>${float(h.get('entry_price_usd',0)):.8f}</td><td>${float(h.get('exit_price_usd',0) or 0):.8f}</td>"
        f"<td><b>{_fmt_pnl(h.get('pnl_pct'))}</b></td><td>{html.escape(str(h.get('close_reason','—')))}</td>"
        f"<td><code>{html.escape(str(h.get('sell_signature','')))}</code></td></tr>"
        for h in s["history"]
    ) or '<tr><td colspan="7" class="muted">No closed trades yet</td></tr>'
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Solana Bot Live Dashboard</title>
<style>
body{{margin:0;background:#0b0d12;color:#e9edf5;font:15px system-ui,-apple-system,sans-serif}}main{{max-width:1400px;margin:auto;padding:24px}}h1{{margin:0 0 6px}}h2{{margin-top:28px}}.muted{{color:#8993a5}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:20px 0}}.card{{background:#141821;border:1px solid #252c3a;border-radius:14px;padding:16px}}.value{{font-size:22px;font-weight:700;margin-top:8px}}.ok{{color:#52d273}}table{{width:100%;border-collapse:collapse;background:#141821;border-radius:14px;overflow:hidden}}th,td{{padding:11px;border-bottom:1px solid #252c3a;text-align:left;font-size:12px;vertical-align:top}}th{{color:#9da7b8}}code{{font-size:10px;word-break:break-all}}.note{{background:#17130a;border:1px solid #4d3b13;padding:14px;border-radius:12px;margin-top:18px}}.small{{font-size:12px;color:#8993a5}}
</style></head><body><main>
<h1>Solana Trading Bot</h1><div class="muted">Live Railway service dashboard · refreshes every 5 seconds</div>
<div class="grid"><div class="card"><div class="muted">Service</div><div class="value ok">ONLINE</div></div><div class="card"><div class="muted">Bot</div><div class="value">{"RUNNING" if s["bot_loaded"] else "OFFLINE"}</div></div><div class="card"><div class="muted">Wallet</div><div class="value">{wallet_text}</div><div class="small">{html.escape(wallet_addr)}</div></div><div class="card"><div class="muted">Auto-Sniper</div><div class="value">{sniper}</div><div class="small">Allocation: {allocation}</div></div><div class="card"><div class="muted">Open positions</div><div class="value">{len(s["positions"])}</div></div><div class="card"><div class="muted">Closed history</div><div class="value">{len(s["history"])}</div></div></div>
<h2>🟢 Open positions</h2><div class="small">No fixed TP. Positions use -30% hard protection, +5% break-even, then a ratcheting profit lock at 50% of peak profit.</div><br><table><thead><tr><th>Token</th><th>CA / Mint</th><th>Entry</th><th>Amount</th><th>TP</th><th>Hard SL</th><th>Smart SL</th><th>Peak PnL</th></tr></thead><tbody>{pos_rows}</tbody></table>
<h2>📚 Closed trade history</h2><table><thead><tr><th>Token</th><th>CA / Mint</th><th>Entry</th><th>Exit</th><th>PnL</th><th>Reason</th><th>Sell Tx</th></tr></thead><tbody>{hist_rows}</tbody></table>
<div class="note"><b>Trade journal</b><br>Each completed trade keeps its entry snapshot, contract address, market/security data, entry and exit prices, close reason, peak/Smart-SL state, and buy/sell transaction signatures. The history is capped at 500 records and stored atomically in the bot state file.</div>
<p class="small">No private key or recovery phrase is displayed. Last refresh: <span id="t"></span></p><script>document.getElementById('t').textContent=new Date().toLocaleTimeString();setTimeout(()=>location.reload(),5000);</script>
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
        if path in ("/", "/dashboard"):
            self._send(200, _page()); return
        self._send(404, b"not found", "text/plain; charset=utf-8")


def serve():
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Dashboard listening on 0.0.0.0:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    serve()
