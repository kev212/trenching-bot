"""Tests for RiskManager: position limits, daily trade limits."""
import asyncio
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, "/Users/khezuma/workspace/trenching")

from core.risk_manager import RiskManager


def make_risk(risk_rules=None, daily_trades=0, daily_pnl=0.0):
    rm = RiskManager(
        config={"daily_loss_limit_sol": 0.5},
        db=None,
        risk_rules=risk_rules or {},
        position_manager=None,
    )
    rm.daily_trades = daily_trades
    rm.daily_pnl = daily_pnl
    return rm


class TestMaxOpenPositions:
    def test_no_limit_allows_any_count(self):
        rm = make_risk(risk_rules={})
        ok, _ = rm.can_trade(open_position_count=100)
        assert ok

    def test_blocks_at_limit(self):
        rm = make_risk(risk_rules={"max_open_positions": 5})
        ok, reason = rm.can_trade(open_position_count=5)
        assert not ok
        assert "5/5" in reason

    def test_blocks_above_limit(self):
        rm = make_risk(risk_rules={"max_open_positions": 5})
        ok, _ = rm.can_trade(open_position_count=10)
        assert not ok

    def test_allows_below_limit(self):
        rm = make_risk(risk_rules={"max_open_positions": 5})
        ok, _ = rm.can_trade(open_position_count=3)
        assert ok


class TestMaxDailyTrades:
    def test_no_limit_allows(self):
        rm = make_risk(risk_rules={}, daily_trades=100)
        ok, _ = rm.can_trade(open_position_count=0)
        assert ok

    def test_blocks_at_limit(self):
        rm = make_risk(risk_rules={"max_daily_trades": 20}, daily_trades=20)
        ok, reason = rm.can_trade(open_position_count=0)
        assert not ok
        assert "20/20" in reason

    def test_allows_below_limit(self):
        rm = make_risk(risk_rules={"max_daily_trades": 20}, daily_trades=5)
        ok, _ = rm.can_trade(open_position_count=0)
        assert ok


class TestRecordTradeOpen:
    def test_increments_counter(self):
        rm = make_risk()
        rm.record_trade_open()
        rm.record_trade_open()
        rm.record_trade_open()
        assert rm.daily_trades == 3

    def test_daily_reset(self):
        rm = make_risk(daily_trades=5)
        # Force a date in the past
        from datetime import timedelta
        rm.last_reset_date = (datetime.now(timezone.utc) - timedelta(days=1)).date()
        rm.record_trade_open()
        # Should reset to 0 first, then increment
        assert rm.daily_trades == 1


class TestCombinedGates:
    def test_daily_loss_still_works(self):
        rm = make_risk(
            risk_rules={"max_open_positions": 5, "max_daily_trades": 20},
            daily_pnl=-0.6,
        )
        ok, reason = rm.can_trade(open_position_count=0)
        assert not ok
        assert "Daily loss" in reason

    def test_loss_streak_still_works(self):
        from datetime import timedelta
        rm = make_risk(risk_rules={"max_open_positions": 5, "max_daily_trades": 20})
        rm.halted_until = datetime.now(timezone.utc) + timedelta(hours=1)
        ok, reason = rm.can_trade(open_position_count=0)
        assert not ok
        assert "Halted" in reason

    def test_all_gates_pass(self):
        rm = make_risk(
            risk_rules={"max_open_positions": 5, "max_daily_trades": 20},
        )
        ok, reason = rm.can_trade(open_position_count=2)
        assert ok
        assert reason == "OK"
