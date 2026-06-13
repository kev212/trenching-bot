"""Test live-buy wiring in TrenchingBot._maybe_execute_live_buy.

Verifies the full guard chain (paper_mode, verdict, confidence, gmgn_cli,
risk_manager, balance, size) and executor.execute_buy() invocation.

Also tests post-execution Telegram alerts (✅ EXECUTED / ❌ BLOCKED).
"""
import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, "/Users/khezuma/workspace/trenching")

import pytest

from analysis.models import Position, TokenData, Verdict


def make_decision(verdict=Verdict.APE, conf=0.85, score=0.7):
    d = MagicMock()
    d.verdict = verdict
    d.confidence = conf
    d.score = score
    d.reasoning = "test"
    d.key_factors = []
    d.processing_time_ms = 100
    return d


def make_token(symbol="TEST", address="Tok1111111111111111111111111111111111111111"):
    t = MagicMock(spec=TokenData)
    t.symbol = symbol
    t.address = address
    return t


class FakeBot:
    """Minimal stand-in for TrenchingBot that exposes _maybe_execute_live_buy.

    Replaces the full TrenchingBot init (which requires db, jupiter, twitter, etc.)
    with just the attributes _maybe_execute_live_buy needs.
    """

    def __init__(self, paper_mode=False, gmgn_cli=None, balance=0.5,
                 executor=None, risk_manager=None, min_size=0.02, fixed_size=0.05,
                 confidence_threshold=0.60, open_positions=None):
        self.paper_mode = paper_mode
        self._live_paused = False
        self.gmgn_cli = gmgn_cli
        self.executor = executor or MagicMock()
        self.executor.execute_buy = AsyncMock(return_value=MagicMock(id=42))
        self.state = MagicMock()
        self.state.get_filter_version = AsyncMock(return_value=1)
        if risk_manager is None:
            risk_manager = MagicMock()
            risk_manager.can_trade = MagicMock(return_value=(True, "OK"))
            risk_manager.get_position_size = MagicMock(return_value=fixed_size)
        self.risk_manager = risk_manager
        self.position_manager = MagicMock()
        self.position_manager.get_open_positions = AsyncMock(
            return_value=open_positions or []
        )
        self.trading_config = {
            "min_position_sol": min_size,
            "position_size_sol": fixed_size,
        }
        self._balance = balance
        if gmgn_cli is not None:
            gmgn_cli.is_ready = MagicMock(return_value=True)
            gmgn_cli.get_sol_balance = AsyncMock(return_value=balance)

    # Bind the real methods
    from main import TrenchingBot
    _maybe_execute_live_buy = TrenchingBot._maybe_execute_live_buy
    _send_post_execution_alert = TrenchingBot._send_post_execution_alert

    async def run(self, decision, address="Tok1111111111111111111111111111111111111111"):
        token = make_token(address=address)
        await self._maybe_execute_live_buy(token, address, decision)
        return self.executor.execute_buy.call_args


class TestPaperMode:
    def test_paper_mode_skips_buy(self):
        bot = FakeBot(paper_mode=True)

        async def go():
            return await bot.run(make_decision())

        asyncio.run(go())
        bot.executor.execute_buy.assert_not_called()


class TestVerdictGate:
    def test_watch_verdict_skipped(self):
        bot = FakeBot(paper_mode=False)

        async def go():
            return await bot.run(make_decision(verdict=Verdict.WATCH))

        asyncio.run(go())
        bot.executor.execute_buy.assert_not_called()

    def test_ape_verdict_proceeds(self):
        gmgn = MagicMock()
        gmgn.is_ready = MagicMock(return_value=True)
        gmgn.get_sol_balance = AsyncMock(return_value=0.5)
        bot = FakeBot(paper_mode=False, gmgn_cli=gmgn)

        async def go():
            return await bot.run(make_decision(verdict=Verdict.APE))

        asyncio.run(go())
        bot.executor.execute_buy.assert_called_once()


class TestConfidenceGate:
    """Gate 3 (conf_auto_execute check) was removed 2026-06-14.

    APE verdict threshold (60) is the only gate. Low confidence does NOT
    block APE attempts anymore.
    """

    def test_low_confidence_proceeds(self):
        """2026-06-14: conf_auto_execute gate removed. APE proceeds regardless of conf."""
        gmgn = MagicMock()
        gmgn.is_ready = MagicMock(return_value=True)
        gmgn.get_sol_balance = AsyncMock(return_value=0.5)
        bot = FakeBot(paper_mode=False, gmgn_cli=gmgn)

        async def go():
            return await bot.run(make_decision(conf=0.50))

        asyncio.run(go())
        # Low conf but APE verdict — buy proceeds (no Gate 3)
        bot.executor.execute_buy.assert_called_once()

    def test_high_confidence_proceeds(self):
        gmgn = MagicMock()
        gmgn.is_ready = MagicMock(return_value=True)
        gmgn.get_sol_balance = AsyncMock(return_value=0.5)
        bot = FakeBot(paper_mode=False, gmgn_cli=gmgn)

        async def go():
            return await bot.run(make_decision(conf=0.80))

        asyncio.run(go())
        bot.executor.execute_buy.assert_called_once()

    def test_very_low_confidence_still_proceeds(self):
        """Edge: conf=0.0 still proceeds if APE verdict (gate 3 is gone)."""
        gmgn = MagicMock()
        gmgn.is_ready = MagicMock(return_value=True)
        gmgn.get_sol_balance = AsyncMock(return_value=0.5)
        bot = FakeBot(paper_mode=False, gmgn_cli=gmgn)

        async def go():
            return await bot.run(make_decision(conf=0.0))

        asyncio.run(go())
        bot.executor.execute_buy.assert_called_once()


class TestGMGNReadyGate:
    def test_gmgn_not_ready_skipped(self):
        bot = FakeBot(paper_mode=False, gmgn_cli=None)

        async def go():
            return await bot.run(make_decision())

        asyncio.run(go())
        bot.executor.execute_buy.assert_not_called()

    def test_gmgn_is_ready_false_skipped(self):
        bot = FakeBot(paper_mode=False, gmgn_cli=MagicMock())
        bot.gmgn_cli.is_ready = MagicMock(return_value=False)

        async def go():
            return await bot.run(make_decision())

        asyncio.run(go())
        bot.executor.execute_buy.assert_not_called()


class TestRiskGate:
    def test_risk_halt_skipped(self):
        gmgn = MagicMock()
        gmgn.is_ready = MagicMock(return_value=True)
        gmgn.get_sol_balance = AsyncMock(return_value=0.5)
        rm = MagicMock()
        rm.can_trade = MagicMock(return_value=(False, "Daily loss limit hit"))
        rm.get_position_size = MagicMock(return_value=0.05)
        bot = FakeBot(paper_mode=False, gmgn_cli=gmgn, risk_manager=rm)

        async def go():
            return await bot.run(make_decision())

        asyncio.run(go())
        bot.executor.execute_buy.assert_not_called()


class TestBalanceGate:
    def test_zero_balance_skipped(self):
        gmgn = MagicMock()
        gmgn.is_ready = MagicMock(return_value=True)
        gmgn.get_sol_balance = AsyncMock(return_value=0.0)
        bot = FakeBot(paper_mode=False, gmgn_cli=gmgn, balance=0.0)

        async def go():
            return await bot.run(make_decision())

        asyncio.run(go())
        bot.executor.execute_buy.assert_not_called()


class TestPositionSizeGate:
    def test_size_below_min_skipped(self):
        gmgn = MagicMock()
        gmgn.is_ready = MagicMock(return_value=True)
        gmgn.get_sol_balance = AsyncMock(return_value=0.5)
        rm = MagicMock()
        rm.can_trade = MagicMock(return_value=(True, "OK"))
        rm.get_position_size = MagicMock(return_value=0.005)  # below min
        bot = FakeBot(paper_mode=False, gmgn_cli=gmgn, risk_manager=rm, min_size=0.02)

        async def go():
            return await bot.run(make_decision())

        asyncio.run(go())
        bot.executor.execute_buy.assert_not_called()


class TestBuyExecution:
    def test_executor_called_with_size(self):
        gmgn = MagicMock()
        gmgn.is_ready = MagicMock(return_value=True)
        gmgn.get_sol_balance = AsyncMock(return_value=0.5)
        bot = FakeBot(paper_mode=False, gmgn_cli=gmgn, balance=0.5, fixed_size=0.05)

        async def go():
            return await bot.run(make_decision(), address="TokTest111")

        call_args = asyncio.run(go())
        bot.executor.execute_buy.assert_called_once()
        args, kwargs = call_args
        token, size = args[0], args[1]
        assert size == 0.05
        assert token.address == "TokTest111"

    def test_executor_exception_logged_not_raised(self):
        gmgn = MagicMock()
        gmgn.is_ready = MagicMock(return_value=True)
        gmgn.get_sol_balance = AsyncMock(return_value=0.5)
        bot = FakeBot(paper_mode=False, gmgn_cli=gmgn)
        bot.executor.execute_buy = AsyncMock(side_effect=RuntimeError("swap failed"))

        async def go():
            await bot.run(make_decision())

        asyncio.run(go())


class TestLivePausedGate:
    def test_live_paused_blocks_buy(self):
        gmgn = MagicMock()
        gmgn.is_ready = MagicMock(return_value=True)
        gmgn.get_sol_balance = AsyncMock(return_value=0.5)
        bot = FakeBot(paper_mode=False, gmgn_cli=gmgn)
        bot._live_paused = True

        async def go():
            await bot.run(make_decision())

        asyncio.run(go())
        bot.executor.execute_buy.assert_not_called()

    def test_live_resumed_allows_buy(self):
        gmgn = MagicMock()
        gmgn.is_ready = MagicMock(return_value=True)
        gmgn.get_sol_balance = AsyncMock(return_value=0.5)
        bot = FakeBot(paper_mode=False, gmgn_cli=gmgn)
        bot._live_paused = False

        async def go():
            await bot.run(make_decision())

        asyncio.run(go())
        bot.executor.execute_buy.assert_called_once()


# ============ Post-Execution Telegram Alerts ============

def make_position(position_id=42, size=0.1, entry_price=0.00012345,
                 entry_tx="5xYzAbcDeFgHiJkLmNoPqRsTuVwXyZ"):
    """Build a Position-like mock for alert tests."""
    p = MagicMock(spec=Position)
    p.id = position_id
    p.entry_amount_sol = size
    p.entry_price = entry_price
    p.entry_tx_sig = entry_tx
    return p


class TestPostExecutionAlerts:
    @patch("main.dispatcher")
    def test_executed_alert_sent_on_buy_success(self, mock_dispatcher):
        gmgn = MagicMock()
        gmgn.is_ready = MagicMock(return_value=True)
        gmgn.get_sol_balance = AsyncMock(return_value=0.5)
        bot = FakeBot(paper_mode=False, gmgn_cli=gmgn, balance=0.5, fixed_size=0.1)
        bot.executor.execute_buy = AsyncMock(
            return_value=make_position(size=0.1)
        )

        async def go():
            await bot.run(make_decision(), address="TokTest111")

        asyncio.run(go())

        # Buy happened
        bot.executor.execute_buy.assert_called_once()
        # Alert sent with EXECUTED
        mock_dispatcher.send_alert.assert_called_once()
        alert_text = mock_dispatcher.send_alert.call_args[0][0]
        assert "✅ BUY EXECUTED" in alert_text
        assert "TokTest1..." in alert_text  # address[:8] + "..."
        assert "0.1000 SOL" in alert_text
        assert "Position ID: 42" in alert_text
        # Strategy details
        assert "TP1: 1.50x sell 75%" in alert_text
        assert "TP2: 2.00x sell 100% remaining" in alert_text
        assert "Trailing: 1.50x activation, 30% drawdown" in alert_text
        assert "SL: -50% sell 100%" in alert_text
        # Tx hash (truncated)
        assert "5xYzAbcDeFgHi" in alert_text

    @patch("main.dispatcher")
    def test_blocked_alert_sent_on_max_position(self, mock_dispatcher):
        gmgn = MagicMock()
        gmgn.is_ready = MagicMock(return_value=True)
        gmgn.get_sol_balance = AsyncMock(return_value=0.5)
        rm = MagicMock()
        rm.can_trade = MagicMock(return_value=(False, "Max open positions (1/1)"))
        rm.get_position_size = MagicMock(return_value=0.1)
        bot = FakeBot(paper_mode=False, gmgn_cli=gmgn, risk_manager=rm)
        # Active position for context
        bot.position_manager.get_open_positions = AsyncMock(return_value=[
            {"paper": 0, "token_symbol": "WIF", "entry_time": "2026-06-14T14:23:00Z"},
        ])

        async def go():
            await bot.run(make_decision())

        asyncio.run(go())

        bot.executor.execute_buy.assert_not_called()
        mock_dispatcher.send_alert.assert_called_once()
        alert_text = mock_dispatcher.send_alert.call_args[0][0]
        assert "❌ BUY BLOCKED" in alert_text
        assert "Risk gate: Max open positions (1/1)" in alert_text
        assert "Position active: WIF" in alert_text
        assert "Will retry on next APE signal" in alert_text

    @patch("main.dispatcher")
    def test_no_alert_in_paper_mode(self, mock_dispatcher):
        bot = FakeBot(paper_mode=True)

        async def go():
            await bot.run(make_decision())

        asyncio.run(go())

        bot.executor.execute_buy.assert_not_called()
        mock_dispatcher.send_alert.assert_not_called()

    @patch("main.dispatcher")
    def test_no_alert_when_paused(self, mock_dispatcher):
        gmgn = MagicMock()
        gmgn.is_ready = MagicMock(return_value=True)
        gmgn.get_sol_balance = AsyncMock(return_value=0.5)
        bot = FakeBot(paper_mode=False, gmgn_cli=gmgn)
        bot._live_paused = True

        async def go():
            await bot.run(make_decision())

        asyncio.run(go())

        bot.executor.execute_buy.assert_not_called()
        # Paused → no spam alert
        mock_dispatcher.send_alert.assert_not_called()

    @patch("main.dispatcher")
    def test_watch_verdict_skips_post_alert(self, mock_dispatcher):
        gmgn = MagicMock()
        gmgn.is_ready = MagicMock(return_value=True)
        gmgn.get_sol_balance = AsyncMock(return_value=0.5)
        bot = FakeBot(paper_mode=False, gmgn_cli=gmgn)

        async def go():
            await bot.run(make_decision(verdict=Verdict.WATCH))

        asyncio.run(go())

        # WATCH never attempts buy
        bot.executor.execute_buy.assert_not_called()
        # No post-exec alert (WATCH pre-alert is in _process_token, not here)
        mock_dispatcher.send_alert.assert_not_called()

    @patch("main.dispatcher")
    def test_blocked_alert_includes_active_position_context(self, mock_dispatcher):
        """When risk gate fails due to max positions, show WHICH token is blocking."""
        gmgn = MagicMock()
        gmgn.is_ready = MagicMock(return_value=True)
        gmgn.get_sol_balance = AsyncMock(return_value=0.5)
        rm = MagicMock()
        rm.can_trade = MagicMock(return_value=(False, "Max open positions (1/1)"))
        rm.get_position_size = MagicMock(return_value=0.1)
        bot = FakeBot(paper_mode=False, gmgn_cli=gmgn, risk_manager=rm)
        # No active position (edge case: race between position close and check)
        bot.position_manager.get_open_positions = AsyncMock(return_value=[])

        async def go():
            await bot.run(make_decision())

        asyncio.run(go())

        alert_text = mock_dispatcher.send_alert.call_args[0][0]
        assert "❌ BUY BLOCKED" in alert_text
        assert "Risk gate: Max open positions (1/1)" in alert_text
        # No "Position active" line when no active position
        assert "Position active:" not in alert_text
