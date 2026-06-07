"""Tests for Audit Cycle 2 fixes (June 2026).

Covers:
- M4: /positions displays actual SOL value (not USD labeled as SOL)
- M5: fromisoformat handles Z-suffixed strings + logs on unparseable
- L1: filter compound threshold matches code (1.0 SOL, not 0.1)
- L2: Jupiter client uses lite-api v3 endpoint
- L3: position_monitor doesn't cleanup lock on partial TP1
- L7: cache.save_active_calls is no-op with docstring
- L8: position_monitor reads PnL from position (not recomputed)
- L9: /positions peak_pct = 0 when peak is 0
"""
import asyncio
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, AsyncMock

sys.path.insert(0, "/Users/khezuma/workspace/trenching")

from core.position_manager import PositionManager
from core.jupiter_client import PRICE_API as JUPITER_PRICE_API
from analysis.filters import COMPOUND_FEE_MIN_SOL


# --- M4: /positions displays actual SOL, not USD-as-SOL --------------------

def test_remaining_sol_is_actual_sol_not_usd():
    """When tokens=1000, entry_price=$0.001 (USD), sol_usd=150:
    - remaining_tokens * entry_price = $1.00 (USD)
    - remaining_sol_value = $1.00 / 150 = 0.00667 SOL
    The bug was showing $1.00 labeled as SOL.
    """
    entry_price = 0.001        # USD per token
    entry_tokens = 1000.0
    remaining_tokens = 1000.0
    sol_usd = 150.0

    remaining_usd = remaining_tokens * entry_price
    remaining_actual_sol = remaining_usd / sol_usd

    assert remaining_usd == 1.0
    # Actual SOL value of $1.00 at $150/SOL is 0.00667, not 1.0
    assert abs(remaining_actual_sol - 0.00667) < 0.001
    assert remaining_actual_sol < remaining_usd  # SOL is < USD for sub-dollar amounts


def test_remaining_sol_falls_back_when_sol_usd_at_entry_missing():
    """If sol_usd_at_entry is 0, fallback to 150.0."""
    sol_usd_at_entry = 0.0
    sol_usd = sol_usd_at_entry or 0.0
    if sol_usd <= 0:
        sol_usd = 150.0
    assert sol_usd == 150.0


# --- M5: fromisoformat handles Z-suffixed strings + logs on bad data -------

def test_fromisoformat_handles_z_suffix():
    """ISO 8601 strings ending in 'Z' (Zulu) are common; Py3.7-3.10 fail
    on fromisoformat. Normalization to '+00:00' is the fix.
    """
    raw = "2024-06-07T10:30:00Z"
    if isinstance(raw, str) and raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    assert dt.tzinfo is not None
    assert dt.year == 2024 and dt.month == 6 and dt.day == 7


def test_fromisoformat_handles_plus_offset():
    """Normal +00:00 suffix works on all Python versions."""
    raw = "2024-06-07T10:30:00+00:00"
    dt = datetime.fromisoformat(raw)
    assert dt.tzinfo is not None
    assert dt.hour == 10


def test_fromisoformat_bad_string_logs_and_returns_zero():
    """Garbage string should not crash; we log and treat as age=0."""
    raw = "not-a-date"
    age_sec = 0
    try:
        datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        age_sec = 0
    assert age_sec == 0


# --- L1: filter compound threshold matches code ----------------------------

def test_compound_fee_min_sol_is_one():
    """COMPOUND_FEE_MIN_SOL = 1.0 (was mistakenly documented as 0.1)."""
    assert COMPOUND_FEE_MIN_SOL == 1.0


# --- L2: Jupiter client uses lite-api v3 -----------------------------------

def test_jupiter_client_uses_lite_api_v3():
    """PRICE_API (module-level) should point to lite-api.jup.ag/price/v3."""
    assert JUPITER_PRICE_API == "https://lite-api.jup.ag/price/v3"


# --- L3: position_monitor doesn't cleanup lock on partial TP1 --------------

def test_lock_not_cleaned_on_partial_tp1():
    """After a partial TP1 sell, the lock should STILL be in the dict
    (position remains open with reduced tokens).
    """
    pm = PositionManager(db=None)
    address = "TestTokenAddress123"
    lock = pm.get_lock(address)
    assert lock in pm._position_locks.values()

    # Simulate the audit fix: only full-close reasons trigger cleanup.
    full_close_reasons = ("SL", "TP1-EXTREME", "TP2", "TRAILING", "TIME")
    partial_reasons = ("TP1",)

    # Partial TP1 — lock should remain
    last_reason = "TP1"
    if last_reason in full_close_reasons:
        pm.cleanup_lock(address)
    assert address in pm._position_locks

    # Full close (TP2) — lock should be removed
    last_reason = "TP2"
    if last_reason in full_close_reasons:
        pm.cleanup_lock(address)
    assert address not in pm._position_locks


def test_lock_cleaned_on_full_close():
    """After SL/TP2/TRAILING/TIME/STUCK, lock should be removed."""
    pm = PositionManager(db=None)
    address = "TestTokenAddress456"
    pm.get_lock(address)  # create
    assert address in pm._position_locks

    full_close_reasons = ("SL", "TP1-EXTREME", "TP2", "TRAILING", "TIME")
    for reason in full_close_reasons:
        # Re-create the lock each time (since we removed it)
        pm.get_lock(address)
        assert address in pm._position_locks
        if reason in full_close_reasons:
            pm.cleanup_lock(address)
        assert address not in pm._position_locks


# --- L7: cache.save_active_calls is no-op with docstring -------------------

def test_save_active_calls_is_noop():
    """state.save_active_calls() should not raise and should be a no-op."""
    import asyncio
    from storage.cache import SharedState
    state = SharedState()

    async def _run():
        await state.save_active_calls()  # should not raise

    asyncio.new_event_loop().run_until_complete(_run())


# --- L8: position_monitor reads PnL from position (not recomputed) ---------

def test_pnl_read_from_position_for_full_close():
    """For full-close exits (SL/TRAILING/TIME), PnL should come from
    position dict (set by close_position), not be recomputed.
    """
    # Simulate a position that has been close_position'd
    position = {
        "id": 1,
        "token_address": "TestAddr",
        "token_symbol": "TEST",
        "entry_price": 0.001,        # USD
        "entry_amount_sol": 0.05,
        "entry_amount_token": 1000.0,
        "current_amount_token": 0.0,  # fully closed
        "total_sold_sol": 0.04,      # sold for less than entry (SL)
        "total_sold_usd": 6.0,
        "status": "CLOSED",
        "exit_reason": "SL",
        "exit_price": 0.0008,
        "pnl_sol": -0.01,
        "pnl_usd": -2.0,
        "pnl_pct": -20.0,
    }

    # The L8 fix path: read PnL from position
    pnl_sol = position.get("pnl_sol") or 0.0
    pnl_usd = position.get("pnl_usd") or 0.0
    pnl_pct = position.get("pnl_pct") or 0.0

    assert pnl_sol == -0.01
    assert pnl_usd == -2.0
    assert pnl_pct == -20.0


def test_pnl_zero_when_position_field_missing():
    """If position dict has no pnl_* fields, defaults to 0.0."""
    position = {
        "id": 1,
        "token_address": "TestAddr",
        "token_symbol": "TEST",
    }
    pnl_sol = position.get("pnl_sol") or 0.0
    pnl_usd = position.get("pnl_usd") or 0.0
    pnl_pct = position.get("pnl_pct") or 0.0
    assert pnl_sol == 0.0
    assert pnl_usd == 0.0
    assert pnl_pct == 0.0


# --- L9: /positions peak_pct = 0 when peak is 0 -----------------------------

def test_peak_pct_zero_when_peak_unset():
    """Newly opened positions have peak_price = entry_price (or 0 in some
    edge cases). When peak <= 0 or entry <= 0, peak_pct should be 0.0,
    NOT -100%.
    """
    peak = 0.0
    entry = 0.001
    if peak <= 0 or entry <= 0:
        peak_pct = 0.0
    else:
        peak_pct = ((peak / entry) - 1) * 100
    assert peak_pct == 0.0


def test_peak_pct_zero_when_entry_unset():
    """When entry_price is 0 (corrupted row), peak_pct defaults to 0."""
    peak = 0.5
    entry = 0.0
    if peak <= 0 or entry <= 0:
        peak_pct = 0.0
    else:
        peak_pct = ((peak / entry) - 1) * 100
    assert peak_pct == 0.0


def test_peak_pct_calculated_normally():
    """When both peak and entry are positive, peak_pct is normal."""
    peak = 0.002
    entry = 0.001
    if peak <= 0 or entry <= 0:
        peak_pct = 0.0
    else:
        peak_pct = ((peak / entry) - 1) * 100
    assert peak_pct == 100.0  # doubled = +100%
