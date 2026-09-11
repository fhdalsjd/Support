"""JSON persistence for positions and bot settings."""
import asyncio
import json
import os
from dataclasses import asdict, dataclass

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
    peak_profit_pct: float = 0.0
    smart_stop_profit_pct: float | None = None
    entry_sol: float = 0.0


DEFAULT_STATE = {
    "positions": {},
    "slippage_bps": settings.default_slippage_bps,
    "priority_fee_microlamports": settings.default_priority_fee_microlamports,
    "auto_sniper_enabled": False,
    "auto_sniper_allocation_pct": None,
    "default_tp_pct": 100.0,
    "default_sl_pct": 30.0,
}


def _load() -> dict:
    if not os.path.exists(settings.state_file):
        return json.loads(json.dumps(DEFAULT_STATE))
    try:
        with open(settings.state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("state root must be an object")
    except Exception:
        return json.loads(json.dumps(DEFAULT_STATE))
    for k, v in DEFAULT_STATE.items():
        data.setdefault(k, v)
    if not isinstance(data.get("positions"), dict):
        data["positions"] = {}
    return data


def _save(data: dict):
    path = settings.state_file
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class StateStore:
    def __init__(self):
        self._data = _load()

    async def get_positions(self) -> dict[str, Position]:
        async with _lock:
            positions = {}
            for mint, raw in self._data["positions"].items():
                try:
                    positions[mint] = Position(**raw)
                except TypeError:
                    continue
            return positions

    async def upsert_position(self, pos: Position):
        async with _lock:
            self._data["positions"][pos.mint] = asdict(pos)
            _save(self._data)

    async def remove_position(self, mint: str):
        async with _lock:
            self._data["positions"].pop(mint, None)
            _save(self._data)

    async def reduce_position(self, mint: str, sell_fraction: float):
        if not 0 < sell_fraction <= 1:
            raise ValueError("sell_fraction must be between 0 and 1")
        async with _lock:
            p = self._data["positions"].get(mint)
            if not p:
                return
            p["amount_tokens"] = max(0.0, float(p.get("amount_tokens", 0.0)) * (1 - sell_fraction))
            if sell_fraction >= 0.999 or p["amount_tokens"] <= 0:
                self._data["positions"].pop(mint, None)
            _save(self._data)

    async def reduce_position_amount(self, mint: str, sold_tokens: float):
        """Subtract the exact token quantity successfully sold from a position."""
        if sold_tokens <= 0:
            raise ValueError("sold_tokens must be positive")
        async with _lock:
            p = self._data["positions"].get(mint)
            if not p:
                return
            remaining = max(0.0, float(p.get("amount_tokens", 0.0)) - sold_tokens)
            if remaining <= 0:
                self._data["positions"].pop(mint, None)
            else:
                p["amount_tokens"] = remaining
                old_amount = max(remaining + sold_tokens, 1e-18)
                old_entry = max(0.0, float(p.get("entry_sol", 0.0) or 0.0))
                p["entry_sol"] = old_entry * (remaining / old_amount)
            _save(self._data)

    async def get_settings(self) -> dict:
        async with _lock:
            return dict(self._data)

    async def set_setting(self, key: str, value):
        async with _lock:
            self._data[key] = value
            _save(self._data)


store = StateStore()
