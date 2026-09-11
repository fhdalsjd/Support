"""
trading.py — Quote + swap execution.

Uses Jupiter's current Lite Swap API. The old quote-api.jup.ag/v6 host is
retired/unreachable; lite-api.jup.ag/swap/v1 is the no-key endpoint.
"""
import asyncio
import base64
import httpx
from dataclasses import dataclass
from solders.transaction import VersionedTransaction
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.message import MessageV0

from config import settings
from wallet import wallet, LAMPORTS_PER_SOL

SOL_MINT = "So11111111111111111111111111111111111111112"
JITO_TIP_ACCOUNTS = [
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
]


def _jupiter_base() -> str:
    base = settings.jupiter_quote_api.rstrip("/")
    # Protect Railway deployments that still have the old variable configured.
    if "quote-api.jup.ag" in base or base.endswith("/v6"):
        return "https://lite-api.jup.ag/swap/v1"
    return base


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


async def _request(method: str, url: str, **kwargs):
    """Retry transient DNS/network/5xx failures without ever retrying a swap POST blindly."""
    attempts = 3 if method.upper() == "GET" else 2
    last = None
    for attempt in range(attempts):
        try:
            async with httpx.AsyncClient(timeout=15) as http:
                r = await http.request(method, url, **kwargs)
                if r.status_code >= 500 and attempt + 1 < attempts:
                    await asyncio.sleep(0.7 * (attempt + 1))
                    continue
                return r
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
            last = exc
            if attempt + 1 < attempts:
                await asyncio.sleep(0.7 * (attempt + 1))
                continue
            raise
    if last:
        raise last
    raise RuntimeError("HTTP request failed")


async def get_quote(input_mint: str, output_mint: str, amount_lamports: int, slippage_bps: int) -> QuoteResult:
    if amount_lamports <= 0:
        raise ValueError("Swap amount must be positive")
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": amount_lamports,
        "slippageBps": slippage_bps,
        "restrictIntermediateTokens": "true",
        "instructionVersion": "V2",
    }
    r = await _request("GET", f"{_jupiter_base()}/quote", params=params)
    if r.status_code >= 400:
        raise RuntimeError(f"Jupiter HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    route_labels = [step.get("swapInfo", {}).get("label", "?") for step in data.get("routePlan", [])]
    return QuoteResult(
        raw=data,
        in_amount=int(data["inAmount"]),
        out_amount=int(data["outAmount"]),
        price_impact_pct=float(data.get("priceImpactPct", 0)) * 100,
        route_summary=" -> ".join(route_labels) or "direct",
    )


async def build_swap_transaction(quote: QuoteResult, priority_fee_microlamports: int) -> VersionedTransaction:
    payload = {
        "quoteResponse": quote.raw,
        "userPublicKey": str(wallet.pubkey),
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": priority_fee_microlamports,
    }
    r = await _request("POST", f"{_jupiter_base()}/swap", json=payload)
    if r.status_code >= 400:
        raise RuntimeError(f"Jupiter swap build HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    raw = base64.b64decode(data["swapTransaction"])
    return VersionedTransaction.from_bytes(raw)


async def execute_swap(
    input_mint: str,
    output_mint: str,
    amount_lamports: int,
    slippage_bps: int | None = None,
    priority_fee_microlamports: int | None = None,
    add_jito_tip: bool = True,
) -> SwapResult:
    slippage_bps = slippage_bps or settings.default_slippage_bps
    priority_fee = priority_fee_microlamports or settings.default_priority_fee_microlamports

    if input_mint == output_mint:
        return SwapResult(success=False, error="Input and output token are identical")

    try:
        quote = await get_quote(input_mint, output_mint, amount_lamports, slippage_bps)
    except httpx.RequestError as e:
        return SwapResult(success=False, error=f"Jupiter network unavailable after retries: {type(e).__name__}")
    except Exception as e:
        return SwapResult(success=False, error=f"Quote error: {e}")

    if quote.price_impact_pct > 25:
        return SwapResult(success=False, error=f"Price impact too high ({quote.price_impact_pct:.1f}%), aborting")

    try:
        tx = await build_swap_transaction(quote, priority_fee)
    except Exception as e:
        return SwapResult(success=False, error=f"Failed to build swap tx: {e}")

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
            pass

    return SwapResult(success=True, signature=sig)


async def send_jito_tip(lamports: int):
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
