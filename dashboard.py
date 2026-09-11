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


def _status() -> dict:
    state = _read_state()
    positions = state.get("positions", {})
    wallet = getattr(BOT, "wallet", None)
    settings = getattr(BOT, "settings", None)
    bot_alive = bool(BOT)
    wallet_connected = bool(wallet and wallet.configured)
    address = None
    if wallet_connected:
        try:
            address = str(wallet.pubkey)
        except Exception:
            address = None

    return {
        "service": "online",
        "bot_loaded": bot_alive,
        "wallet_connected": wallet_connected,
        "wallet_address": address,
        "auto_sniper_enabled": bool(state.get("auto_sniper_enabled", False)),
        "slippage_bps": state.get("slippage_bps", getattr(settings, "default_slippage_bps", 500)),
        "positions": [
            {
                "mint": mint,
                "symbol": p.get("symbol", "?"),
                "entry_price_usd": p.get("entry_price_usd", 0),
                "amount_tokens": p.get("amount_tokens", 0),
                "tp": p.get("take_profit_pct"),
                "sl": p.get("stop_loss_pct"),
                "smart_sl": p.get("smart_stop_profit_pct"),
                "peak_profit": p.get("peak_profit_pct", 0),
            }
            for mint, p in positions.items()
        ],
        "uptime_seconds": int(time.time() - STARTED_AT),
        "auto_trading_note": (
            "Auto-sniper is wired to live DEX Screener discovery. When the Telegram Sniper toggle is ON, "
            "the bot checks new Solana profiles, liquidity, market cap, 5m volume/momentum, pool age, RugCheck, "
            "and a Jupiter route/price-impact gate before an automatic buy."
        ),
        "smart_sl_note": (
            "Smart SL keeps the initial downside stop until the trade reaches +5%, then moves to break-even. "
            "At +10% it locks 50% of peak profit and ratchets upward in 2% peak-profit steps."
        ),
    }


def _page() -> bytes:
    s = _status()
    wallet_text = "Connected" if s["wallet_connected"] else "Not connected"
    wallet_addr = s["wallet_address"] or "—"
    if wallet_addr and wallet_addr != "—":
        wallet_addr = wallet_addr[:6] + "…" + wallet_addr[-6:]
    sniper = "ON" if s["auto_sniper_enabled"] else "OFF"
    pos_rows = "".join(
        f"<tr><td><b>${html.escape(str(p['symbol']))}</b></td><td><code>{html.escape(p['mint'])}</code></td>"
        f"<td>${p['entry_price_usd']:.8f}</td><td>{p['amount_tokens']:.6f}</td>"
        f"<td>+{p['tp']}%</td><td>-{p['sl']}%</td>"
        f"<td>{('BE' if p['smart_sl'] == 0 else (f'+{p[\"smart_sl\"]:.1f}%' if p['smart_sl'] is not None else '—'))}</td>"
        f"<td>+{p['peak_profit']:.1f}%</td></tr>"
        for p in s["positions"]
    ) or '<tr><td colspan="8" class="muted">No open positions</td></tr>'

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Solana Bot Live Dashboard</title>
<style>
body{{margin:0;background:#0b0d12;color:#e9edf5;font:15px system-ui,-apple-system,sans-serif}}
main{{max-width:1200px;margin:auto;padding:24px}} h1{{margin:0 0 6px}} .muted{{color:#8993a5}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin:20px 0}}
.card{{background:#141821;border:1px solid #252c3a;border-radius:14px;padding:16px}} .value{{font-size:22px;font-weight:700;margin-top:8px}}
.ok{{color:#52d273}} .warn{{color:#ffca5c}} .bad{{color:#ff6b6b}}
table{{width:100%;border-collapse:collapse;background:#141821;border-radius:14px;overflow:hidden}}th,td{{padding:12px;border-bottom:1px solid #252c3a;text-align:left;font-size:13px}} th{{color:#9da7b8}}
code{{font-size:11px}} .note{{background:#17130a;border:1px solid #4d3b13;padding:14px;border-radius:12px;margin-top:18px}}
.small{{font-size:12px;color:#8993a5}}
</style></head><body><main>
<h1>Solana Trading Bot</h1><div class="muted">Live Railway service dashboard · refreshes every 5 seconds</div>
<div class="grid">
<div class="card"><div class="muted">Service</div><div class="value ok">ONLINE</div></div>
<div class="card"><div class="muted">Bot process</div><div class="value">{"RUNNING" if s["bot_loaded"] else "OFFLINE"}</div></div>
<div class="card"><div class="muted">Wallet</div><div class="value">{wallet_text}</div><div class="small">{html.escape(wallet_addr)}</div></div>
<div class="card"><div class="muted">Auto-sniper</div><div class="value">{sniper}</div><div class="small">live discovery + guarded entry</div></div>
<div class="card"><div class="muted">Open positions</div><div class="value">{len(s["positions"])}</div></div>
<div class="card"><div class="muted">Uptime</div><div class="value">{s["uptime_seconds"]}s</div></div>
</div>
<h2>Open positions</h2>
<table><thead><tr><th>Token</th><th>Mint</th><th>Entry</th><th>Amount</th><th>TP</th><th>Hard SL</th><th>Smart SL</th><th>Peak PnL</th></tr></thead><tbody>{pos_rows}</tbody></table>
<div class="note"><b>Smart stop-loss</b><br>{html.escape(s["smart_sl_note"])}<br><br>
<b>Auto-trading status</b><br>{html.escape(s["auto_trading_note"])}<br><br>
Safety gates are deliberately conservative. The Sniper toggle remains <b>OFF</b> until you enable it in Telegram; when ON, it can spend real SOL up to the configured AUTO_SNIPER_BUY_SOL amount.</div>
<p class="small">No private key or recovery phrase is displayed. Last refresh: <span id="t"></span></p>
<script>document.getElementById('t').textContent=new Date().toLocaleTimeString();setTimeout(()=>location.reload(),5000);</script>
</main></body></html>""".encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _send(self, code: int, body: bytes, content_type="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._send(200, b"ok", "text/plain; charset=utf-8")
            return
        if not _auth_ok(self.headers.get("Authorization")):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Solana Bot Dashboard"')
            self.end_headers()
            return
        if path == "/api/status":
            self._send(200, json.dumps(_status()).encode(), "application/json; charset=utf-8")
            return
        if path in ("/", "/dashboard"):
            self._send(200, _page())
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")


def serve():
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Dashboard listening on 0.0.0.0:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    serve()
