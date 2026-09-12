"""Quote + swap execution with preflight simulation and strict safety checks."""
import asyncio
import base64
import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR

import httpx
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
INSUFFICIENT_RE = re.compile(r"insufficient lamports (\d+), need (\d+)", re.I)


def to_raw_units(amount: float | Decimal | str, decimals: int) -> int:
    """Safely convert UI token amount to raw integer units without float truncation."""
    d_amount = Decimal(str(amount))
    multiplier = Decimal(10) ** decimals
    return int((d_amount * multiplier).to_integral_value(rounding=ROUND_FLOOR))


def _jupiter_base() -> str:
    base = settings.jupiter_quote_api.rstrip("/")
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
    in_amount: int | None = None   # raw units of the input mint actually quoted (lamports for a buy)
    out_amount: int | None = None  # raw units of the output mint actually quoted (raw token units for a buy)


async def _request(method: str, url: str, **kwargs):
    attempts = 3 if method.upper() == "GET" else 2
    for attempt in range(attempts):
        try:
            async with httpx.AsyncClient(timeout=15) as http:
                r = await http.request(method, url, **kwargs)
                if r.status_code >= 500 and attempt + 1 < attempts:
                    await asyncio.sleep(0.7 * (attempt + 1))
                    continue
                return r
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError):
            if attempt + 1 < attempts:
                await asyncio.sleep(0.7 * (attempt + 1))
                continue
            raise
    raise RuntimeError("HTTP request failed")


async def get_quote(input_mint: str, output_mint: str, amount_lamports: int, slippage_bps: int) -> QuoteResult:
    if amount_lamports <= 0:
        raise ValueError("Swap amount must be positive")
    if slippage_bps <= 0 or slippage_bps > 5000:
        raise ValueError("Slippage must be between 1 and 5000 bps")
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


def _insufficient_balance_message(current_lamports: int, diagnostic: str) -> str | None:
    match = INSUFFICIENT_RE.search(diagnostic)
    if not match:
        return None
    available_in_instruction = int(match.group(1))
    needed_by_instruction = int(match.group(2))
    shortfall = max(0, needed_by_instruction - available_in_instruction)
    required_start = current_lamports + shortfall
    top_up = max(0, required_start - current_lamports)
    return (
        "❌ Insufficient SOL balance.\n"
        f"Current: {current_lamports / LAMPORTS_PER_SOL:.9f} SOL\n"
        f"Required: {required_start / LAMPORTS_PER_SOL:.9f} SOL\n"
        f"Please add at least {top_up / LAMPORTS_PER_SOL:.9f} SOL, then press Refresh and try again."
    )


async def execute_swap(
    input_mint: str,
    output_mint: str,
    amount_lamports: int,
    slippage_bps: int | None = None,
    priority_fee_microlamports: int | None = None,
) -> SwapResult:
    slippage_bps = settings.default_slippage_bps if slippage_bps is None else slippage_bps
    priority_fee = settings.default_priority_fee_microlamports if priority_fee_microlamports is None else priority_fee_microlamports

    if input_mint == output_mint:
        return SwapResult(success=False, error="Input and output token are identical")
    if not wallet.configured:
        return SwapResult(success=False, error="No wallet connected")

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
        current_lamports = int((await wallet.client.get_balance(wallet.pubkey, commitment="confirmed")).value)
        signed_tx = wallet.sign_transaction(tx)
        simulation = await wallet.client.simulate_transaction(signed_tx, sig_verify=False, commitment="confirmed")
        sim_value = simulation.value
        if sim_value.err is not None:
            diagnostic = str(sim_value.err)
            if sim_value.logs:
                diagnostic += " " + " ".join(sim_value.logs)
            friendly = _insufficient_balance_message(current_lamports, diagnostic)
            if friendly:
                return SwapResult(success=False, error=friendly)
            return SwapResult(success=False, error=f"Transaction simulation rejected: {diagnostic[:1200]}")
    except Exception as e:
        return SwapResult(success=False, error=f"Preflight simulation failed: {e}")

    try:
        resp = await wallet.client.send_raw_transaction(bytes(signed_tx))
        sig = str(resp.value)
    except Exception as e:
        try:
            current_lamports = int((await wallet.client.get_balance(wallet.pubkey, commitment="confirmed")).value)
            friendly = _insufficient_balance_message(current_lamports, str(e))
            if friendly:
                return SwapResult(success=False, error=friendly)
        except Exception:
            pass
        return SwapResult(success=False, error=f"Broadcast failed / simulation rejected: {e}")

    try:
        await wallet.client.confirm_transaction(resp.value, commitment="confirmed")
    except Exception as e:
        return SwapResult(success=False, error=f"Sent but not confirmed in time: {e} (sig={sig})")

    return SwapResult(success=True, signature=sig, in_amount=quote.in_amount, out_amount=quote.out_amount)


async def send_jito_tip(lamports: int):
    """Utility to send a tip to Jito tip accounts (intended for bundled transactions)."""
    import random
    if lamports <= 0 or not wallet.configured:
        return
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
    if sol_amount <= 0:
        return SwapResult(success=False, error="Buy amount must be positive")
    if sol_amount > settings.max_buy_sol:
        return SwapResult(success=False, error=f"Amount exceeds MAX_BUY_SOL safety cap ({settings.max_buy_sol} SOL)")
    lamports = to_raw_units(sol_amount, 9)
    if lamports <= 0:
        return SwapResult(success=False, error="Buy amount in lamports is zero")
    return await execute_swap(SOL_MINT, mint, lamports, slippage_bps)


async def sell_token(mint: str, token_amount_raw: int, decimals: int, slippage_bps: int | None = None) -> SwapResult:
    if token_amount_raw <= 0:
        return SwapResult(success=False, error="Sell amount must be positive")
    if decimals < 0 or decimals > 18:
        return SwapResult(success=False, error="Invalid token decimals")
    return await execute_swap(mint, SOL_MINT, token_amount_raw, slippage_bps)
