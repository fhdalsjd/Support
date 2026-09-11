"""
config.py — loads and validates all environment configuration in one place.
Nothing else in the project should call os.getenv directly.
"""
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    v = os.getenv(name)
    return int(v) if v else default


def _float(name: str, default: float) -> float:
    v = os.getenv(name)
    return float(v) if v else default


@dataclass(frozen=True)
class Settings:
    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    admin_ids: frozenset = frozenset(
        int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x
    )

    wallet_mnemonic: str = os.getenv("WALLET_MNEMONIC", "").strip()
    wallet_private_key_b58: str = os.getenv("WALLET_PRIVATE_KEY_B58", "").strip()

    rpc_url: str = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
    jupiter_quote_api: str = os.getenv("JUPITER_QUOTE_API", "https://quote-api.jup.ag/v6")
    jupiter_price_api: str = os.getenv("JUPITER_PRICE_API", "https://price.jup.ag/v6")

    jito_block_engine_url: str = os.getenv("JITO_BLOCK_ENGINE_URL", "https://mainnet.block-engine.jito.wtf")
    jito_tip_lamports: int = _int("JITO_TIP_LAMPORTS", 100_000)

    default_slippage_bps: int = _int("DEFAULT_SLIPPAGE_BPS", 500)
    default_priority_fee_microlamports: int = _int("DEFAULT_PRIORITY_FEE_MICROLAMICRO_LAMPORTS", 50_000)
    max_buy_sol: float = _float("MAX_BUY_SOL", 2.0)

    rugcheck_api: str = os.getenv("RUGCHECK_API", "https://api.rugcheck.xyz/v1")
    state_file: str = os.getenv("STATE_FILE", "./state.json")

    def validate(self):
        """Validate only settings required for the Telegram service to start.

        Wallet credentials are intentionally NOT required here. The bot can run
        in a safe read-only/configuration-error mode until a wallet is added.
        """
        errs = []
        if not self.telegram_token:
            errs.append("TELEGRAM_BOT_TOKEN is missing")
        if not self.admin_ids:
            errs.append("ADMIN_IDS is empty — bot would be unusable/unsafe with no whitelist")
        if self.wallet_mnemonic and self.wallet_private_key_b58:
            errs.append("Provide only ONE of WALLET_MNEMONIC / WALLET_PRIVATE_KEY_B58, not both")
        if errs:
            raise RuntimeError("Config errors:\n- " + "\n- ".join(errs))

    @property
    def wallet_configured(self) -> bool:
        return bool(self.wallet_mnemonic or self.wallet_private_key_b58)


settings = Settings()
