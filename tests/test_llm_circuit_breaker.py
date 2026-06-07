"""Tests for PioneerLLMClient circuit breaker (June 2026 audit fix #4).

Covers:
- Breaker opens after N consecutive failures
- Calls return immediately when open
- Breaker stays open during cooldown
- Breaker closes on first success after cooldown
- Reset on success within threshold
- analyze_token integrates the breaker (returns None when open)
- analyze_batch with open breaker (all None in parallel)
"""
import asyncio
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, "/Users/khezuma/workspace/trenching")

from llm.pioneer_client import CircuitBreaker, PioneerLLMClient, CB_THRESHOLD, CB_COOLDOWN_S


def _run(coro):
    """Helper: run coroutine in a fresh event loop (test isolation)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# --- 1. Breaker opens after threshold failures -----------------------------

def test_breaker_opens_after_threshold_failures():
    """After CB_THRESHOLD consecutive failures, breaker should be open."""
    cb = CircuitBreaker(threshold=3, cooldown=60.0)

    async def _run_test():
        # 2 failures — should still be closed
        await cb.record_failure()
        await cb.record_failure()
        assert not await cb.is_open()
        # 3rd failure — should open
        await cb.record_failure()
        assert await cb.is_open()

    _run(_run_test())


# --- 2. Calls return immediately when breaker is open ----------------------

def test_breaker_returns_immediately_when_open():
    """When breaker is open, is_open() returns True and stays open
    during cooldown."""
    cb = CircuitBreaker(threshold=2, cooldown=60.0)

    async def _run_test():
        # Force open
        await cb.record_failure()
        await cb.record_failure()
        assert await cb.is_open()
        # Check again — still open
        assert await cb.is_open()

    _run(_run_test())


# --- 3. Breaker closes on success after cooldown ---------------------------

def test_breaker_closes_on_success_after_cooldown():
    """When open_until has elapsed, the next is_open() call should
    auto-close the breaker and return False.
    """
    cb = CircuitBreaker(threshold=2, cooldown=0.1)  # very short cooldown

    async def _run_test():
        await cb.record_failure()
        await cb.record_failure()
        assert await cb.is_open()
        # Wait for cooldown
        await asyncio.sleep(0.15)
        # Should auto-close
        assert not await cb.is_open()
        # And reset failure count
        assert cb._failures == 0

    _run(_run_test())


# --- 4. Breaker stays open during cooldown ---------------------------------

def test_breaker_keeps_open_during_cooldown():
    """During the cooldown window, is_open() should keep returning True."""
    cb = CircuitBreaker(threshold=2, cooldown=10.0)  # long cooldown

    async def _run_test():
        await cb.record_failure()
        await cb.record_failure()
        # First check — open
        assert await cb.is_open()
        # After a short wait — still open
        await asyncio.sleep(0.05)
        assert await cb.is_open()

    _run(_run_test())


# --- 5. Success before threshold resets failure count ----------------------

def test_breaker_resets_on_first_success():
    """A success after 2 failures (threshold=5) should reset the count."""
    cb = CircuitBreaker(threshold=5, cooldown=60.0)

    async def _run_test():
        # 2 failures
        await cb.record_failure()
        await cb.record_failure()
        assert cb._failures == 2
        # 1 success — resets
        await cb.record_success()
        assert cb._failures == 0
        # 4 more failures — still under threshold
        for _ in range(4):
            await cb.record_failure()
        assert not await cb.is_open()
        # 5th failure — opens
        await cb.record_failure()
        assert await cb.is_open()

    _run(_run_test())


# --- 6. analyze_token uses circuit breaker (returns None fast) ------------

def test_analyze_token_uses_circuit_breaker():
    """When breaker is open, analyze_token should return None in <10ms
    without making any API call.
    """
    async def _run_test():
        client = PioneerLLMClient()
        # Force breaker open
        for _ in range(CB_THRESHOLD):
            await client.circuit_breaker.record_failure()
        assert await client.circuit_breaker.is_open()

        # Now call analyze_token — should return None immediately
        start = time.time()
        result = await client.analyze_token("system", "user")
        elapsed = time.time() - start

        assert result is None
        # Should be near-instant (no LLM call was made)
        assert elapsed < 0.1, f"analyze_token took {elapsed}s when breaker was open"

    _run(_run_test())


# --- 7. analyze_batch with open breaker (all None, fast) ------------------

def test_analyze_batch_handles_open_breaker():
    """When breaker is open, analyze_batch should return all-None in parallel
    (not one-by-one serially).
    """
    async def _run_test():
        client = PioneerLLMClient()
        # Force breaker open
        for _ in range(CB_THRESHOLD):
            await client.circuit_breaker.record_failure()

        prompts = [("system_a", "user_a"), ("system_b", "user_b"), ("system_c", "user_c")]
        start = time.time()
        results = await client.analyze_batch(prompts)
        elapsed = time.time() - start

        assert all(r is None for r in results), f"expected all None, got {results}"
        # Should be near-instant — all calls short-circuited
        assert elapsed < 0.2, f"analyze_batch took {elapsed}s with open breaker"

    _run(_run_test())


# --- Bonus: ensure analyze_token records failures on error -----------------

def test_analyze_token_records_failure_on_error():
    """When LLM throws an exception, analyze_token should record a failure
    on the circuit breaker.
    """
    async def _run_test():
        client = PioneerLLMClient()
        # Mock the inner client to raise
        client.client.chat.completions.create = AsyncMock(
            side_effect=Exception("API down")
        )

        # First call: fails 1 time, returns None
        result = await client.analyze_token("system", "user", retries=0)
        assert result is None
        assert client.circuit_breaker._failures == 1
        assert not await client.circuit_breaker.is_open()

    _run(_run_test())
