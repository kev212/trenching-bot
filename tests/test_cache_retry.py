"""Unit tests for storage/cache.py retry backoff + dead-letter logic.

Run: .venv/bin/python -m pytest tests/test_cache_retry.py -v
"""
import asyncio
import sys
import time as time_module
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from storage.cache import (
    RETRY_BACKOFF,
    MAX_RETRIES,
    get_retry_delay,
    SharedState,
)

# ── get_retry_delay ──────────────────────────────────────────────────────────


def test_retry_0_returns_60():
    assert get_retry_delay(0) == 60


def test_retry_1_returns_180():
    assert get_retry_delay(1) == 180


def test_retry_2_returns_300():
    assert get_retry_delay(2) == 300


def test_retry_3_fallback_300():
    assert get_retry_delay(3) == 300


def test_retry_unknown_fallback_300():
    assert get_retry_delay(99) == 300


def test_backoff_dict_keys():
    assert set(RETRY_BACKOFF.keys()) == {0, 1, 2}


def test_max_retries_is_3():
    assert MAX_RETRIES == 3


# ── SharedState requires event loop for asyncio.Lock in __init__ ──────────────


async def _make_state(real_metrics: bool = False):
    state = SharedState()
    if not real_metrics:
        state.metrics = MagicMock()
    return state


# ── PERMANENT_FILTERS ─────────────────────────────────────────────────────────


def test_permanent_filters_only_min_total_fee():
    from analysis.filters import PERMANENT_FILTERS
    assert PERMANENT_FILTERS == frozenset({"min_total_fee"})


def test_permanent_filters_excludes_others():
    from analysis.filters import PERMANENT_FILTERS
    assert "min_market_cap" not in PERMANENT_FILTERS
    assert "max_market_cap" not in PERMANENT_FILTERS
    assert "min_holders" not in PERMANENT_FILTERS
    assert "token_age" not in PERMANENT_FILTERS
    assert "fee_tier" not in PERMANENT_FILTERS
    assert "holder_distribution" not in PERMANENT_FILTERS
    assert "rug_probability" not in PERMANENT_FILTERS
    assert "ath_drawdown" not in PERMANENT_FILTERS


def test_is_permanent_failure_true():
    from analysis.filters import is_permanent_failure
    assert is_permanent_failure(["min_total_fee"]) is True
    assert is_permanent_failure(["min_total_fee", "token_age"]) is True


def test_is_permanent_failure_false():
    from analysis.filters import is_permanent_failure
    assert is_permanent_failure([]) is False
    assert is_permanent_failure(["token_age"]) is False
    assert is_permanent_failure(["min_market_cap"]) is False
    assert is_permanent_failure(["max_market_cap"]) is False
    assert is_permanent_failure(["min_holders"]) is False
    assert is_permanent_failure(["fee_tier", "holder_distribution"]) is False


# ── COMPOUND RULE ──────────────────────────────────────────────────


def test_compound_skip_for_old_low_fee():
    from analysis.filters import is_compound_permanent_failure, TokenData

    token = TokenData(
        address="cmp1",
        symbol="CMP",
        name="Compound",
        market_cap=10000,
        fee_collected=0.5,  # < 1.0 SOL
        creation_timestamp=1000,
        open_timestamp=0,
        migrated_timestamp=0,  # pre-migrate
    )
    assert is_compound_permanent_failure(token) is True


def test_compound_skip_old_post_migrate_low_fee():
    from analysis.filters import is_compound_permanent_failure, TokenData

    token = TokenData(
        address="cmp2",
        symbol="CMP2",
        name="Cmp2",
        market_cap=80000,
        fee_collected=0.5,  # < 1.0 SOL
        creation_timestamp=1000,
        open_timestamp=0,
        migrated_timestamp=2000,  # post-migrate
    )
    assert is_compound_permanent_failure(token) is True


def test_compound_no_skip_when_fee_above_1():
    from analysis.filters import is_compound_permanent_failure, TokenData

    token = TokenData(
        address="cmp3",
        symbol="CMP3",
        name="Compound3",
        market_cap=80000,
        fee_collected=1.5,  # >= 1.0 SOL
        creation_timestamp=1000,
        open_timestamp=0,
        migrated_timestamp=0,
    )
    assert is_compound_permanent_failure(token) is False


def test_compound_no_skip_when_young():
    from analysis.filters import is_compound_permanent_failure, TokenData

    token = TokenData(
        address="cmp4",
        symbol="CMP4",
        name="Compound4",
        market_cap=10000,
        fee_collected=0.5,
        creation_timestamp=time_module.time() - 60,  # 1 min old
        open_timestamp=0,
        migrated_timestamp=0,
    )
    assert is_compound_permanent_failure(token) is False


def test_compound_skip_independent_of_failures():
    from analysis.filters import is_compound_permanent_failure, TokenData

    token = TokenData(
        address="cmp5",
        symbol="CMP5",
        name="Compound5",
        market_cap=10000,
        fee_collected=0.5,
        creation_timestamp=1000,
        open_timestamp=0,
        migrated_timestamp=0,
    )
    # compound rule triggers regardless of which filters failed
    assert is_compound_permanent_failure(token) is True


# ── Metrics ────────────────────────────────────────────────────────────────────


def test_metrics_skip_permanent_increments():
    async def go():
        state = await _make_state(real_metrics=True)
        assert state.metrics.calls_skip_permanent == 0
        state.metrics.record_call("SKIP_PERMANENT")
        assert state.metrics.calls_skip_permanent == 1
        assert state.metrics.calls_total == 1
    asyncio.run(go())


def test_metrics_retry_passes():
    async def go():
        state = await _make_state(real_metrics=True)
        state.metrics.record_retry(passed=True)
        assert state.metrics.retry_attempts == 1
        assert state.metrics.retry_passes == 1
        assert state.metrics.retry_fails == 0
    asyncio.run(go())


def test_metrics_retry_fails():
    async def go():
        state = await _make_state(real_metrics=True)
        state.metrics.record_retry(passed=False)
        assert state.metrics.retry_attempts == 1
        assert state.metrics.retry_passes == 0
        assert state.metrics.retry_fails == 1
    asyncio.run(go())


def test_metrics_retry_success_rate():
    async def go():
        state = await _make_state(real_metrics=True)
        assert state.metrics.retry_success_rate == 0.0
        state.metrics.record_retry(passed=True)
        state.metrics.record_retry(passed=False)
        state.metrics.record_retry(passed=False)
        assert abs(state.metrics.retry_success_rate - 33.3333) < 0.01
    asyncio.run(go())


def test_retry_to_dict_includes_new_fields():
    async def go():
        state = await _make_state(real_metrics=True)
        d = state.metrics.to_dict()
        assert "calls_skip_permanent" in d
        assert "retry_attempts" in d
        assert "retry_passes" in d
        assert "retry_fails" in d
        assert "retry_success_rate" in d
    asyncio.run(go())


# ── add_retry ────────────────────────────────────────────────────────────────


def test_add_retry_creates_entry():
    async def go():
        state = await _make_state()
        await state.add_retry("abc", symbol="TEST", name="Test Token")
        info = state.retry_queue.get("abc")
        assert info is not None
        assert info["symbol"] == "TEST"
        assert info["name"] == "Test Token"
        assert info["retries"] == 0
        assert info.get("failed_filters") == []
        assert "timestamp" in info
    asyncio.run(go())


def test_add_retry_with_failed_filters():
    async def go():
        state = await _make_state()
        await state.add_retry("abc", symbol="T", name="T", failed_filters=["min_total_fee", "token_age"])
        info = state.retry_queue["abc"]
        assert info["failed_filters"] == ["min_total_fee", "token_age"]
    asyncio.run(go())


def test_add_retry_with_failed_filters_increment():
    async def go():
        state = await _make_state()
        await state.add_retry("abc", symbol="T", name="T", failed_filters=["min_total_fee"])
        await state.add_retry("abc", symbol="T", name="T", failed_filters=["min_total_fee"])
        # Second call increments retries but keeps first failed_filters
        assert state.retry_queue["abc"]["retries"] == 1
        assert "failed_filters" in state.retry_queue["abc"]
    asyncio.run(go())


def test_add_retry_increments():
    async def go():
        state = await _make_state()
        await state.add_retry("abc", symbol="A")
        await state.add_retry("abc", symbol="A")
        assert state.retry_queue["abc"]["retries"] == 1
    asyncio.run(go())


def test_add_retry_updates_timestamp():
    async def go():
        state = await _make_state()
        await state.add_retry("abc", symbol="A")
        old_ts = state.retry_queue["abc"]["timestamp"]
        await asyncio.sleep(0.01)
        await state.add_retry("abc", symbol="A")
        new_ts = state.retry_queue["abc"]["timestamp"]
        assert new_ts > old_ts
    asyncio.run(go())


def test_add_retry_preserves_symbol():
    async def go():
        state = await _make_state()
        await state.add_retry("abc", symbol="TEST")
        info = state.retry_queue["abc"]
        assert info["symbol"] == "TEST"
        await state.add_retry("abc", symbol="TEST")
        assert state.retry_queue["abc"]["symbol"] == "TEST"
    asyncio.run(go())


def test_add_retry_defaults():
    async def go():
        state = await _make_state()
        await state.add_retry("abc")
        info = state.retry_queue["abc"]
        assert info["symbol"] == "?"
        assert info["name"] == "?"
    asyncio.run(go())


# ── should_retry ─────────────────────────────────────────────────────────────


def test_should_retry_not_in_queue():
    async def go():
        state = await _make_state()
        assert await state.should_retry("nonexistent") is False
    asyncio.run(go())


def test_should_retry_exhausted():
    async def go():
        state = await _make_state()
        state.retry_queue["abc"] = {"timestamp": 0, "retries": 3, "symbol": "T"}
        assert await state.should_retry("abc") is False
    asyncio.run(go())


def test_should_retry_too_soon():
    async def go():
        state = await _make_state()
        state.retry_queue["abc"] = {"timestamp": time_module.time(), "retries": 0, "symbol": "T"}
        assert await state.should_retry("abc") is False
    asyncio.run(go())


def test_should_retry_ready():
    async def go():
        state = await _make_state()
        state.retry_queue["abc"] = {"timestamp": time_module.time() - 120, "retries": 0, "symbol": "T"}
        assert await state.should_retry("abc") is True
    asyncio.run(go())


def test_should_retry_retry1_needs_180():
    async def go():
        state = await _make_state()
        state.retry_queue["abc"] = {"timestamp": time_module.time() - 120, "retries": 1, "symbol": "T"}
        assert await state.should_retry("abc") is False
    asyncio.run(go())


def test_should_retry_retry2_needs_300():
    async def go():
        state = await _make_state()
        state.retry_queue["abc"] = {"timestamp": time_module.time() - 400, "retries": 2, "symbol": "T"}
        assert await state.should_retry("abc") is True
    asyncio.run(go())


def test_should_retry_respects_retry1_after_180():
    async def go():
        state = await _make_state()
        state.retry_queue["abc"] = {"timestamp": time_module.time() - 200, "retries": 1, "symbol": "T"}
        assert await state.should_retry("abc") is True
    asyncio.run(go())


# ── cleanup_retry_queue ──────────────────────────────────────────────────────


def test_cleanup_removes_exhausted():
    async def go():
        state = await _make_state()
        now = time_module.time()
        state.retry_queue["abc"] = {"timestamp": now, "retries": 3, "symbol": "T"}
        state.retry_queue["def"] = {"timestamp": now, "retries": 2, "symbol": "T2"}
        await state.cleanup_retry_queue()
        assert "abc" not in state.retry_queue
        assert "def" in state.retry_queue
    asyncio.run(go())


def test_cleanup_removes_stale():
    async def go():
        state = await _make_state()
        very_old = time_module.time() - 1200  # > max_stale (600)
        state.retry_queue["abc"] = {"timestamp": very_old, "retries": 0, "symbol": "T"}
        await state.cleanup_retry_queue()
        assert "abc" not in state.retry_queue
    asyncio.run(go())


def test_cleanup_removes_permanent_filter():
    async def go():
        state = await _make_state()
        now = time_module.time()
        state.retry_queue["perm"] = {"timestamp": now, "retries": 0, "symbol": "P",
                                      "failed_filters": ["min_total_fee"]}
        state.retry_queue["dyn"] = {"timestamp": now, "retries": 0, "symbol": "D",
                                     "failed_filters": ["token_age"]}
        await state.cleanup_retry_queue()
        assert "perm" not in state.retry_queue
        assert "dyn" in state.retry_queue
    asyncio.run(go())


# ── get_retry_info ───────────────────────────────────────────────────────────


def test_get_retry_info_new():
    async def go():
        state = await _make_state()
        assert await state.get_retry_info("nonexistent") == {}
    asyncio.run(go())


def test_get_retry_info_full():
    async def go():
        state = await _make_state()
        state.retry_queue["abc"] = {"timestamp": 100, "retries": 1, "symbol": "T", "name": "Test"}
        info = await state.get_retry_info("abc")
        assert info["retries"] == 1
        assert info["symbol"] == "T"
        assert info["name"] == "Test"
    asyncio.run(go())


# ── remove_retry ─────────────────────────────────────────────────────────────


def test_remove_retry():
    async def go():
        state = await _make_state()
        state.retry_queue["abc"] = {"timestamp": 0, "retries": 0}
        await state.remove_retry("abc")
        assert "abc" not in state.retry_queue
    asyncio.run(go())


def test_remove_retry_missing():
    async def go():
        state = await _make_state()
        await state.remove_retry("nonexistent")
    asyncio.run(go())
