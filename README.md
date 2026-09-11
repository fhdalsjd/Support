# Solana Memecoin Telegram Trader

Private Telegram-controlled Solana trading bot for Pump.fun/PumpSwap/Raydium routes, token inspection, wallet signing, Jito low-latency submission, position storage and Railway deployment.

## Files
- `bot.py` — Telegram dashboard, ADMIN_ID whitelist, mint ingestion and trade buttons.
- `wallet.py` — Base58/BIP39 wallet loading, burner generation, balance and transaction signing.
- `security.py` — DexScreener metrics + RugCheck checks.
- `trading.py` — PumpPortal Local Transaction API for Pump.fun/Raydium routes and Jito submission.
- `store.py` — SQLite position state.
- `settings.py` — environment configuration.

## Security
Only `ADMIN_ID` is allowed to use the bot. Other users are silently ignored. Never commit `.env`, mnemonic phrases or private keys. Railway Variables should be used for secrets.

`LIVE_TRADING=false` is the default. Enable it only after testing with a dedicated wallet and small amount.

## Railway
1. Deploy this GitHub repository as a Railway service.
2. Railway will build the included Dockerfile.
3. Add variables from `.env.example` in Railway Variables.
4. Set `BOT_TOKEN`, `ADMIN_ID`, and exactly one wallet source: `WALLET_PRIVATE_KEY` or `WALLET_MNEMONIC`.
5. Keep `LIVE_TRADING=false` for the first smoke test.
6. After `/start` and mint inspection work, fund a dedicated burner wallet and enable live trading only if you accept the risk.

## Execution
PumpPortal's Local Transaction API can build transactions for `pump`, `raydium`, `pump-amm`, `launchlab`, `raydium-cpmm`, `bonk`, or `auto`. The bot signs locally so the private key is not sent to the trade-builder service. When `JITO_ENABLED=true`, the signed transaction is submitted to the Jito Block Engine.

Jito acceptance is not the same as final confirmation; network/leader conditions still determine landing.
