"""Tests for USD-canonical price oracle.

These tests verify:
1. Oracle returns USD, not SOL
2. All 3 sources return USD directly
3. sol_usd is pre-warmed at start()
4. No more USD-as-SOL silent fallbacks
"""
import asyncio
import sys
from unittest.mock import MagicMock

sys.path.insert(0, "/Users/khezuma/workspace/trenching")

from core.price_oracle import PriceOracle


def test_oracle_dexscreener_returns_usd_not_sol():
    """DexScreener path: should return priceUsd (USD), not priceNative (SOL)."""
    from core.price_oracle import PriceOracle

    class FakeRespCtx:
        def __init__(self, status, payload):
            self.status = status
            self.payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self):
            return self.payload

        async def text(self):
            return str(self.payload)

    class FakeSession:
        def get(self, url, **kwargs):
            return FakeRespCtx(200, {
                "pairs": [
                    {
                        "priceUsd": "0.00012345",
                        "priceNative": "0.0000001",  # would be SOL
                        "liquidity": {"usd": 10000},
                    }
                ]
            })

    async def go():
        oracle = PriceOracle(proxy="", timeout=5)
        oracle._session = FakeSession()
        price = await oracle._from_dexscreener_usd("ADDR")
        assert price == 0.00012345, f"expected USD price 0.00012345, got {price}"
    asyncio.run(go())


def test_oracle_jupiter_returns_usd():
    """Jupiter path: returns USD directly, no conversion needed."""
    from core.price_oracle import PriceOracle

    class FakeRespCtx:
        def __init__(self, status, payload):
            self.status = status
            self.payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self):
            return self.payload

    class FakeSession:
        def get(self, url, **kwargs):
            return FakeRespCtx(200, {"data": {"TOKADDR": {"price": 0.0005}}})

    async def go():
        oracle = PriceOracle(proxy="", timeout=5)
        oracle._session = FakeSession()
        price = await oracle._from_jupiter_usd("TOKADDR")
        assert price == 0.0005, f"expected USD 0.0005, got {price}"
    asyncio.run(go())


def test_oracle_gmgn_returns_usd_not_sol():
    """GMGN path: should use price.price (USD), NOT price.native_token.price (SOL)."""
    from core.price_oracle import PriceOracle

    class FakeGmgn:
        async def get_token_info(self, addr):
            return {
                "price": {
                    "price": 0.00001,  # USD
                    "native_token": {"price": 0.00000007},  # SOL — IGNORE
                }
            }

    async def go():
        oracle = PriceOracle(gmgn=FakeGmgn(), proxy="", timeout=5)
        oracle._session = None
        price = await oracle._from_gmgn_usd("ADDR")
        assert price == 0.00001, f"expected USD 0.00001, got {price}"
        assert price > 0.000001, f"got SOL value, expected USD"
    asyncio.run(go())


def test_oracle_start_prewarms_sol_usd():
    """PriceOracle.start() should pre-warm SOL/USD cache to avoid startup race."""
    from core.price_oracle import PriceOracle
    import time

    class FakeRespCtx:
        def __init__(self, status, payload):
            self.status = status
            self.payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self):
            return self.payload

    class FakeSession:
        closed = False
        def get(self, url, **kwargs):
            return FakeRespCtx(200, {
                "data": {"So11111111111111111111111111111111111111112": {"price": 150.0}}
            })

    async def go():
        oracle = PriceOracle(proxy="", timeout=5)
        oracle._session = FakeSession()
        await oracle.start()
        cached = oracle._sol_price_cache.get("SOL")
        assert cached is not None, "sol_usd not pre-warmed"
        assert cached["price"] == 150.0, f"unexpected sol_usd: {cached['price']}"
    asyncio.run(go())


def test_buy_uses_usd_math():
    """execute_buy computes amount_token via USD math: (size_sol × sol_usd) / entry_usd."""
    from core.trade_executor import TradeExecutor, MIN_VALID_USD_PRICE, MAX_VALID_USD_PRICE
    from core.wallet import Wallet
    from core.risk_manager import RiskManager
    from analysis.models import TokenData

    class FakeJupiter:
        async def get_quote(self, *a, **k): return {}
        async def get_token_price_in_sol_with_retry(self, *a, **k): return 0.0

    class FakeOracle:
        async def get_price_in_usd(self, addr): return 0.0001  # USD
        async def get_sol_price_usd(self): return 150.0

    captured = {}

    class FakePM:
        db = None
        async def open_position(self, p):
            captured["position"] = p
            return 1
        async def record_trade(self, t): return 1

    async def go():
        executor = TradeExecutor(
            paper=True,
            wallet=Wallet(paper=True, starting_balance_sol=10.0),
            jupiter=FakeJupiter(),
            positions=FakePM(),
            risk=RiskManager({}),
            config={},
            price_oracle=FakeOracle(),
        )
        token = TokenData(address="X", symbol="T", name="n")
        token.raw_gmgn = {}  # not used; oracle returns USD
        position = await executor.execute_buy(token, 0.05)
        assert position is not None
        # amount_token = (0.05 × 150) / (0.0001 × 1.01) = 7.5 / 0.000101 ≈ 74257.43
        expected_tokens = (0.05 * 150.0) / (0.0001 * 1.01)
        assert abs(position.entry_amount_token - expected_tokens) < 1.0
        # entry_price stored in USD
        assert position.entry_price == 0.0001
        # sol_usd_at_entry stored
        assert position.sol_usd_at_entry == 150.0
    asyncio.run(go())


def test_sell_pnl_in_both_units():
    """execute_sell computes pnl_usd (canonical) and pnl_sol (display) both."""
    from core.trade_executor import TradeExecutor
    from core.wallet import Wallet
    from core.risk_manager import RiskManager
    from analysis.models import TokenData
    from datetime import datetime, timezone

    class FakeJupiter:
        async def get_quote(self, *a, **k): return {}
        async def get_token_price_in_sol_with_retry(self, *a, **k): return 0.0

    class FakeOracle:
        async def get_price_in_usd(self, addr):
            # First call: BUY price. Second call: SELL price.
            if not hasattr(FakeOracle, "calls"):
                FakeOracle.calls = 0
            FakeOracle.calls += 1
            if FakeOracle.calls == 1:
                return 0.0001  # entry
            return 0.00015  # exit (+50% gain)
        async def get_sol_price_usd(self): return 150.0

    captured = {}

    class FakePM:
        db = None
        async def open_position(self, p):
            captured["position"] = p
            return 1
        async def record_trade(self, t): return 1
        async def close_position(self, pos, reason, exit_price, **kwargs):
            captured["close"] = kwargs
            captured["close"]["exit_price"] = exit_price
            return None
        async def record_partial_sell(self, pos, sold, remaining): return None
        async def update_position(self, pos): return None

    async def go():
        executor = TradeExecutor(
            paper=True,
            wallet=Wallet(paper=True, starting_balance_sol=10.0),
            jupiter=FakeJupiter(),
            positions=FakePM(),
            risk=RiskManager({}),
            config={},
            price_oracle=FakeOracle(),
        )
        token = TokenData(address="X", symbol="T", name="n")
        token.raw_gmgn = {}
        # BUY
        position = await executor.execute_buy(token, 0.05)
        # Set up position dict for SELL
        pos_dict = {
            "id": position.id,
            "token_address": "X",
            "token_symbol": "T",
            "entry_price": position.entry_price,
            "entry_amount_sol": 0.05,
            "entry_amount_token": position.entry_amount_token,
            "current_amount_token": position.entry_amount_token,
            "total_sold_sol": 0.0,
            "total_sold_usd": 0.0,
            "status": "OPEN",
            "sol_usd_at_entry": 150.0,
            "raw_gmgn_json": "",
            "peak_price": position.entry_price,
        }
        # Warm paper cache so SELL doesn't refetch and get a different value
        import time
        executor._paper_price_cache["X"] = {"ts": time.time(), "price": 0.00015}
        # SELL 100% at exit
        await executor.execute_sell(pos_dict, 100, "TP1")
        # Verify pnl_usd and pnl_sol both computed
        assert "close" in captured
        assert "pnl_usd" in captured["close"]
        assert "pnl_sol" in captured["close"]
    asyncio.run(go())


def test_sanity_range_rejects_too_high_usd():
    """execute_buy rejects entry_price > MAX_VALID_USD_PRICE (likely unit mismatch)."""
    from core.trade_executor import TradeExecutor, MAX_VALID_USD_PRICE
    from core.wallet import Wallet
    from core.risk_manager import RiskManager
    from analysis.models import TokenData

    class FakeJupiter:
        async def get_quote(self, *a, **k): return {}
        async def get_token_price_in_sol_with_retry(self, *a, **k): return 0.0

    class FakeOracle:
        async def get_price_in_usd(self, addr): return 200.0  # above MAX 100
        async def get_sol_price_usd(self): return 150.0

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
            price_oracle=FakeOracle(),
        )
        token = TokenData(address="X", symbol="T", name="n")
        token.raw_gmgn = {}
        result = await executor.execute_buy(token, 0.05)
        assert result is None, f"expected None (sanity reject), got {result}"
    asyncio.run(go())


def test_sanity_range_rejects_too_low_usd():
    """execute_buy rejects entry_price < MIN_VALID_USD_PRICE (likely junk)."""
    from core.trade_executor import TradeExecutor, MIN_VALID_USD_PRICE
    from core.wallet import Wallet
    from core.risk_manager import RiskManager
    from analysis.models import TokenData

    class FakeJupiter:
        async def get_quote(self, *a, **k): return {}
        async def get_token_price_in_sol_with_retry(self, *a, **k): return 0.0

    class FakeOracle:
        async def get_price_in_usd(self, addr): return 0.0000000001  # below MIN 1e-8
        async def get_sol_price_usd(self): return 150.0

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
            price_oracle=FakeOracle(),
        )
        token = TokenData(address="X", symbol="T", name="n")
        token.raw_gmgn = {}
        result = await executor.execute_buy(token, 0.05)
        assert result is None, f"expected None (sanity reject), got {result}"
    asyncio.run(go())
