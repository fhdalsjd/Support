"""
state.py — minimal JSON-file persistence for positions, per-admin settings,
and the auto-sniper toggle. Swap this for SQLite/Postgres if you need
concurrent writers or history; a single-admin bot with one process is fine
with a flat file guarded by an asyncio lock.
"""
import json
import asyncio
import os
from dataclasses import dataclass, field, asdict

from config import settings

_lock = asyncio.Lock()


@dataclass
class Position:
    mint: str
    symbol: str
    entry_price_usd: float
    amount_tokens: float
    decimals: int
    take_profit_pct: float | None = None
    stop_loss_pct: float | None = None
    trailing_sl_pct: float | None = None
    trailing_high_price: float | None = None


DEFAULT_STATE = {
    "positions": {},          # mint -> Position dict
    "slippage_bps": settings.default_slippage_bps,
    "priority_fee_microlamports": settings.default_priority_fee_microlamports,
    "auto_sniper_enabled": False,
    "default_tp_pct": 100.0,   # +100% take profit default
    "default_sl_pct": 30.0,    # -30% stop loss default
}


def _load() -> dict:
    if not os.path.exists(settings.state_file):
        return json.loads(json.dumps(DEFAULT_STATE))
    with open(settings.state_file, "r") as f:
        data = json.load(f)
    for k, v in DEFAULT_STATE.items():
        data.setdefault(k, v)
    return data


def _save(data: dict):
    with open(settings.state_file, "w") as f:
        json.dump(data, f, indent=2)


class StateStore:
    def __init__(self):
        self._data = _load()

    async def get_positions(self) -> dict[str, Position]:
        async with _lock:
            return {m: Position(**p) for m, p in self._data["positions"].items()}

    async def upsert_position(self, pos: Position):
        async with _lock:
            self._data["positions"][pos.mint] = asdict(pos)
            _save(self._data)

    async def remove_position(self, mint: str):
        async with _lock:
            self._data["positions"].pop(mint, None)
            _save(self._data)

    async def reduce_position(self, mint: str, sell_fraction: float):
        """sell_fraction: 0.25 / 0.5 / 1.0 etc — shrinks the recorded amount."""
        async with _lock:
            p = self._data["positions"].get(mint)
            if not p:
                return
            p["amount_tokens"] *= (1 - sell_fraction)
            if sell_fraction >= 0.999 or p["amount_tokens"] <= 0:
                self._data["positions"].pop(mint, None)
            _save(self._data)

    async def get_settings(self) -> dict:
        async with _lock:
            return dict(self._data)

    async def set_setting(self, key: str, value):
        async with _lock:
            self._data[key] = value
            _save(self._data)


store = StateStore()
