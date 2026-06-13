"""Test live-buy wiring in TrenchingBot._maybe_execute_live_buy.

Verifies the full guard chain (paper_mode, verdict, confidence, gmgn_cli,
risk_manager, balance, size) and executor.execute_buy() invocation.
"""
import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, "/Users/khezuma/workspace/trenching")

import pytest

from analysis.models import TokenData, Verdict


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

    # Bind the real method
    from main import TrenchingBot
    _maybe_execute_live_buy = TrenchingBot._maybe_execute_live_buy

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
    def test_low_confidence_skipped(self):
        bot = FakeBot(paper_mode=False, confidence_threshold=0.60)

        async def go():
            return await bot.run(make_decision(conf=0.50))

        asyncio.run(go())
        bot.executor.execute_buy.assert_not_called()

    def test_high_confidence_proceeds(self):
        gmgn = MagicMock()
        gmgn.is_ready = MagicMock(return_value=True)
        gmgn.get_sol_balance = AsyncMock(return_value=0.5)
        bot = FakeBot(paper_mode=False, gmgn_cli=gmgn)

        async def go():
            return await bot.run(make_decision(conf=0.80))

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
