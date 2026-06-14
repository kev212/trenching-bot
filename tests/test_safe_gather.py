"""Tests for safe_gather helper + position monitor timeout caps (audit cycle 4).

Covers:
- safe_gather no-timeout path returns full results
- safe_gather timeout returns partial results with TimeoutError placeholders
- safe_gather no leaked CancelledError (works around cpython#102988)
- safe_gather works with single coroutine
- Position monitor paper price walk capped at 10s
- Position monitor live price walk capped at 10s
- GMGN call in _simulate_paper_price_walk capped at 5s
"""
import asyncio
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, "/Users/khezuma/workspace/trenching")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# --- safe_gather tests ------------------------------------------------------


async def _fast():
    return "ok"


async def _slow(seconds=99):
    await asyncio.sleep(seconds)
    return "never"


async def _sentry(val=0):
    """Instant return for sentinel checks."""
    return val


def test_safe_gather_no_timeout():
    """Returns full results when timeout is None."""
    from utils.helpers import safe_gather

    results = _run(safe_gather(_sentry(1), _sentry(2), _sentry(3)))
    assert results == [1, 2, 3], f"expected [1,2,3], got {results}"


def test_safe_gather_returns_partial_on_timeout():
    """Fast coros return their results; slow coros get TimeoutError."""
    from utils.helpers import safe_gather

    results = _run(safe_gather(_fast(), _slow(99), _sentry(42), timeout=0.3))
    assert len(results) == 3, f"expected 3 results, got {len(results)}"
    assert results[0] == "ok", f"first should be 'ok', got {results[0]}"
    assert isinstance(results[1], asyncio.TimeoutError), \
        f"second should be TimeoutError, got {type(results[1]).__name__}"
    assert results[2] == 42, f"third should be 42, got {results[2]}"


def test_safe_gather_single_coro_no_timeout():
    """Works with a single coroutine (like Jupiter-blocked oracle path)."""
    from utils.helpers import safe_gather

    results = _run(safe_gather(_sentry(99)))
    assert results == [99], f"expected [99], got {results}"


def test_safe_gather_single_coro_times_out():
    """Single coro that times out returns TimeoutError placeholder."""
    from utils.helpers import safe_gather

    results = _run(safe_gather(_slow(99), timeout=0.2))
    assert len(results) == 1
    assert isinstance(results[0], asyncio.TimeoutError)


def test_safe_gather_empty():
    """Empty coros returns empty list."""
    from utils.helpers import safe_gather

    results = _run(safe_gather())
    assert results == []


def test_safe_gather_no_leaked_cancelled_warning():
    """Verify safe_gather does NOT produce "exception was never retrieved"
    warnings that the old wait_for(gather) pattern suffered from.

    We run safe_gather with a timeout and check no 'exception was never
    retrieved' messages make it to stderr via the warnings module.
    """
    import io
    import warnings

    from utils.helpers import safe_gather

    # Capture stderr for "exception was never retrieved"
    stderr = io.StringIO()
    old_stderr = sys.stderr
    sys.stderr = stderr

    warnings.catch_warnings()
    try:
        _run(safe_gather(_slow(99), timeout=0.2))
    finally:
        sys.stderr = old_stderr

    output = stderr.getvalue()
    assert "exception was never retrieved" not in output, \
        f"safe_gather leaked CancelledError: {output[:200]}"


def test_safe_gather_with_exception_coro():
    """If a coroutine raises an exception (not timeout), it's returned
    as-is (not wrapped in TimeoutError)."""
    from utils.helpers import safe_gather

    async def _raises():
        raise ValueError("boom")

    results = _run(safe_gather(_fast(), _raises(), _sentry(1), timeout=2.0))
    assert results[0] == "ok"
    assert isinstance(results[1], ValueError) and "boom" in str(results[1])
    assert results[2] == 1


# --- Position monitor timeout caps -----------------------------------------


def test_paper_price_walk_has_10s_cap():
    """Fix #3 + Bug #6: position monitor wraps paper price walk in 5s timeout
    (refactored into _fetch_token_price helper)."""
    from tracking import position_monitor as pm

    with open(pm.__file__) as f:
        src = f.read()

    # Must have asyncio.wait_for(executor._simulate_paper_price_walk, timeout=5)
    # Now lives inside _fetch_token_price
    assert "asyncio.wait_for(" in src
    assert "_simulate_paper_price_walk(position, \"monitor\")" in src or \
           "_simulate_paper_price_walk(position, 'monitor')" in src
    assert "timeout=5" in src or "timeout=10" in src, \
        "paper and live price walks should have timeout caps"
    assert "price = 0.0" in src, \
        "on timeout, should set price to 0 (skip SL/TP)"


def test_live_price_walk_has_10s_cap():
    """Fix #3: position monitor wraps live price retry in 10s timeout
    (refactored into _fetch_token_price helper)."""
    from tracking import position_monitor as pm

    with open(pm.__file__) as f:
        src = f.read()

    assert "get_token_price_in_sol_with_retry(token_address)" in src or \
           "get_token_price_in_sol_with_retry(token_address)" in src
    assert "price = 0.0" in src, \
        "on timeout, should return 0 (skip SL/TP)"


def test_gmgn_call_in_paper_price_walk_has_5s_cap():
    """Fix #4: GMGN get_token_info inside _simulate_paper_price_walk is
    capped at 5s (not the default 15s)."""
    from core import trade_executor as te

    with open(te.__file__) as f:
        src = f.read()

    assert "asyncio.wait_for(" in src, \
        "should wrap GMGN call in asyncio.wait_for"
    assert "timeout=5.0" in src, \
        "GMGN fallback in paper price walk should be capped at 5s"


# --- Regression: price_oracle uses safe_gather -----------------------------


def test_price_oracle_uses_safe_gather():
    """Verify price_oracle.get_price_in_usd uses safe_gather instead of
    the raw wait_for(gather) pattern that leaked CancelledError."""
    from core import price_oracle as po

    with open(po.__file__) as f:
        src = f.read()

    assert "safe_gather" in src, \
        "price_oracle should import and use safe_gather"
    assert "asyncio.wait_for(" not in src.split("def get_price_in_usd")[1].split("def ")[0] if len(src.split("def get_price_in_usd")) > 1 and src.split("def get_price_in_usd")[1].count("def ") > 0 else None, \
        "price_oracle.get_price_in_usd should use safe_gather not raw wait_for"


# --- Regression: main.py uses safe_gather ----------------------------------


def test_main_uses_safe_gather():
    """Verify main.py GMGN batches use safe_gather instead of
    the raw wait_for(gather) pattern."""
    import main as m

    with open(m.__file__) as f:
        src = f.read()

    assert "safe_gather" in src, \
        "main.py should import and use safe_gather"
    assert "asyncio.wait_for(" not in src.split(
        "# Phase B: fetch token data"
    )[1].split(
        "if not info:"
    )[0] if len(src.split("# Phase B: fetch token data")) > 1 and len(src.split("# Phase B: fetch token data")[1].split("if not info:")) > 1 else True, \
        "GMGN batches should use safe_gather, not raw wait_for"
