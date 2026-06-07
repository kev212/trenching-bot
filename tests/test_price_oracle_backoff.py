"""Tests for PriceOracle backoff (_set_jupiter_backoff helper)."""
import asyncio
import sys
import time

sys.path.insert(0, "/Users/khezuma/workspace/trenching")

from core.price_oracle import PriceOracle


def test_jupiter_backoff_429_sets_30s_default():
    """429 with no header → now + 30s."""
    p = PriceOracle(proxy="", timeout=5)
    p._set_jupiter_backoff(429, {})
    assert p._jupiter_backoff_active()
    remaining = p._jupiter_backoff_until - time.time()
    assert 28 < remaining <= 31, f"expected ~30s, got {remaining}"


def test_jupiter_backoff_429_with_delta_seconds_header():
    """429 with x-ratelimit-reset: 60 → now + 60s."""
    p = PriceOracle(proxy="", timeout=5)
    p._set_jupiter_backoff(429, {"x-ratelimit-reset": "60"})
    remaining = p._jupiter_backoff_until - time.time()
    assert 55 < remaining <= 61, f"expected ~60s, got {remaining}"


def test_jupiter_backoff_429_with_unix_seconds_header():
    """429 with x-ratelimit-reset as Unix seconds → use that timestamp."""
    p = PriceOracle(proxy="", timeout=5)
    # 2030-01-01 = 1893456000 unix seconds (far future)
    p._set_jupiter_backoff(429, {"x-ratelimit-reset": "1893456000"})
    remaining = p._jupiter_backoff_until - time.time()
    assert remaining > 100, f"expected far future, got {remaining}"


def test_jupiter_backoff_429_with_milliseconds_header():
    """429 with x-ratelimit-reset as Unix milliseconds."""
    p = PriceOracle(proxy="", timeout=5)
    # 2030-01-01 in ms = 1.893e12
    p._set_jupiter_backoff(429, {"x-ratelimit-reset": "1893456000000"})
    remaining = p._jupiter_backoff_until - time.time()
    assert remaining > 100, f"expected far future, got {remaining}"


def test_jupiter_backoff_403_sets_10min():
    p = PriceOracle(proxy="", timeout=5)
    p._set_jupiter_backoff(403)
    remaining = p._jupiter_backoff_until - time.time()
    assert 595 < remaining <= 601, f"expected ~600s, got {remaining}"
    assert "10 min" in p._jupiter_backoff_reason


def test_jupiter_backoff_503_sets_30min():
    p = PriceOracle(proxy="", timeout=5)
    p._set_jupiter_backoff(503)
    remaining = p._jupiter_backoff_until - time.time()
    assert 1795 < remaining <= 1801, f"expected ~1800s, got {remaining}"
    assert "30 min" in p._jupiter_backoff_reason


def test_jupiter_backoff_invalid_header_falls_back_to_30s():
    """Invalid x-ratelimit-reset → 30s default."""
    p = PriceOracle(proxy="", timeout=5)
    p._set_jupiter_backoff(429, {"x-ratelimit-reset": "not-a-number"})
    remaining = p._jupiter_backoff_until - time.time()
    assert 28 < remaining <= 31, f"expected ~30s fallback, got {remaining}"


def test_jupiter_backoff_expires():
    """After duration passes, backoff no longer active."""
    p = PriceOracle(proxy="", timeout=5)
    p._backoff_until = time.time() - 1  # 1s in the past
    p._backoff_reason = "test"
    assert not p._jupiter_backoff_active()
