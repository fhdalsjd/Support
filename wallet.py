import base58
import base64
import httpx
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from settings import settings

LAMPORTS_PER_SOL = 1_000_000_000


class Wallet:
    """Wallet wrapper.

    Startup never requires a private key. This is important because the bot's
    Telegram UI can start before a wallet is connected/configured.
    Secrets, when used for automated trading, are loaded only from Railway
    environment variables and transactions are signed locally.
    """

    def __init__(self):
        self.keypair: Keypair | None = None
        self.rpc_url = settings.rpc_url

    def _load(self) -> Keypair:
        if settings.wallet_private_key:
            try:
                raw = base58.b58decode(settings.wallet_private_key.strip())
            except Exception as exc:
                raise RuntimeError("WALLET_PRIVATE_KEY is not valid Base58") from exc
            if len(raw) == 64:
                return Keypair.from_bytes(raw)
            if len(raw) == 32:
                return Keypair.from_seed(raw)
            raise RuntimeError("WALLET_PRIVATE_KEY must decode to 32 or 64 bytes")

        if settings.wallet_mnemonic:
            try:
                from bip_utils import Bip39SeedGenerator, Bip44, Bip44Coins
            except ImportError as exc:
                raise RuntimeError("Install bip-utils to use WALLET_MNEMONIC") from exc
            seed = Bip39SeedGenerator(settings.wallet_mnemonic).Generate()
            node = Bip44.FromSeed(seed, Bip44Coins.SOLANA).DeriveDefaultPath()
            return Keypair.from_seed(node.PrivateKey().Raw().ToBytes())

        if settings.generate_burner:
            kp = Keypair()
            print(f"[BURNER] Generated wallet: {kp.pubkey()}")
            return kp

        raise RuntimeError("Wallet not connected/configured")

    def connect_from_environment(self) -> Pubkey:
        """Load the configured automated-trading wallet on demand."""
        if self.keypair is None:
            self.keypair = self._load()
        return self.keypair.pubkey()

    @property
    def connected(self) -> bool:
        return self.keypair is not None

    @property
    def pubkey(self) -> Pubkey:
        if self.keypair is None:
            raise RuntimeError("Wallet not connected")
        return self.keypair.pubkey()

    def short_address(self) -> str:
        address = str(self.pubkey)
        return f"{address[:5]}…{address[-5:]}"

    async def _rpc(self, method: str, params: list):
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(self.rpc_url, json=payload)
            response.raise_for_status()
            data = response.json()
        if "error" in data:
            raise RuntimeError(str(data["error"]))
        return data.get("result")

    async def balance_sol_async(self) -> float:
        address = str(self.pubkey)
        result = await self._rpc("getBalance", [address, {"commitment": "confirmed"}])
        return result["value"] / LAMPORTS_PER_SOL

    def sign(self, raw_tx: bytes) -> bytes:
        if self.keypair is None:
            raise RuntimeError("Wallet not connected")
        tx = VersionedTransaction.from_bytes(raw_tx)
        signed = VersionedTransaction(tx.message, [self.keypair])
        return bytes(signed)

    async def send_raw(self, raw_tx: bytes) -> str:
        encoded = base64.b64encode(raw_tx).decode("ascii")
        result = await self._rpc(
            "sendTransaction",
            [encoded, {"encoding": "base64", "skipPreflight": False, "maxRetries": 2}],
        )
        return str(result)


# Lazy wallet: importing bot.py must never crash because no wallet secret is set.
wallet = Wallet()
