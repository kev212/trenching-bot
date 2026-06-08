"""Tests for USD-canonical position_monitor.

These tests verify:
1. Phantom-SL guard catches 1-tick drops below 5% of entry
2. SL/TP/trailing comparisons are all in USD
"""
import asyncio
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/Users/khezuma/workspace/trenching")

from tracking.position_monitor import _process_position, PHANTOM_SL_THRESHOLD


def make_position(entry_price=0.0001, amount_token=1000.0, amount_sol=0.05,
                  peak_price=None, exit_reason=None, entry_time=None):
    return {
        "id": 1,
        "token_address": "ADDR",
        "token_symbol": "T",
        "entry_price": entry_price,  # USD
        "entry_amount_sol": amount_sol,
        "entry_amount_token": amount_token,
        "current_amount_token": amount_token,
        "total_sold_sol": 0.0,
        "total_sold_usd": 0.0,
        "peak_price": peak_price if peak_price is not None else entry_price,
        "exit_reason": exit_reason,
        "entry_time": entry_time or datetime.now(timezone.utc),
        "status": "OPEN",
        "sol_usd_at_entry": 150.0,
    }


class FakeJupiter:
    """Returns a configurable USD price (already converted: SOL × sol_usd = USD)."""
    def __init__(self, price_usd):
        self.price_usd = price_usd

    async def get_token_price_in_sol_with_retry(self, addr):
        # Caller does: current_price_usd = sol × sol_usd_now
        # So we return USD/sol_usd_now
        return self.price_usd / 150.0


class FakeExecutor:
    def __init__(self, oracle_price_usd):
        self.sells = []
        self.oracle_price_usd = oracle_price_usd
        self.jupiter = FakeJupiter(oracle_price_usd)

    async def execute_sell(self, position, pct, reason, current_price_usd=None):
        self.sells.append((position["token_symbol"], pct, reason))
        position["current_amount_token"] *= (1 - pct / 100)
        position["total_sold_sol"] += position["entry_amount_sol"] * (pct / 100)
        position["total_sold_usd"] = position["total_sold_sol"] * 150.0
        position["exit_reason"] = reason
        return position


class FakePM:
    def __init__(self):
        self.locks = {}
        self.updates = []

    def get_lock(self, addr):
        if addr not in self.locks:
            self.locks[addr] = asyncio.Lock()
        return self.locks[addr]

    async def update_position(self, pos):
        self.updates.append(pos)
        return None

    def cleanup_lock(self, addr):
        self.locks.pop(addr, None)

    def __getattr__(self, name):
        # Return an async no-op for any method we don't define
        async def _noop(*a, **k): return None
        return _noop


def test_phantom_sl_guard_skips_extreme_drop():
    """A 1-tick drop below 5% of entry is treated as data issue, not a real SL.

    This is the exact scenario that caused the user's -98% PnL phantom SL.
    """
    async def go():
        # Current price = 0.0000000001 (1e-10), entry = 0.0001
        # 1e-10 < 0.0001 × 0.05 = 5e-6 → phantom-SL guard fires
        executor = FakeExecutor(oracle_price_usd=0.0000000001)
        pm = FakePM()
        position = make_position(entry_price=0.0001)
        result = await _process_position(
            position=position,
            executor=executor,
            position_manager=pm,
            is_paper=False,  # use jupiter path
            stop_loss_pct=50.0,
            tp1_mult=1.5,
            tp2_mult=2.0,
            extreme_tp_mult=5.0,
            tp1_pct=33.0,
            trailing_pct=20.0,
            min_hold_seconds=60,
            time_limit=0,
            sol_usd_now=150.0,
        )
        assert result == {}, f"expected empty (skipped), got {result}"
        assert executor.sells == [], f"phantom SL should NOT have sold, got {executor.sells}"
    asyncio.run(go())


def test_sl_fires_at_minus_50pct_in_usd():
    """SL fires when current_price_usd is at entry × (1 - 0.5) = 50% drop."""
    async def go():
        executor = FakeExecutor(oracle_price_usd=0.00005)  # 50% drop from 0.0001
        pm = FakePM()
        position = make_position(entry_price=0.0001)
        result = await _process_position(
            position=position,
            executor=executor,
            position_manager=pm,
            is_paper=False,
            stop_loss_pct=50.0,
            tp1_mult=1.5,
            tp2_mult=2.0,
            extreme_tp_mult=5.0,
            tp1_pct=33.0,
            trailing_pct=20.0,
            min_hold_seconds=60,
            time_limit=0,
            sol_usd_now=150.0,
        )
        assert executor.sells, f"SL should have fired, got {executor.sells}"
        assert executor.sells[0][2] == "SL", f"expected SL, got {executor.sells}"
    asyncio.run(go())


def test_30pct_drop_does_not_trigger_sl():
    """A 30% drop (above 5% threshold, below 50% SL) is just monitored."""
    async def go():
        executor = FakeExecutor(oracle_price_usd=0.00007)  # 30% drop
        pm = FakePM()
        position = make_position(entry_price=0.0001)
        result = await _process_position(
            position=position,
            executor=executor,
            position_manager=pm,
            is_paper=False,
            stop_loss_pct=50.0,
            tp1_mult=1.5,
            tp2_mult=2.0,
            extreme_tp_mult=5.0,
            tp1_pct=33.0,
            trailing_pct=20.0,
            min_hold_seconds=60,
            time_limit=0,
            sol_usd_now=150.0,
        )
        assert executor.sells == [], f"30% drop should not trigger SL, got {executor.sells}"
    asyncio.run(go())


def test_tp1_fires_at_1_5x_in_usd():
    """TP1 fires when current_price_usd reaches 1.5× entry."""
    async def go():
        # Use 0.000151 to avoid float precision edge case (0.0001 * 1.5 = 0.00015000000000000001)
        executor = FakeExecutor(oracle_price_usd=0.000151)  # 1.51x (above 1.5x threshold)
        pm = FakePM()
        position = make_position(entry_price=0.0001)
        result = await _process_position(
            position=position,
            executor=executor,
            position_manager=pm,
            is_paper=False,
            stop_loss_pct=50.0,
            tp1_mult=1.5,
            tp2_mult=2.0,
            extreme_tp_mult=5.0,
            tp1_pct=33.0,
            trailing_pct=20.0,
            min_hold_seconds=0,  # skip warmup
            time_limit=0,
            sol_usd_now=150.0,
        )
        assert executor.sells, f"TP1 should have fired, got sells={executor.sells} result={result}"
        assert executor.sells[0][2].startswith("TP1"), f"expected TP1*, got {executor.sells}"
    asyncio.run(go())


def test_phantom_sl_threshold_is_5pct():
    """Verify PHANTOM_SL_THRESHOLD = 0.05."""
    assert PHANTOM_SL_THRESHOLD == 0.05, f"threshold changed: {PHANTOM_SL_THRESHOLD}"
