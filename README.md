# Solana Memecoin Trading Telegram Bot

Manual + automated (TP/SL/trailing-SL/smart ratcheting stop) trading bot
for Solana memecoins, controlled through Telegram inline buttons. Swaps
route through Jupiter's aggregator (covers Raydium + Pump.fun-graduated
pools), with Jito tip integration to reduce sandwich risk, plus an
auto-sniper and a small web dashboard.

## Architecture

```
config.py         — loads/validates .env
wallet.py          — keypair derivation (mnemonic or private key), balances, signing
trading.py         — Jupiter quote/swap, Jito tip submission
security.py        — DexScreener market data + on-chain mint/freeze check + RugCheck
market_snapshot.py — renders a price-chart image attached to Telegram messages
state.py           — JSON-backed positions & settings store
trading_bot.py     — Telegram handlers, inline keyboards
sniper.py          — auto-sniper background logic
smart_sl.py        — ratcheting stop-loss background logic
dashboard.py        — small authenticated web dashboard (balance/positions/history)
sitecustomize.py    — live-pricing hardening patch (see "Why this file matters" below)
launcher.py         — actual process entrypoint: runs trading_bot.py + sniper.py + smart_sl.py + dashboard.py together
```

Run with `python launcher.py` — **not** `python trading_bot.py` directly,
since the sniper, smart-SL daemon, dashboard, and the live-pricing patch
below are only wired up by `launcher.py`.

## Fix: "real data not showing when I paste a token"

If pasting a mint address showed `Unknown` / all-zero market cap, price,
and liquidity — especially for a **brand-new pump.fun token** — this was
the cause, now fixed:

`security.py`'s base `get_token_overview()` only reads DexScreener. For a
token that's minutes old, DexScreener often hasn't indexed a pool for it
yet, so it correctly reports "not found" — but that's precisely the token
class a sniper bot pastes/buys most. `sitecustomize.py` adds a fallback
chain (pump.fun's own API → GeckoTerminal → Jupiter price → direct Solana
RPC reads) for exactly this case, plus fixes for stuck/stale Telegram
refresh callbacks. It does this by monkeypatching
`security.get_token_overview` at process startup.

That patch was never actually taking effect, for two stacked reasons:

1. **`sitecustomize.py` was never being loaded at all.** Python's `site`
   module only auto-imports a `sitecustomize.py` sitting next to your
   entry script if that directory is already on `sys.path` *before*
   interpreter startup finishes — which, without `PYTHONPATH` set, it
   isn't. Running `python launcher.py` plainly (or via the Dockerfile,
   which doesn't set `PYTHONPATH`) never triggered it.
2. Even if it somehow had loaded, `trading_bot.py` and `smart_sl.py`
   imported the function by name (`from security import
   get_token_overview`). That freezes a reference to whatever function
   object existed at import time — a later `security.get_token_overview
   = live_overview` reassignment is invisible through that frozen name.

**Fix applied:** `launcher.py` now loads `sitecustomize.py` by explicit
file path at the very top of the file — before anything else is
imported — which sidesteps both the sys.path timing issue and the
possibility of a same-named system `sitecustomize.py` (Debian-based
images, including `python:3.11-slim-bookworm`, ship their own) silently
shadowing it. `trading_bot.py` and `smart_sl.py` now do `import security`
and call `security.get_token_overview(...)`, so the patch is honored
regardless of import order going forward.

If you still see missing data after this fix, check (in order): the bot's
logs for `Loaded project sitecustomize.py explicitly` at startup (confirms
the patch applied), then whether `SOLANA_RPC_URL` is a paid endpoint (the
public one frequently rate-limits or blocks the RPC calls this fallback
needs), then whether the specific mint is simply too new for even
pump.fun's own API to have indexed yet (retry after a few seconds).

## About connecting your Phantom wallet

There's no way to "connect" the Phantom extension itself to a server bot
the way a dApp connects in-browser — that flow needs Phantom's UI to
approve each transaction. A bot trading autonomously needs real signing
authority, meaning you import either your recovery phrase or raw private
key.

**Use a fresh, dedicated wallet created specifically for this bot** — not
your main one:

1. In Phantom: Add/Connect Wallet → Create New Wallet.
2. Fund *that* wallet only with what you're prepared to lose.
3. Either put its mnemonic/private key in `.env` (never committed, never
   hardcoded), or use `/connect_wallet` in Telegram to paste it directly
   into the chat — the bot reads and discards the message immediately
   rather than storing it in an env var.
4. Keep your main holdings in a separate wallet Phantom never exposes to
   this bot.

If the VPS/Railway project is ever compromised, only the burner wallet's
funds are at risk.

## Local setup

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # fill in TELEGRAM_BOT_TOKEN, ADMIN_IDS, wallet creds
python launcher.py
```

Get `ADMIN_IDS` from @userinfobot (your numeric Telegram user ID — the bot
silently ignores everyone else). Get `TELEGRAM_BOT_TOKEN` from @BotFather.

**Use a paid RPC** (Helius, QuickNode, Triton) for `SOLANA_RPC_URL` — the
public `api.mainnet-beta.solana.com` endpoint rate-limits hard and will
cause missed snipes, failed TP/SL checks, and degraded on-chain data
under real trading load.

## Deploying on Railway

1. Push to a **private** GitHub repo.
2. Railway → **New Project → Deploy from GitHub repo**.
3. Add every variable from `.env.example` under **Variables**. Never
   commit a real `.env`.
4. Deploy, check **Deployments → Logs** for `Loaded project
   sitecustomize.py explicitly` and `Bot starting — whitelisted admins:
   {...}`.
5. Message `/start` from your whitelisted account.

Railway's filesystem is ephemeral on redeploys — `state.json` (positions,
settings) resets unless you mount a volume at the app's working directory.

## What's intentionally simplified (and where to extend)

- **Jito bundles**: `trading.py` sends a standalone tip transaction
  alongside the swap rather than a true atomic bundle. For guaranteed
  atomic multi-tx bundling, integrate `jito-searcher-client` or POST
  directly to your block engine's `/api/v1/bundles` endpoint.
- **Auto-sniper feed**: `sniper.py` polls for candidates on an interval
  (`AUTO_SNIPER_POLL_SECONDS`). For lower-latency detection, wire in a
  Pump.fun WebSocket feed or a Helius webhook instead of polling.

## Standing disclaimer

Memecoin trading — manual or automated — is high-risk, and most tokens on
Pump.fun/new Raydium pools lose most or all of their value. Nothing here
is financial advice, and `MAX_BUY_SOL` / the auto-sniper thresholds are
blunt safety backstops, not a substitute for position sizing you're
comfortable with.
