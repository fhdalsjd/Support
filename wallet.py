"""
wallet.py — Keypair loading (mnemonic OR raw private key), balance queries,
and low-level transaction signing/sending helpers.

SECURITY NOTES
- The keypair is derived once at process start and held in memory only.
- Never log the mnemonic, private key, or raw keypair bytes.
- Recommend a dedicated burner wallet funded only with risk capital.

When wallet credentials are missing, the module stays importable and the
Telegram bot can start in a safe configuration-error mode. Wallet operations
fail with a clear RuntimeError instead of crashing the process at startup.
"""
import base58
import httpx
from bip_utils import Bip39SeedGenerator, Bip44, Bip44Coins, Bip44Changes
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed

from config import settings

SOL_DECIMALS = 9
LAMPORTS_PER_SOL = 10**9
SOLANA_DERIVATION_ACCOUNT = 0


def _keypair_from_mnemonic(mnemonic: str) -> Keypair:
    seed_bytes = Bip39SeedGenerator(mnemonic).Generate()
    bip44 = Bip44.FromSeed(seed_bytes, Bip44Coins.SOLANA).Purpose().Coin()
    account = bip44.Account(SOLANA_DERIVATION_ACCOUNT).Change(Bip44Changes.CHAIN_EXT)
    priv_key_bytes = account.PrivateKey().Raw().ToBytes()
    return Keypair.from_seed(priv_key_bytes)


def _keypair_from_b58(key_b58: str) -> Keypair:
    raw = base58.b58decode(key_b58)
    return Keypair.from_bytes(raw)


def load_keypair() -> Keypair | None:
    if settings.wallet_mnemonic:
        return _keypair_from_mnemonic(settings.wallet_mnemonic)
    if settings.wallet_private_key_b58:
        return _keypair_from_b58(settings.wallet_private_key_b58)
    return None


class Wallet:
    """Thin async wrapper around the signing keypair + RPC client.

    Missing credentials are allowed at startup so the bot does not crash-loop.
    Any operation that actually needs a wallet gives an actionable error.
    """

    def __init__(self):
        self.keypair: Keypair | None = load_keypair()
        self.pubkey: Pubkey | None = self.keypair.pubkey() if self.keypair else None
        self.client = AsyncClient(settings.rpc_url, commitment=Confirmed) if self.keypair else None

    @property
    def configured(self) -> bool:
        return self.keypair is not None

    def _require_configured(self):
        if not self.configured:
            raise RuntimeError(
                "Wallet is not configured. Add WALLET_MNEMONIC or "
                "WALLET_PRIVATE_KEY_B58 in Railway Variables."
            )

    def short_address(self) -> str:
        self._require_configured()
        s = str(self.pubkey)
        return f"{s[:4]}...{s[-4:]}"

    async def get_sol_balance(self) -> float:
        self._require_configured()
        resp = await self.client.get_balance(self.pubkey, commitment=Confirmed)
        return resp.value / LAMPORTS_PER_SOL

    async def get_sol_usd_price(self) -> float:
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.get(
                settings.jupiter_price_api + "/price",
                params={"ids": "So11111111111111111111111111111111111111112"},
            )
            r.raise_for_status()
            data = r.json()
            return float(data["data"]["So11111111111111111111111111111111111111112"]["price"])

    async def get_token_balance(self, mint: str) -> float:
        self._require_configured()
        opts = {"mint": mint}
        resp = await self.client.get_token_accounts_by_owner_json_parsed(self.pubkey, opts)
        total = 0.0
        for acct in resp.value:
            info = acct.account.data.parsed["info"]
            total += float(info["tokenAmount"]["uiAmount"] or 0)
        return total

    def sign_transaction(self, tx: VersionedTransaction) -> VersionedTransaction:
        self._require_configured()
        message_bytes = bytes(tx.message)
        signature = self.keypair.sign_message(message_bytes)
        tx.signatures[0] = signature
        return tx

    async def close(self):
        if self.client:
            await self.client.close()


# Safe to import even when Railway wallet variables have not been configured.
wallet = Wallet()
