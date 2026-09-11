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
    # Old quote-api.jup.ag/v6 is no longer usable. Lite API is the current no-key
    # endpoint; production users can set JUPITER_QUOTE_API to their own endpoint.
    jupiter_quote_api: str = os.getenv("JUPITER_QUOTE_API", "https://lite-api.jup.ag/swap/v1")
    jupiter_price_api: str = os.getenv("JUPITER_PRICE_API", "https://lite-api.jup.ag/price/v3")

    jito_block_engine_url: str = os.getenv("JITO_BLOCK_ENGINE_URL", "https://mainnet.block-engine.jito.wtf")
    jito_tip_lamports: int = _int("JITO_TIP_LAMPORTS", 100_000)

    default_slippage_bps: int = _int("DEFAULT_SLIPPAGE_BPS", 500)
    default_priority_fee_microlamports: int = _int("DEFAULT_PRIORITY_FEE_MICROLAMPORTS", 50_000)
    max_buy_sol: float = _float("MAX_BUY_SOL", 2.0)

    # Auto-sniper is opt-in in state.json. These env settings cap its exposure.
    auto_sniper_buy_sol: float = _float("AUTO_SNIPER_BUY_SOL", 0.01)
    auto_sniper_min_liquidity_usd: float = _float("AUTO_SNIPER_MIN_LIQUIDITY_USD", 25000.0)
    auto_sniper_min_market_cap_usd: float = _float("AUTO_SNIPER_MIN_MARKET_CAP_USD", 50000.0)
    auto_sniper_min_volume_5m_usd: float = _float("AUTO_SNIPER_MIN_VOLUME_5M_USD", 1000.0)
    auto_sniper_max_price_impact_pct: float = _float("AUTO_SNIPER_MAX_PRICE_IMPACT_PCT", 5.0)
    auto_sniper_max_risk_score: int = _int("AUTO_SNIPER_MAX_RISK_SCORE", 0)
    auto_sniper_poll_seconds: int = _int("AUTO_SNIPER_POLL_SECONDS", 20)
    auto_sniper_cooldown_seconds: int = _int("AUTO_SNIPER_COOLDOWN_SECONDS", 900)

    # Smart SL: protect the original downside, move to break-even once proven,
    # then lock 50% of the best profit reached. The stop only moves upward.
    smart_sl_activation_pct: float = _float("SMART_SL_ACTIVATION_PCT", 5.0)
    smart_sl_profit_lock_start_pct: float = _float("SMART_SL_PROFIT_LOCK_START_PCT", 10.0)
    smart_sl_lock_ratio: float = _float("SMART_SL_LOCK_RATIO", 0.50)
    smart_sl_ratchet_step_pct: float = _float("SMART_SL_RATCHET_STEP_PCT", 2.0)

    rugcheck_api: str = os.getenv("RUGCHECK_API", "https://api.rugcheck.xyz/v1")
    state_file: str = os.getenv("STATE_FILE", "./state.json")

    def validate(self):
        """Validate only settings required for the Telegram service to start."""
        errs = []
        if not self.telegram_token:
            errs.append("TELEGRAM_BOT_TOKEN is missing")
        if not self.admin_ids:
            errs.append("ADMIN_IDS is empty — bot would be unusable/unsafe with no whitelist")
        if self.wallet_mnemonic and self.wallet_private_key_b58:
            errs.append("Provide only ONE of WALLET_MNEMONIC / WALLET_PRIVATE_KEY_B58, not both")
        if self.auto_sniper_buy_sol <= 0 or self.auto_sniper_buy_sol > self.max_buy_sol:
            errs.append("AUTO_SNIPER_BUY_SOL must be > 0 and <= MAX_BUY_SOL")
        if not 0 < self.smart_sl_lock_ratio <= 1:
            errs.append("SMART_SL_LOCK_RATIO must be > 0 and <= 1")
        if self.smart_sl_activation_pct <= 0:
            errs.append("SMART_SL_ACTIVATION_PCT must be > 0")
        if self.smart_sl_profit_lock_start_pct < self.smart_sl_activation_pct:
            errs.append("SMART_SL_PROFIT_LOCK_START_PCT must be >= SMART_SL_ACTIVATION_PCT")
        if self.smart_sl_ratchet_step_pct <= 0:
            errs.append("SMART_SL_RATCHET_STEP_PCT must be > 0")
        if errs:
            raise RuntimeError("Config errors:\n- " + "\n- ".join(errs))

    @property
    def wallet_configured(self) -> bool:
        return bool(self.wallet_mnemonic or self.wallet_private_key_b58)


settings = Settings()
