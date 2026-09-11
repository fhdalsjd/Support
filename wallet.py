"""wallet.py — optional Solana wallet with safe runtime connection.

Wallet credentials supplied through Telegram are kept in memory only. The
incoming Telegram message can be deleted after validation; the secret is
never written to state.json or logged.
"""
import base58
import logging
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
log = logging.getLogger("wallet")


def _keypair_from_mnemonic(mnemonic: str) -> Keypair:
    seed_bytes = Bip39SeedGenerator(mnemonic).Generate()
    bip44 = Bip44.FromSeed(seed_bytes, Bip44Coins.SOLANA).Purpose().Coin()
    account = bip44.Account(SOLANA_DERIVATION_ACCOUNT).Change(Bip44Changes.CHAIN_EXT)
    return Keypair.from_seed(account.PrivateKey().Raw().ToBytes())


def _keypair_from_b58(key_b58: str) -> Keypair:
    raw = base58.b58decode(key_b58)
    return Keypair.from_bytes(raw)


def load_keypair() -> Keypair | None:
    if settings.wallet_mnemonic:
        try:
            return _keypair_from_mnemonic(settings.wallet_mnemonic)
        except Exception as exc:
            log.error("WALLET_MNEMONIC could not be loaded: %s", type(exc).__name__)
            return None
    if settings.wallet_private_key_b58:
        try:
            return _keypair_from_b58(settings.wallet_private_key_b58)
        except Exception as exc:
            log.error("WALLET_PRIVATE_KEY_B58 could not be loaded: %s", type(exc).__name__)
            return None
    return None


class Wallet:
    def __init__(self):
        self.keypair: Keypair | None = None
        self.pubkey: Pubkey | None = None
        self.client: AsyncClient | None = None
        self.last_error: str | None = None
        self._load_from_environment()

    def _load_from_environment(self):
        if settings.wallet_mnemonic:
            try:
                self.connect_mnemonic(settings.wallet_mnemonic)
                return
            except Exception as exc:
                self.last_error = "Configured wallet mnemonic is invalid"
                log.error("Configured wallet could not be loaded: %s", type(exc).__name__)
        elif settings.wallet_private_key_b58:
            try:
                self.connect_private_key(settings.wallet_private_key_b58)
                return
            except Exception as exc:
                self.last_error = "Configured wallet private key is invalid"
                log.error("Configured wallet could not be loaded: %s", type(exc).__name__)

    @property
    def configured(self) -> bool:
        return self.keypair is not None and self.pubkey is not None and self.client is not None

    def connect_mnemonic(self, mnemonic: str) -> str:
        mnemonic = " ".join(mnemonic.strip().split())
        if not mnemonic:
            raise ValueError("Mnemonic is empty")
        keypair = _keypair_from_mnemonic(mnemonic)
        self._set_keypair(keypair)
        self.last_error = None
        return str(self.pubkey)

    def connect_private_key(self, key_b58: str) -> str:
        key_b58 = key_b58.strip()
        if not key_b58:
            raise ValueError("Private key is empty")
        keypair = _keypair_from_b58(key_b58)
        self._set_keypair(keypair)
        self.last_error = None
        return str(self.pubkey)

    def _set_keypair(self, keypair: Keypair):
        self.keypair = keypair
        self.pubkey = keypair.pubkey()
        self.client = AsyncClient(settings.rpc_url, commitment=Confirmed)

    def disconnect(self):
        self.keypair = None
        self.pubkey = None
        self.client = None
        self.last_error = None

    def _require_configured(self):
        if not self.configured:
            raise RuntimeError("No wallet connected. Use /connect_wallet to connect a wallet.")

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
            r = await http.get(settings.jupiter_price_api + "/price", params={"ids": "So11111111111111111111111111111111111111112"})
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
        signature = self.keypair.sign_message(bytes(tx.message))
        tx.signatures[0] = signature
        return tx

    async def close(self):
        if self.client:
            await self.client.close()
            self.client = None


wallet = Wallet()
