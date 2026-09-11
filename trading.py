import base64
from typing import Literal
import httpx
from settings import settings
from wallet import wallet

class TradeError(RuntimeError): pass

class Trader:
    async def build_trade(self, mint: str, action: Literal["buy","sell"], amount: str, slippage: float | None = None, pool: str = "auto") -> bytes:
        slip = settings.slippage_pct if slippage is None else slippage
        payload = {"publicKey":str(wallet.pubkey),"action":action,"mint":mint,"amount":amount,"denominatedInSol":"true" if action=="buy" else "false","slippage":slip,"priorityFee":settings.priority_fee_sol,"pool":pool}
        async with httpx.AsyncClient() as client:
            r = await client.post(f"{settings.pumpportal_url}/api/trade-local", json=payload, timeout=15)
            if r.status_code != 200: raise TradeError(f"Swap builder rejected request ({r.status_code}): {r.text[:300]}")
            return r.content

    async def execute(self, mint: str, action: Literal["buy","sell"], amount: str, slippage: float | None = None, pool: str = "auto") -> str:
        if not settings.live_trading: raise TradeError("LIVE_TRADING=false — trade blocked.")
        raw = await self.build_trade(mint, action, amount, slippage, pool)
        signed = wallet.sign(raw)
        if settings.jito_enabled: return await self._jito_send(signed)
        return wallet.send_raw(signed)

    async def _jito_send(self, signed: bytes) -> str:
        tx_b64 = base64.b64encode(signed).decode()
        body={"jsonrpc":"2.0","id":1,"method":"sendTransaction","params":[tx_b64,{"encoding":"base64"}]}
        async with httpx.AsyncClient() as client:
            r=await client.post(f"{settings.jito_url}/api/v1/transactions",json=body,timeout=10); data=r.json()
            if "error" in data: raise TradeError(str(data["error"]))
            return str(data["result"])

    async def bundle(self, signed_transactions: list[bytes]) -> str:
        if not 1 <= len(signed_transactions) <= 5: raise TradeError("Jito bundles accept 1–5 transactions")
        encoded=[base64.b64encode(x).decode() for x in signed_transactions]
        body={"jsonrpc":"2.0","id":1,"method":"sendBundle","params":[encoded,{"encoding":"base64"}]}
        async with httpx.AsyncClient() as client:
            r=await client.post(f"{settings.jito_url}/api/v1/bundles",json=body,timeout=10); data=r.json()
            if "error" in data: raise TradeError(str(data["error"]))
            return str(data["result"])

trader=Trader()
