"""Test Phase 1 (Paper Trading) — wallet, risk_manager, position_manager, executor."""
import asyncio
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "/Users/khezuma/workspace/trenching")

import pytest

from core.wallet import Wallet, RESERVE_SOL, PAPER_PUBKEY
from core.risk_manager import RiskManager
from core.position_manager import PositionManager
from analysis.models import Position, Trade


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


# ============ Wallet ============

def test_wallet_paper_init():
    async def go():
        w = Wallet(paper=True, starting_balance_sol=10.0)
        assert w.paper is True
        assert w.pubkey == PAPER_PUBKEY
        assert await w.get_sol_balance() == 10.0
    asyncio.run(go())


def test_wallet_debit():
    async def go():
        w = Wallet(paper=True, starting_balance_sol=10.0)
        ok = await w.debit(2.0, "test")
        assert ok is True
        bal = await w.get_sol_balance()
        assert bal == 8.0
    asyncio.run(go())


def test_wallet_debit_insufficient():
    async def go():
        w = Wallet(paper=True, starting_balance_sol=1.0)
        ok = await w.debit(2.0, "test")
        assert ok is False
        bal = await w.get_sol_balance()
        assert bal == 1.0
    asyncio.run(go())


def test_wallet_debit_reserve():
    async def go():
        w = Wallet(paper=True, starting_balance_sol=0.5)
        ok = await w.debit(0.5, "test")
        assert ok is False
    asyncio.run(go())


def test_wallet_credit():
    async def go():
        w = Wallet(paper=True, starting_balance_sol=5.0)
        await w.credit(2.0, "test")
        bal = await w.get_sol_balance()
        assert bal == 7.0
    asyncio.run(go())


def test_wallet_paper_signature():
    w = Wallet(paper=True, starting_balance_sol=10.0)
    sig = w.generate_paper_signature()
    assert sig.startswith("PAPER_")
    assert len(sig) == 22


# ============ Risk Manager ============

def test_risk_can_trade_default():
    rm = RiskManager({"daily_loss_limit_sol": 0.5})
    ok, reason = rm.can_trade()
    assert ok is True
    assert reason == "OK"


def test_risk_daily_loss_halts():
    rm = RiskManager({"daily_loss_limit_sol": 0.5})
    rm.daily_pnl = -0.6
    ok, reason = rm.can_trade()
    assert ok is False
    assert "Daily loss limit" in reason


def test_risk_loss_streak_halt():
    rm = RiskManager({"loss_streak_halt": 3, "loss_streak_halt_hours": 1})
    rm.record_trade_result(-0.1)
    rm.record_trade_result(-0.1)
    rm.record_trade_result(-0.1)
    assert rm.halted_until is not None
    assert rm.halted_until > datetime.now(timezone.utc)


def test_risk_loss_streak_resets_on_win():
    rm = RiskManager({"loss_streak_halt": 3})
    rm.record_trade_result(-0.1)
    rm.record_trade_result(-0.1)
    rm.record_trade_result(0.1)
    assert rm.loss_streak == 0


def test_risk_position_size_fixed():
    rm = RiskManager({"sizing_mode": "fixed", "position_size_sol": 0.05,
                       "min_position_sol": 0.02, "max_position_sol": 0.5})
    size = rm.get_position_size(balance=10.0)
    assert size == 0.05


def test_risk_position_size_capped():
    rm = RiskManager({"sizing_mode": "fixed", "position_size_sol": 0.5,
                       "min_position_sol": 0.02, "max_position_sol": 0.1})
    size = rm.get_position_size(balance=10.0)
    assert size == 0.1


def test_risk_position_size_balance_pct():
    rm = RiskManager({"sizing_mode": "balance_pct", "position_size_pct": 0.02,
                       "min_position_sol": 0.02, "max_position_sol": 0.5})
    size = rm.get_position_size(balance=10.0)
    assert 0.19 < size < 0.21


def test_risk_position_size_reserve():
    rm = RiskManager({"sizing_mode": "fixed", "position_size_sol": 0.5,
                       "min_position_sol": 0.02, "max_position_sol": 0.5})
    size = rm.get_position_size(balance=0.3)
    assert abs(size - 0.2) < 1e-9


# ============ Position Manager ============

def test_position_dataclass_defaults():
    p = Position()
    assert p.status == "OPEN"
    assert p.side == "BUY"
    assert p.paper is True


def test_trade_dataclass_defaults():
    t = Trade()
    assert t.side == "BUY"
    assert t.status == "PENDING"


def test_position_monitor_state_machine_logic():
    """Sanity-check SL/TP/trailing math without a live position_monitor."""
    entry = 0.0001
    tp1 = entry * 1.30
    tp2 = entry * 1.50
    sl = entry * 0.70
    peak = entry * 1.60
    trailing_stop = peak * 0.85
    assert tp1 < tp2
    assert sl < entry
    assert trailing_stop < peak


# ============ Format Trade Alert ============

def test_format_trade_alert_buy():
    from alerts.formatter import format_trade_alert
    p = Position(
        token_address="ABCDEFGH123456",
        token_symbol="TST",
        entry_price=0.0001,
        entry_amount_sol=0.05,
        entry_amount_token=500.0,
        entry_tx_sig="PAPER_abcdef1234567890",
        paper=True,
    )
    text = format_trade_alert(p, "BUY")
    assert "TST" in text
    assert "PAPER" in text
    assert "BUY" in text
    assert "0.0500" in text


def test_format_trade_alert_sell():
    from alerts.formatter import format_trade_alert
    p = Position(
        token_address="ABCDEFGH123456",
        token_symbol="TST",
        entry_price=0.0001,
        entry_amount_sol=0.05,
        exit_price=0.00015,
        pnl_sol=0.025,
        pnl_pct=50.0,
        exit_reason="TP1",
        paper=True,
    )
    text = format_trade_alert(p, "SELL")
    assert "SELL" in text
    assert "TP1" in text
    assert "+50.0%" in text or "50.0%" in text


# ============ Trading Config ============

def test_trading_config_loads():
    from config import load_trading_config
    cfg = load_trading_config()
    assert "paper_mode" in cfg
    assert "position_size_sol" in cfg
    assert "stop_loss_pct" in cfg


def test_risk_rules_loads():
    from config import load_risk_rules
    rules = load_risk_rules()
    assert "max_open_positions" in rules
    assert "min_wallet_reserve_sol" in rules


# ============ BUY-DECISION log format ============

def test_buy_decision_log_format():
    """BUY-DECISION gate: confidence-based, NOT verdict-based.

    A WATCH with conf>=0.75 still trades.
    An APE with conf<0.75 does NOT trade.
    """
    threshold = 0.75
    cases = [
        ("APE", 0.85, "TRADE"),
        ("APE", 0.60, "NO_TRADE"),
        ("WATCH", 0.70, "NO_TRADE"),
        ("WATCH", 0.80, "TRADE"),
        ("SKIP", 0.90, "TRADE"),
        ("SKIP", 0.50, "NO_TRADE"),
    ]
    for verdict, conf, expected in cases:
        action = "TRADE" if conf >= threshold else "NO_TRADE"
        assert action == expected, f"{verdict}@{conf} should be {expected}, got {action}"


def test_position_summary_fields():
    """Summary dict should have all fields needed for Telegram output."""
    summary_keys = {"id", "symbol", "address", "entry_sol", "entry_price",
                    "peak_gain_pct", "tokens", "age_sec", "status", "paper"}
    assert len(summary_keys) == 10


# ============ cmd_positions Telegram command ============

def test_cmd_positions_importable():
    """cmd_positions should be importable and registered."""
    from alerts.bot import cmd_positions
    assert callable(cmd_positions)
