# Solana Memecoin Trading Telegram Bot

Manual + automated (TP/SL/trailing-SL) trading bot for Solana memecoins,
controlled entirely through Telegram inline buttons. Swaps route through
Jupiter's aggregator (covers Raydium + Pump.fun-graduated pools), with
Jito tip integration to reduce sandwich risk.

## Architecture

```
config.py     — loads/validates .env
wallet.py     — keypair derivation (mnemonic or private key), balances, signing
trading.py    — Jupiter quote/swap, Jito tip submission
security.py   — DexScreener market data + RugCheck mint/freeze/holder check
state.py      — JSON-backed positions & settings store
bot.py        — Telegram handlers, inline keyboards, TP/SL background job
```

## ⚠️ About connecting your Phantom wallet

There is no way to "connect" Phantom itself to a server bot the way a
dApp connects in-browser (that flow relies on Phantom's browser extension
approving each transaction interactively). For a bot that trades
autonomously while you're away, the bot needs to hold real signing
authority — that means importing either:

- Your **12/24-word recovery phrase**, or
- The **raw private key** (Phantom: Settings → Export Private Key → base58 string)

**Do this only with a fresh, dedicated wallet you create specifically for
the bot** — not your main Phantom wallet:

1. In Phantom, create a **new wallet** (Add/Connect Wallet → Create New Wallet).
2. Fund *that* wallet only with what you're prepared to lose.
3. Export its private key or seed phrase and put it in `.env` — never in
   code, never in a git commit, never in a Railway-linked public repo.
4. Keep your main holdings in a separate wallet Phantom never exposes to
   this bot.

If the VPS/Railway project is ever compromised, only the burner wallet's
funds are at risk.

## Local Setup

```bash
git clone <your-private-repo>
cd solana-memebot
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # fill in TELEGRAM_BOT_TOKEN, ADMIN_IDS, wallet creds
python bot.py
```

Get `ADMIN_IDS` from @userinfobot on Telegram (your numeric user ID — the
bot silently ignores everyone else). Get `TELEGRAM_BOT_TOKEN` from
@BotFather.

**Use a paid RPC** (Helius, QuickNode, Triton) — the public
`api.mainnet-beta.solana.com` endpoint rate-limits hard and will cause
missed snipes / failed TP-SL checks under real trading load.

## Deploying on Railway

1. Push this project to a **private** GitHub repo (never public — it
   contains the loading logic for your wallet secrets even though the
   secrets themselves stay in env vars).
2. On [railway.com](https://railway.com): **New Project → Deploy from GitHub repo**.
3. Once created, go to your service → **Variables** tab and add every key
   from `.env.example` (TELEGRAM_BOT_TOKEN, ADMIN_IDS, WALLET_MNEMONIC or
   WALLET_PRIVATE_KEY_B58, SOLANA_RPC_URL, etc). Do **not** commit a real
   `.env` file to the repo — `.env.example` is a template only.
4. Railway auto-detects Python; if it doesn't pick up a start command, add
   a `Procfile` or set the service's **Start Command** to:
   ```
   python bot.py
   ```
5. Deploy. Check the **Deployments → Logs** tab — you should see
   `Bot starting — whitelisted admins: {...}`.
6. Message your bot's `/start` command on Telegram from your whitelisted
   account.

### Keeping it running

Railway will restart the service on crash by default. Since `state.json`
is written to local disk, note that **Railway's filesystem is ephemeral on
redeploys** — if you care about position history surviving redeploys,
either mount a Railway volume or switch `state.py` to a small hosted
Postgres/SQLite-on-volume instead of the flat JSON file.

## What's intentionally simplified (and where to extend)

- **Jito bundles**: `trading.py` sends a standalone tip transaction
  alongside the swap rather than a true atomic bundle. For guaranteed
  atomic multi-tx bundling, integrate `jito-searcher-client` or POST
  directly to your block engine's `/api/v1/bundles` endpoint.
- **Token decimals**: sells assume a configurable `decimals` field on the
  stored `Position` — fetch the real mint decimals via `get_account_info`
  when a position is opened rather than assuming 9.
- **Entry price precision**: entry price is captured from the last-quoted
  market price, not the swap's actual fill price — for exact PnL, parse
  `out_amount` from the Jupiter quote used in the executed swap.
- **Auto-sniper feed**: the `[Toggle Auto-Sniper]` button flips a state
  flag; you'll need to wire in a source of new-token events (e.g. a
  Pump.fun WebSocket feed or a Helius webhook) that calls `buy_token()`
  when `auto_sniper_enabled` is true and your filter criteria match.
- **Trailing SL input capture**: the callback stub prompts for a percent
  but doesn't yet capture the reply — mirror the `_awaiting_custom_amount`
  pattern in `bot.py` to wire it up fully.

## Standing disclaimer

Memecoin trading — manual or automated — is high-risk and most tokens on
Pump.fun/new Raydium pools lose most or all of their value. Nothing here
is financial advice, and the `MAX_BUY_SOL` env var is a blunt safety
backstop, not a substitute for position sizing you're comfortable with.
