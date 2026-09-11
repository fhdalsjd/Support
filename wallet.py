import base58
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solana.rpc.api import Client
from solana.rpc.types import TxOpts
from settings import settings

LAMPORTS_PER_SOL = 1_000_000_000


class Wallet:
    def __init__(self):
        self.keypair = self._load()
        self.client = Client(settings.rpc_url)

    def _load(self) -> Keypair:
        if settings.wallet_private_key:
            raw = base58.b58decode(settings.wallet_private_key)
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
            print("[BURNER] Fund it only after verifying the public key. Use a persistent private key for production.")
            return kp

        raise RuntimeError(
            "Set WALLET_PRIVATE_KEY or WALLET_MNEMONIC, or GENERATE_BURNER=true"
        )

    @property
    def pubkey(self) -> Pubkey:
        return self.keypair.pubkey()

    def short_address(self) -> str:
        address = str(self.pubkey)
        return f"{address[:5]}…{address[-5:]}"

    def balance_sol(self) -> float:
        return self.client.get_balance(self.pubkey).value / LAMPORTS_PER_SOL

    def sign(self, raw_tx: bytes) -> bytes:
        tx = VersionedTransaction.from_bytes(raw_tx)
        signed = VersionedTransaction(tx.message, [self.keypair])
        return bytes(signed)

    def send_raw(self, raw_tx: bytes) -> str:
        result = self.client.send_raw_transaction(
            raw_tx,
            opts=TxOpts(skip_preflight=False, max_retries=2),
        )
        return str(result.value)


wallet = Wallet()
