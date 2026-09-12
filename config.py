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
    jupiter_quote_api: str = os.getenv("JUPITER_QUOTE_API", "https://lite-api.jup.ag/swap/v1")
    jupiter_price_api: str = os.getenv("JUPITER_PRICE_API", "https://lite-api.jup.ag/price/v3")

    jito_block_engine_url: str = os.getenv("JITO_BLOCK_ENGINE_URL", "https://mainnet.block-engine.jito.wtf")
    jito_tip_lamports: int = _int("JITO_TIP_LAMPORTS", 100_000)

    default_slippage_bps: int = _int("DEFAULT_SLIPPAGE_BPS", 500)
    default_priority_fee_microlamports: int = _int("DEFAULT_PRIORITY_FEE_MICROLAMPORTS", 50_000)
    max_buy_sol: float = _float("MAX_BUY_SOL", 2.0)

    # Auto-trading fee protection. This SOL is deliberately unavailable for
    # new buys so TP/SL/Smart-SL sells still have a fee-paying balance.
    auto_fee_reserve_sol: float = _float("AUTO_FEE_RESERVE_SOL", 0.005)
    auto_safety_buffer_sol: float = _float("AUTO_SAFETY_BUFFER_SOL", 0.002)

    # Auto-sniper is opt-in in state.json. These limits are deliberately
    # independent from the manual MAX_BUY_SOL cap.
    auto_sniper_buy_sol: float = _float("AUTO_SNIPER_BUY_SOL", 0.01)
    auto_sniper_min_liquidity_usd: float = _float("AUTO_SNIPER_MIN_LIQUIDITY_USD", 25000.0)
    auto_sniper_min_market_cap_usd: float = _float("AUTO_SNIPER_MIN_MARKET_CAP_USD", 50000.0)
    auto_sniper_min_volume_5m_usd: float = _float("AUTO_SNIPER_MIN_VOLUME_5M_USD", 1000.0)
    auto_sniper_max_price_impact_pct: float = _float("AUTO_SNIPER_MAX_PRICE_IMPACT_PCT", 5.0)
    auto_sniper_max_risk_score: int = _int("AUTO_SNIPER_MAX_RISK_SCORE", 20)
    auto_sniper_poll_seconds: int = _int("AUTO_SNIPER_POLL_SECONDS", 20)
    auto_sniper_cooldown_seconds: int = _int("AUTO_SNIPER_COOLDOWN_SECONDS", 900)
    auto_sniper_max_positions: int = _int("AUTO_SNIPER_MAX_POSITIONS", 3)
    auto_sniper_max_exposure_sol: float = _float("AUTO_SNIPER_MAX_EXPOSURE_SOL", 0.03)
    auto_sniper_min_liquidity_mcap_pct: float = _float("AUTO_SNIPER_MIN_LIQUIDITY_MCAP_PCT", 8.0)
    auto_sniper_max_5m_change_pct: float = _float("AUTO_SNIPER_MAX_5M_CHANGE_PCT", 25.0)
    auto_sniper_min_age_minutes: int = _int("AUTO_SNIPER_MIN_AGE_MINUTES", 10)
    auto_sniper_max_age_minutes: int = _int("AUTO_SNIPER_MAX_AGE_MINUTES", 1440)
    auto_sniper_min_buy_sell_ratio: float = _float("AUTO_SNIPER_MIN_BUY_SELL_RATIO", 0.80)
    auto_sniper_slippage_bps: int = _int("AUTO_SNIPER_SLIPPAGE_BPS", 300)

    # Smart SL: protect the original downside, move to break-even once proven,
    # then lock 50% of the best profit reached. The stop only moves upward.
    smart_sl_activation_pct: float = _float("SMART_SL_ACTIVATION_PCT", 5.0)
    smart_sl_profit_lock_start_pct: float = _float("SMART_SL_PROFIT_LOCK_START_PCT", 10.0)
    smart_sl_lock_ratio: float = _float("SMART_SL_LOCK_RATIO", 0.50)
    smart_sl_ratchet_step_pct: float = _float("SMART_SL_RATCHET_STEP_PCT", 2.0)

    # Velocity panic exit: independent of PnL from entry. If price falls by
    # this % within this rolling window, close immediately. Catches a fast
    # crash from a local high (e.g. ran to +40%, then dumped 20% in 3
    # minutes) that hard SL and the profit-lock ratchet can both miss, since
    # neither reacts to *how fast* price is moving, only to fixed levels.
    smart_sl_velocity_window_minutes: float = _float("SMART_SL_VELOCITY_WINDOW_MINUTES", 3.0)
    smart_sl_velocity_drop_pct: float = _float("SMART_SL_VELOCITY_DROP_PCT", 15.0)

    rugcheck_api: str = os.getenv("RUGCHECK_API", "https://api.rugcheck.xyz/v1")
    state_file: str = os.getenv("STATE_FILE", "./state.json")

    def validate(self):
        errs = []
        if not self.telegram_token:
            errs.append("TELEGRAM_BOT_TOKEN is missing")
        if not self.admin_ids:
            errs.append("ADMIN_IDS is empty — bot would be unusable/unsafe with no whitelist")
        if self.wallet_mnemonic and self.wallet_private_key_b58:
            errs.append("Provide only ONE of WALLET_MNEMONIC / WALLET_PRIVATE_KEY_B58, not both")
        if self.auto_sniper_buy_sol <= 0 or self.auto_sniper_buy_sol > self.max_buy_sol:
            errs.append("AUTO_SNIPER_BUY_SOL must be > 0 and <= MAX_BUY_SOL")
        if self.auto_fee_reserve_sol <= 0:
            errs.append("AUTO_FEE_RESERVE_SOL must be > 0")
        if self.auto_safety_buffer_sol < 0:
            errs.append("AUTO_SAFETY_BUFFER_SOL must be >= 0")
        if self.auto_sniper_max_positions < 1:
            errs.append("AUTO_SNIPER_MAX_POSITIONS must be >= 1")
        if self.auto_sniper_max_exposure_sol < self.auto_sniper_buy_sol:
            errs.append("AUTO_SNIPER_MAX_EXPOSURE_SOL must be >= AUTO_SNIPER_BUY_SOL")
        if self.auto_sniper_max_risk_score < 0 or self.auto_sniper_max_risk_score > 100:
            errs.append("AUTO_SNIPER_MAX_RISK_SCORE must be between 0 and 100")
        if self.auto_sniper_min_liquidity_mcap_pct < 0:
            errs.append("AUTO_SNIPER_MIN_LIQUIDITY_MCAP_PCT must be >= 0")
        if self.auto_sniper_max_5m_change_pct <= 0:
            errs.append("AUTO_SNIPER_MAX_5M_CHANGE_PCT must be > 0")
        if self.auto_sniper_min_age_minutes < 0 or self.auto_sniper_max_age_minutes < self.auto_sniper_min_age_minutes:
            errs.append("AUTO_SNIPER age limits are invalid")
        if self.auto_sniper_min_buy_sell_ratio < 0:
            errs.append("AUTO_SNIPER_MIN_BUY_SELL_RATIO must be >= 0")
        if self.auto_sniper_slippage_bps <= 0 or self.auto_sniper_slippage_bps > self.default_slippage_bps:
            errs.append("AUTO_SNIPER_SLIPPAGE_BPS must be > 0 and <= DEFAULT_SLIPPAGE_BPS")
        if not 0 < self.smart_sl_lock_ratio <= 1:
            errs.append("SMART_SL_LOCK_RATIO must be > 0 and <= 1")
        if self.smart_sl_activation_pct <= 0:
            errs.append("SMART_SL_ACTIVATION_PCT must be > 0")
        if self.smart_sl_profit_lock_start_pct < self.smart_sl_activation_pct:
            errs.append("SMART_SL_PROFIT_LOCK_START_PCT must be >= SMART_SL_ACTIVATION_PCT")
        if self.smart_sl_ratchet_step_pct <= 0:
            errs.append("SMART_SL_RATCHET_STEP_PCT must be > 0")
        if self.smart_sl_velocity_window_minutes <= 0:
            errs.append("SMART_SL_VELOCITY_WINDOW_MINUTES must be > 0")
        if self.smart_sl_velocity_drop_pct <= 0:
            errs.append("SMART_SL_VELOCITY_DROP_PCT must be > 0")
        if self.default_slippage_bps <= 0 or self.default_slippage_bps > 5000:
            errs.append("DEFAULT_SLIPPAGE_BPS must be between 1 and 5000")
        if self.max_buy_sol <= 0:
            errs.append("MAX_BUY_SOL must be > 0")
        if errs:
            raise RuntimeError("Config errors:\n- " + "\n- ".join(errs))

    @property
    def wallet_configured(self) -> bool:
        return bool(self.wallet_mnemonic or self.wallet_private_key_b58)


settings = Settings()
