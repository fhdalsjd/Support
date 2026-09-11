import base58
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.rpc.api import Client
from solders.rpc.config import RpcSendTransactionConfig
from solders.transaction import VersionedTransaction
from settings import settings

LAMPORTS_PER_SOL = 1_000_000_000

class Wallet:
    def __init__(self):
        self.keypair = self._load()
        self.client = Client(settings.rpc_url)
    def _load(self) -> Keypair:
        if settings.wallet_private_key:
            return Keypair.from_bytes(base58.b58decode(settings.wallet_private_key))
        if settings.wallet_mnemonic:
            try:
                from bip_utils import Bip39SeedGenerator, Bip44, Bip44Coins
            except ImportError as e:
                raise RuntimeError("Install bip-utils to use WALLET_MNEMONIC") from e
            seed = Bip39SeedGenerator(settings.wallet_mnemonic).Generate()
            node = Bip44.FromSeed(seed, Bip44Coins.SOLANA).DeriveDefaultPath()
            return Keypair.from_seed(node.PrivateKey().Raw().ToBytes())
        if settings.generate_burner:
            kp = Keypair()
            print(f"[BURNER] New wallet generated: {kp.pubkey()}")
            print("[BURNER] Railway does not persist this private key. Export it immediately if you need it.")
            return kp
        raise RuntimeError("Set WALLET_PRIVATE_KEY or WALLET_MNEMONIC, or GENERATE_BURNER=true")
    @property
    def pubkey(self) -> Pubkey: return self.keypair.pubkey()
    def short_address(self) -> str:
        s = str(self.pubkey); return f"{s[:5]}…{s[-5:]}"
    def balance_sol(self) -> float:
        return self.client.get_balance(self.pubkey).value / LAMPORTS_PER_SOL
    def sign(self, raw_tx: bytes) -> bytes:
        tx = VersionedTransaction.from_bytes(raw_tx)
        return bytes(VersionedTransaction(tx.message, [self.keypair]))
    def send_raw(self, raw_tx: bytes) -> str:
        result = self.client.send_raw_transaction(raw_tx, opts=RpcSendTransactionConfig(skip_preflight=False))
        return str(result.value)

wallet = Wallet()
