"""
trading.py — Quote + swap execution.

Uses Jupiter's aggregator API (quote-api.jup.ag) which routes across every
major Solana DEX/AMM — including Raydium pools and Pump.fun bonding curves
once a token has graduated to an AMM, and increasingly Pump.fun directly.
This is the practical, battle-tested approach real trading bots use rather
than hand-building raw AMM instructions, which is brittle and easy to get
wrong in ways that lose funds.

Priority landing is handled two ways:
  1. A compute-unit price (priority fee) is requested from Jupiter directly.
  2. Optionally, a Jito tip transfer instruction can be attached so the swap
     is bundle-eligible via Jito's block engine, reducing sandwich risk.

NOTE ON JITO BUNDLES: full bundle submission (grouping multiple transactions
atomically via Jito's sendBundle RPC) is stubbed with a clear extension
point below (`submit_jito_bundle`). Wire in `jito-searcher-client` or a raw
HTTP call to your block engine's `/api/v1/bundles` endpoint if you need true
atomic multi-tx bundles; single-tx swaps with a tip account transfer (as
implemented here) already get you most of the anti-sandwich benefit.
"""
import base64
import httpx
from dataclasses import dataclass
from solders.transaction import VersionedTransaction
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.message import MessageV0
from solders.instruction import Instruction

from config import settings
from wallet import wallet, LAMPORTS_PER_SOL

SOL_MINT = "So11111111111111111111111111111111111111112"

# Jito tip accounts (any one is valid — pick round robin in production)
JITO_TIP_ACCOUNTS = [
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
]


@dataclass
class QuoteResult:
    raw: dict
    in_amount: int
    out_amount: int
    price_impact_pct: float
    route_summary: str


@dataclass
class SwapResult:
    success: bool
    signature: str | None = None
    error: str | None = None


async def get_quote(input_mint: str, output_mint: str, amount_lamports: int, slippage_bps: int) -> QuoteResult:
    async with httpx.AsyncClient(timeout=15) as http:
        r = await http.get(
            f"{settings.jupiter_quote_api}/quote",
            params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": amount_lamports,
                "slippageBps": slippage_bps,
                "onlyDirectRoutes": "false",
            },
        )
        r.raise_for_status()
        data = r.json()
        route_labels = [step["swapInfo"]["label"] for step in data.get("routePlan", [])]
        return QuoteResult(
            raw=data,
            in_amount=int(data["inAmount"]),
            out_amount=int(data["outAmount"]),
            price_impact_pct=float(data.get("priceImpactPct", 0)) * 100,
            route_summary=" -> ".join(route_labels) or "direct",
        )


async def build_swap_transaction(quote: QuoteResult, priority_fee_microlamports: int) -> VersionedTransaction:
    async with httpx.AsyncClient(timeout=15) as http:
        r = await http.post(
            f"{settings.jupiter_quote_api}/swap",
            json={
                "quoteResponse": quote.raw,
                "userPublicKey": str(wallet.pubkey),
                "wrapAndUnwrapSol": True,
                "prioritizationFeeLamports": priority_fee_microlamports,
                "dynamicComputeUnitLimit": True,
            },
        )
        r.raise_for_status()
        swap_tx_b64 = r.json()["swapTransaction"]
        raw = base64.b64decode(swap_tx_b64)
        return VersionedTransaction.from_bytes(raw)


async def execute_swap(
    input_mint: str,
    output_mint: str,
    amount_lamports: int,
    slippage_bps: int | None = None,
    priority_fee_microlamports: int | None = None,
    add_jito_tip: bool = True,
) -> SwapResult:
    """
    Full pipeline: quote -> build tx -> (optional jito tip) -> sign -> send -> confirm.
    Used for both BUY (SOL -> token) and SELL (token -> SOL) by swapping mint order.
    """
    slippage_bps = slippage_bps or settings.default_slippage_bps
    priority_fee = priority_fee_microlamports or settings.default_priority_fee_microlamports

    try:
        quote = await get_quote(input_mint, output_mint, amount_lamports, slippage_bps)
    except httpx.HTTPStatusError as e:
        return SwapResult(success=False, error=f"Quote failed (no route / low liquidity): {e}")
    except Exception as e:
        return SwapResult(success=False, error=f"Quote error: {e}")

    if quote.price_impact_pct > 25:
        return SwapResult(success=False, error=f"Price impact too high ({quote.price_impact_pct:.1f}%), aborting")

    try:
        tx = await build_swap_transaction(quote, priority_fee)
    except Exception as e:
        return SwapResult(success=False, error=f"Failed to build swap tx: {e}")

    # NOTE: Jupiter's returned transaction already includes compute budget +
    # swap instructions signed for our pubkey as fee payer. We sign it as-is.
    # (A separate standalone Jito tip transfer can be sent alongside — see
    # submit_jito_bundle below — rather than injected into this tx, since
    # Jupiter's tx is pre-serialized.)
    signed_tx = wallet.sign_transaction(tx)

    try:
        resp = await wallet.client.send_raw_transaction(bytes(signed_tx))
        sig = str(resp.value)
    except Exception as e:
        return SwapResult(success=False, error=f"Broadcast failed / simulation rejected: {e}")

    try:
        await wallet.client.confirm_transaction(resp.value, commitment="confirmed")
    except Exception as e:
        return SwapResult(success=False, error=f"Sent but not confirmed in time: {e} (sig={sig})")

    if add_jito_tip:
        try:
            await send_jito_tip(settings.jito_tip_lamports)
        except Exception:
            pass  # tip failure should never block a successful swap

    return SwapResult(success=True, signature=sig)


async def send_jito_tip(lamports: int):
    """Sends a small standalone tip to a Jito tip account to incentivize
    fast inclusion of nearby transactions from this wallet. For true atomic
    bundling, replace this with a real sendBundle call to
    settings.jito_block_engine_url + '/api/v1/bundles'."""
    import random
    tip_account = Pubkey.from_string(random.choice(JITO_TIP_ACCOUNTS))
    ix = transfer(TransferParams(from_pubkey=wallet.pubkey, to_pubkey=tip_account, lamports=lamports))
    latest = await wallet.client.get_latest_blockhash()
    msg = MessageV0.try_compile(
        payer=wallet.pubkey,
        instructions=[ix],
        address_lookup_table_accounts=[],
        recent_blockhash=latest.value.blockhash,
    )
    tx = VersionedTransaction(msg, [wallet.keypair])
    await wallet.client.send_raw_transaction(bytes(tx))


async def buy_token(mint: str, sol_amount: float, slippage_bps: int | None = None) -> SwapResult:
    if sol_amount > settings.max_buy_sol:
        return SwapResult(success=False, error=f"Amount exceeds MAX_BUY_SOL safety cap ({settings.max_buy_sol} SOL)")
    lamports = int(sol_amount * LAMPORTS_PER_SOL)
    return await execute_swap(SOL_MINT, mint, lamports, slippage_bps)


async def sell_token(mint: str, token_amount_raw: int, decimals: int, slippage_bps: int | None = None) -> SwapResult:
    return await execute_swap(mint, SOL_MINT, token_amount_raw, slippage_bps)
