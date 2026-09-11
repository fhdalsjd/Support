import os
from dataclasses import dataclass
from typing import Set

def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}

def _float(name: str, default: float) -> float:
    try: return float(os.getenv(name, str(default)))
    except ValueError: return default

def _int(name: str, default: int) -> int:
    try: return int(os.getenv(name, str(default)))
    except ValueError: return default

@dataclass(frozen=True)
class Settings:
    bot_token: str = os.getenv("BOT_TOKEN", "")
    admin_id: int = _int("ADMIN_ID", 0)
    rpc_url: str = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
    wallet_private_key: str = os.getenv("WALLET_PRIVATE_KEY", "")
    wallet_mnemonic: str = os.getenv("WALLET_MNEMONIC", "")
    wallet_account: int = _int("WALLET_ACCOUNT", 0)
    generate_burner: bool = _bool("GENERATE_BURNER", False)
    live_trading: bool = _bool("LIVE_TRADING", False)
    pumpportal_url: str = os.getenv("PUMPPORTAL_URL", "https://pumpportal.fun")
    pumpportal_api_key: str = os.getenv("PUMPPORTAL_API_KEY", "")
    rugcheck_url: str = os.getenv("RUGCHECK_URL", "https://api.rugcheck.xyz")
    dexscreener_url: str = os.getenv("DEXSCREENER_URL", "https://api.dexscreener.com")
    jito_url: str = os.getenv("JITO_BLOCK_ENGINE_URL", "https://mainnet.block-engine.jito.wtf")
    jito_enabled: bool = _bool("JITO_ENABLED", True)
    jito_tip_lamports: int = _int("JITO_TIP_LAMPORTS", 1000)
    slippage_pct: float = _float("DEFAULT_SLIPPAGE_PCT", 5.0)
    priority_fee_sol: float = _float("DEFAULT_PRIORITY_FEE_SOL", 0.00005)
    price_poll_seconds: int = _int("PRICE_POLL_SECONDS", 5)
    max_positions: int = _int("MAX_POSITIONS", 10)
    db_path: str = os.getenv("DB_PATH", "data/trader.sqlite3")
    @property
    def admins(self) -> Set[int]: return {self.admin_id} if self.admin_id else set()

settings = Settings()
