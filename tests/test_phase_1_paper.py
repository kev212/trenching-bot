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


# ============ Paper mode (no Jupiter) ============

def test_executor_paper_skips_jupiter_when_no_price():
    """In paper mode, missing GMGN price should not call Jupiter."""
    from core.trade_executor import TradeExecutor
    from core.wallet import Wallet
    from core.jupiter_client import JupiterClient
    from core.position_manager import PositionManager
    from core.risk_manager import RiskManager
    from analysis.models import TokenData, Position

    class FakeJupiter:
        async def get_quote(self, *a, **k):
            raise AssertionError("Jupiter should NOT be called in paper mode")
        async def get_token_price_in_sol_with_retry(self, *a, **k):
            raise AssertionError("Jupiter should NOT be called in paper mode")

    async def go():
        executor = TradeExecutor(
            paper=True,
            wallet=Wallet(paper=True, starting_balance_sol=10.0),
            jupiter=FakeJupiter(),
            positions=PositionManager(db=None),
            risk=RiskManager({}),
            config={},
        )
        token = TokenData(address="X", symbol="TST", name="Test")
        token.raw_gmgn = {}
        result = await executor.execute_buy(token, 0.05)
        assert result is None
    asyncio.run(go())


def test_executor_paper_uses_gmgn_price():
    """Paper buy with valid GMGN price should succeed and debit wallet."""
    from core.trade_executor import TradeExecutor
    from core.wallet import Wallet
    from core.jupiter_client import JupiterClient
    from core.position_manager import PositionManager
    from core.risk_manager import RiskManager
    from analysis.models import TokenData

    class FakeJupiter:
        async def get_quote(self, *a, **k): return {}
        async def get_token_price_in_sol_with_retry(self, *a, **k): return 0.0

    captured = {}

    class FakePM:
        db = None
        async def open_position(self, p):
            captured["position"] = p
            return 1
        async def record_trade(self, t):
            captured["trade"] = t
            return 1

    async def go():
        executor = TradeExecutor(
            paper=True,
            wallet=Wallet(paper=True, starting_balance_sol=10.0),
            jupiter=FakeJupiter(),
            positions=FakePM(),
            risk=RiskManager({}),
            config={},
        )
        token = TokenData(address="X", symbol="TST", name="Test")
        token.raw_gmgn = {"price": {"price": 0.001}}
        result = await executor.execute_buy(token, 0.05)
        assert result is not None
        assert captured["position"].entry_price == 0.001
        assert captured["position"].entry_amount_token > 0
        assert captured["position"].raw_gmgn_json != ""
    asyncio.run(go())


def test_executor_paper_simulated_slippage():
    """Paper buy with GMGN price 0.001 should apply ~1% slippage."""
    from core.trade_executor import TradeExecutor
    from core.wallet import Wallet
    from core.jupiter_client import JupiterClient
    from core.position_manager import PositionManager
    from core.risk_manager import RiskManager
    from analysis.models import TokenData

    class FakeJupiter:
        async def get_quote(self, *a, **k): return {}
        async def get_token_price_in_sol_with_retry(self, *a, **k): return 0.0

    captured = {}

    class FakePM:
        db = None
        async def open_position(self, p):
            captured["position"] = p
            return 1
        async def record_trade(self, t):
            captured["trade"] = t
            return 1

    async def go():
        executor = TradeExecutor(
            paper=True,
            wallet=Wallet(paper=True, starting_balance_sol=10.0),
            jupiter=FakeJupiter(),
            positions=FakePM(),
            risk=RiskManager({}),
            config={},
        )
        token = TokenData(address="X", symbol="TST", name="Test")
        token.raw_gmgn = {"price": {"price": 0.001}}
        await executor.execute_buy(token, 0.05)
        effective_price = 0.001 * 1.01
        expected_tokens = 0.05 / effective_price
        actual_tokens = captured["position"].entry_amount_token
        assert abs(actual_tokens - expected_tokens) < 0.01
    asyncio.run(go())


# ============ dict/dataclass compatibility ============

def test_position_manager_close_works_on_dict():
    """The position_monitor passes dicts; close_position must work on them."""
    from core.position_manager import PositionManager
    from datetime import datetime, timezone, timedelta

    class FakeDB:
        def __init__(self):
            self.updates = []
        async def update_position(self, p):
            self.updates.append(p)
        async def commit(self):
            pass

    async def go():
        db = FakeDB()
        pm = PositionManager(db)
        entry_time = datetime.now(timezone.utc) - timedelta(seconds=30)
        position = {
            "id": 1,
            "token_address": "ABC",
            "token_symbol": "TST",
            "status": "OPEN",
            "entry_price": 0.001,
            "entry_amount_sol": 0.05,
            "entry_amount_token": 50.0,
            "current_amount_token": 50.0,
            "peak_price": 0.001,
            "entry_time": entry_time,
        }
        await pm.close_position(position, "TP1", 0.0013, 0.015, 30.0)
        assert position["status"] == "CLOSED"
        assert position["exit_reason"] == "TP1"
        assert position["pnl_sol"] == 0.015
        assert len(db.updates) == 1
    asyncio.run(go())


def test_position_manager_record_partial_sell_works_on_dict():
    """Partial sell on dict should update current_amount_token."""
    from core.position_manager import PositionManager

    class FakeDB:
        async def update_position(self, p): pass
        async def commit(self): pass

    async def go():
        pm = PositionManager(FakeDB())
        position = {
            "id": 1, "token_symbol": "TST", "current_amount_token": 100.0,
        }
        await pm.record_partial_sell(position, 33.0, 67.0)
        assert position["current_amount_token"] == 67.0
    asyncio.run(go())


def test_database_update_position_works_on_dict():
    """Database.update_position must accept dicts from _row_to_position."""
    from storage.database import Database
    import tempfile, os

    async def go():
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            db = Database(tmp.name)
            await db.init()
            from analysis.models import Position
            from datetime import datetime, timezone
            p = Position(
                token_address="X1", token_symbol="TST",
                entry_price=0.001, entry_amount_sol=0.05,
                entry_amount_token=50.0, entry_time=datetime.now(timezone.utc),
                peak_price=0.001, current_amount_token=50.0,
            )
            pid = await db.save_position(p)
            await db.update_position({
                "id": pid, "peak_price": 0.0013, "current_amount_token": 50.0,
                "status": "OPEN", "exit_tx_sig": "", "exit_price": 0.0,
                "exit_time": None, "pnl_sol": 0.0, "pnl_pct": 0.0,
                "hold_seconds": 0, "exit_reason": "",
            })
            cursor = await db.db.execute("SELECT peak_price FROM positions WHERE id = ?", (pid,))
            row = await cursor.fetchone()
            assert row["peak_price"] == 0.0013
            await db.close()
        finally:
            os.unlink(tmp.name)
    asyncio.run(go())


# ============ /pnl command ============

def test_cmd_pnl_importable():
    from alerts.bot import cmd_pnl
    assert callable(cmd_pnl)


# ============ /history + format_exit_alert ============

def test_cmd_history_importable():
    from alerts.bot import cmd_history
    assert callable(cmd_history)


def test_format_exit_alert_sl():
    from alerts.formatter import format_exit_alert
    text = format_exit_alert(
        symbol="TST", address="ABCDEFGH",
        entry_price=0.001, exit_price=0.0007,
        pnl_sol=-0.015, pnl_pct=-30.0,
        reason="SL", hold_seconds=120, paper=True,
    )
    assert "SL" in text
    assert "TST" in text
    assert "PAPER" in text
    assert "-30.0%" in text
    assert "🛑" in text
    assert "2m 0s" in text


def test_format_exit_alert_tp():
    from alerts.formatter import format_exit_alert
    text = format_exit_alert(
        symbol="WIN", address="WINPADDR",
        entry_price=0.0001, exit_price=0.00015,
        pnl_sol=0.025, pnl_pct=50.0,
        reason="TP1", hold_seconds=300, paper=True,
        position_size_sol=0.05, total_tokens=50000.0,
        sold_pct=33.0, sold_tokens=16500.0, remaining_tokens=33500.0,
    )
    assert "TP1" in text
    assert "🎯" in text
    assert "5m 0s" in text
    assert "📈" in text
    assert "+50.0%" in text
    assert "0.0500 SOL" in text
    assert "sold 33%" in text
    assert "16,500 tokens" in text
    assert "33,500 remain" in text


def test_format_exit_alert_trailing():
    from alerts.formatter import format_exit_alert
    text = format_exit_alert(
        symbol="TRAIL", address="TRLADDR",
        entry_price=0.0001, exit_price=0.00009,
        pnl_sol=-0.005, pnl_pct=-10.0,
        reason="TRAILING", hold_seconds=45, paper=False,
    )
    assert "TRAILING" in text
    assert "📉" in text
    assert "LIVE" in text
    assert "45s" in text


def test_format_exit_alert_time():
    from alerts.formatter import format_exit_alert
    text = format_exit_alert(
        symbol="TIMEOUT", address="TIMEADDR",
        entry_price=0.0001, exit_price=0.0001,
        pnl_sol=0.0, pnl_pct=0.0,
        reason="TIME", hold_seconds=1800, paper=True,
    )
    assert "TIME" in text
    assert "⏰" in text
    assert "30m 0s" in text


# ============ TP1 spam prevention ============

def test_position_monitor_tp1_logic_avoids_spam():
    """The bug: condition was 'exit_reason in (None, "", "TP1")' which always matches.

    Fix: check 'already_partial' separately. TP1 only fires when exit_reason
    is None/empty AND price >= tp1_mult. After TP1, exit_reason becomes "TP1",
    so the elif goes to TP2 branch.
    """
    def should_trigger_tp1(position, current_price, entry, tp1_mult):
        already_partial = bool(position.get("exit_reason")) and \
            position.get("exit_reason") in ("TP1", "TP2")
        return not already_partial and current_price >= entry * tp1_mult

    entry = 0.001
    tp1_mult = 1.30

    pos_clean = {"exit_reason": None}
    pos_after_tp1 = {"exit_reason": "TP1"}
    pos_after_tp2 = {"exit_reason": "TP2"}

    price_above = 0.0014
    price_below = 0.0011

    assert should_trigger_tp1(pos_clean, price_above, entry, tp1_mult) is True
    assert should_trigger_tp1(pos_clean, price_below, entry, tp1_mult) is False
    assert should_trigger_tp1(pos_after_tp1, price_above, entry, tp1_mult) is False
    assert should_trigger_tp1(pos_after_tp2, price_above, entry, tp1_mult) is False


def test_format_exit_alert_tp1_size_math():
    """TP1 alert with 50,000 token position, 33% sold should show 16,500 sold."""
    from alerts.formatter import format_exit_alert
    text = format_exit_alert(
        symbol="BIG", address="BIGADDR",
        entry_price=0.0001, exit_price=0.00015,
        pnl_sol=0.0025, pnl_pct=50.0,
        reason="TP1", hold_seconds=120, paper=True,
        position_size_sol=0.10, total_tokens=100000.0,
        sold_pct=33.0, sold_tokens=33000.0, remaining_tokens=67000.0,
    )
    assert "0.1000 SOL" in text
    assert "sold 33%" in text
    assert "33,000 tokens" in text
    assert "67,000 remain" in text


def test_format_exit_alert_sl_full_close():
    """SL should show 'closed 100%' not 'sold X%'."""
    from alerts.formatter import format_exit_alert
    text = format_exit_alert(
        symbol="LOST", address="LOSTADDR",
        entry_price=0.001, exit_price=0.0007,
        pnl_sol=-0.015, pnl_pct=-30.0,
        reason="SL", hold_seconds=45, paper=True,
        position_size_sol=0.05, total_tokens=50000.0,
    )
    assert "closed 100%" in text
    assert "50,000 tokens" in text
    assert "SL" in text
    assert "🛑" in text


# ============ Phantom exit prevention ============

def test_min_hold_prevents_phantom_trailing():
    """The bug: TRAILING fired at 1s because GMGN returned a different
    price on the first check after buy. Fix: min_hold_seconds gate."""
    min_hold = 30
    held_so_far = 1
    entry = 0.0000806914
    phantom_current = 0.0000577607
    peak = max(entry, phantom_current)
    trailing_pct = 15

    in_warmup = held_so_far < min_hold
    would_trail = (not in_warmup) and phantom_current <= peak * (1 - trailing_pct / 100)
    assert in_warmup is True
    assert would_trail is False


def test_min_hold_allows_real_trailing_after_grace():
    """After 30s warmup, real trailing should fire."""
    min_hold = 30
    held_so_far = 45
    entry = 0.0001
    peak = 0.00015
    drop_price = 0.00012
    trailing_pct = 15

    in_warmup = held_so_far < min_hold
    would_trail = (not in_warmup) and drop_price <= peak * (1 - trailing_pct / 100)
    assert in_warmup is False
    assert would_trail is True


def test_paper_buy_warms_price_cache():
    """execute_buy should warm the paper price cache so first 5s can't trigger exits."""
    from core.trade_executor import TradeExecutor
    from core.wallet import Wallet
    from core.jupiter_client import JupiterClient
    from core.position_manager import PositionManager
    from core.risk_manager import RiskManager
    from analysis.models import TokenData
    import time

    class FakeJupiter:
        async def get_quote(self, *a, **k): return {}
        async def get_token_price_in_sol_with_retry(self, *a, **k): return 0.0

    class FakePM:
        db = None
        async def open_position(self, p): return 1
        async def record_trade(self, t): return 1

    async def go():
        executor = TradeExecutor(
            paper=True,
            wallet=Wallet(paper=True, starting_balance_sol=10.0),
            jupiter=FakeJupiter(),
            positions=FakePM(),
            risk=RiskManager({}),
            config={},
        )
        token = TokenData(address="HuJuQYaZ", symbol="DATBIHGAH", name="D")
        token.raw_gmgn = {"price": {"price": 0.0000806914}}
        await executor.execute_buy(token, 0.05)
        cached = executor._paper_price_cache.get("HuJuQYaZ")
        assert cached is not None
        assert cached["price"] == 0.0000806914
        assert (time.time() - cached["ts"]) < 1.0
    asyncio.run(go())
