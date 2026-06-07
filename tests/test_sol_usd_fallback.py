"""Tests for SOL/USD 2-tier fallback (Jupiter v3 → Jupiter v6)."""
import asyncio
import sys
import time
from unittest.mock import MagicMock

sys.path.insert(0, "/Users/khezuma/workspace/trenching")

from core.price_oracle import PriceOracle, SOL_PRICE_CACHE_TTL


class FakeRespCtx:
    def __init__(self, status, payload=None, headers=None):
        self.status = status
        self.payload = payload or {}
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self.payload

    async def text(self):
        return str(self.payload)


class FakeSessionV3:
    """Mock session: v3 returns $150, v6 returns different."""
    call_count = {"v3": 0, "v6": 0}

    def get(self, url, **kwargs):
        if "lite-api.jup.ag" in url:
            FakeSessionV3.call_count["v3"] += 1
            return FakeRespCtx(200, {f"So11111111111111111111111111111111111111112": {"usdPrice": 150.0}})
        elif "price.jup.ag" in url:
            FakeSessionV3.call_count["v6"] += 1
            return FakeRespCtx(200, {"data": {"So11111111111111111111111111111111111111112": {"price": 999.0}}})
        return FakeRespCtx(404)


class FakeSessionV3Fail:
    """Mock session: v3 fails, v6 succeeds."""
    call_count = {"v3": 0, "v6": 0}

    def get(self, url, **kwargs):
        if "lite-api.jup.ag" in url:
            FakeSessionV3Fail.call_count["v3"] += 1
            return FakeRespCtx(429, {}, {"x-ratelimit-reset": "30"})
        elif "price.jup.ag" in url:
            FakeSessionV3Fail.call_count["v6"] += 1
            return FakeRespCtx(200, {"data": {"So11111111111111111111111111111111111111112": {"price": 150.0}}})
        return FakeRespCtx(404)


class FakeSessionAllFail:
    call_count = {"v3": 0, "v6": 0}
    closed = False

    def get(self, url, **kwargs):
        if "lite-api.jup.ag" in url:
            FakeSessionAllFail.call_count["v3"] += 1
            return FakeRespCtx(500)
        elif "price.jup.ag" in url:
            FakeSessionAllFail.call_count["v6"] += 1
            return FakeRespCtx(500)
        return FakeRespCtx(404)


def test_sol_usd_v3_returns_price():
    """v3 is primary; v6 should NOT be called when v3 succeeds."""
    async def go():
        FakeSessionV3.call_count = {"v3": 0, "v6": 0}
        oracle = PriceOracle(proxy="", timeout=5)
        oracle._session = FakeSessionV3()
        price = await oracle.get_sol_price_usd()
        assert price == 150.0, f"expected 150.0, got {price}"
        assert FakeSessionV3.call_count["v3"] == 1, f"v3 should be called once"
        assert FakeSessionV3.call_count["v6"] == 0, f"v6 should NOT be called when v3 succeeds"
    asyncio.run(go())


def test_sol_usd_falls_back_to_v6_when_v3_fails():
    """When v3 returns 429, fallback to v6."""
    async def go():
        FakeSessionV3Fail.call_count = {"v3": 0, "v6": 0}
        oracle = PriceOracle(proxy="", timeout=5)
        oracle._session = FakeSessionV3Fail()
        price = await oracle.get_sol_price_usd()
        assert price == 150.0, f"expected 150.0 from v6 fallback, got {price}"
        assert FakeSessionV3Fail.call_count["v3"] == 1
        assert FakeSessionV3Fail.call_count["v6"] == 1
    asyncio.run(go())


def test_sol_usd_returns_cached_when_all_fail():
    """If all sources fail AND cache exists, return stale cache."""
    async def go():
        oracle = PriceOracle(proxy="", timeout=5)
        oracle._session = FakeSessionAllFail()
        # Pre-warm cache
        oracle._sol_price_cache["SOL"] = {"ts": time.time() - 100, "price": 145.0}
        price = await oracle.get_sol_price_usd()
        assert price == 145.0, f"expected stale cache 145.0, got {price}"
    asyncio.run(go())


def test_sol_usd_returns_zero_when_no_cache_and_all_fail():
    async def go():
        FakeSessionAllFail.call_count = {"v3": 0, "v6": 0}
        oracle = PriceOracle(proxy="", timeout=5)
        oracle._session = FakeSessionAllFail()
        price = await oracle.get_sol_price_usd()
        assert price == 0.0, f"expected 0.0, got {price}"
    asyncio.run(go())


def test_sol_usd_cache_expires_after_10s():
    """Cache TTL is 10s, not 30s (per Charon-style faster recovery)."""
    async def go():
        FakeSessionV3.call_count = {"v3": 0, "v6": 0}
        oracle = PriceOracle(proxy="", timeout=5)
        oracle._session = FakeSessionV3()
        # First call → cache
        p1 = await oracle.get_sol_price_usd()
        assert p1 == 150.0
        # Simulate cache being 11s old (past TTL)
        oracle._sol_price_cache["SOL"] = {"ts": time.time() - 11, "price": 100.0}
        p2 = await oracle.get_sol_price_usd()
        # Should re-fetch since cache expired
        assert p2 == 150.0
        assert FakeSessionV3.call_count["v3"] == 2, "should re-fetch after 10s"
    asyncio.run(go())


def test_sol_usd_prewarm_retries_3x():
    """start() should retry pre-warm 3x with exponential backoff."""
    async def go():
        FakeSessionAllFail.call_count = {"v3": 0, "v6": 0}
        oracle = PriceOracle(proxy="", timeout=5)
        oracle._session = FakeSessionAllFail()
        # Track elapsed time
        start = time.time()
        await oracle.start()
        elapsed = time.time() - start
        # Should wait 1s + 2s = 3s between 3 attempts minimum
        assert elapsed >= 2.5, f"expected >=2.5s total (1+2 backoff), got {elapsed:.2f}s"
        assert FakeSessionAllFail.call_count["v3"] == 3, f"should attempt v3 3 times, got {FakeSessionAllFail.call_count['v3']}"
    asyncio.run(go())


def test_sol_usd_backoff_blocks_subsequent_calls():
    """When backoff is active, get_sol_price_usd returns cache (no HTTP)."""
    async def go():
        FakeSessionV3Fail.call_count = {"v3": 0, "v6": 0}
        oracle = PriceOracle(proxy="", timeout=5)
        oracle._session = FakeSessionV3Fail()
        # First call: v3 fails with 429 → sets backoff, v6 succeeds
        p1 = await oracle.get_sol_price_usd()
        assert p1 == 150.0
        assert FakeSessionV3Fail.call_count["v3"] == 1
        # Second call within backoff: should return cached, no HTTP
        p2 = await oracle.get_sol_price_usd()
        assert p2 == 150.0
        # v3 should still only have been called once (backoff blocks)
        assert FakeSessionV3Fail.call_count["v3"] == 1, f"v3 should be blocked by backoff"
    asyncio.run(go())


def test_sol_price_cache_ttl_constant_is_10s():
    """Verify the constant is 10s (was 30s)."""
    assert SOL_PRICE_CACHE_TTL == 10.0, f"expected 10s, got {SOL_PRICE_CACHE_TTL}"
