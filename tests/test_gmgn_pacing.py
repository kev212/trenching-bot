"""Tests for GMGN pacing: lock + 2500ms minimum interval between requests.

Charon's `paceGmgnRequest` pattern — serializes all requests through an
asyncio.Lock and ensures a minimum delay so we never trigger GMGN's
rate limit cascade.
"""
import asyncio
import sys
import time

sys.path.insert(0, "/Users/khezuma/workspace/trenching")

from sources.gmgn import (
    GMGNClient,
    GMGN_DEFAULT_REQUEST_DELAY_MS,
)


def _make_client():
    return GMGNClient(api_key="test_key")


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_pace_lock_lazy():
    """Pace lock is None until first call to _pace_request()."""
    client = _make_client()
    assert client._pace_lock is None
    assert client._last_request_time == 0.0


def test_pace_lock_creates_on_first_call():
    """First _pace_request() initializes the lock."""
    client = _make_client()
    _run(client._pace_request())
    assert client._pace_lock is not None
    assert isinstance(client._pace_lock, asyncio.Lock)


def test_pace_request_first_call_no_delay():
    """First request has no prior _last_request_time, so no wait."""
    client = _make_client()
    start = time.time()
    _run(client._pace_request())
    elapsed = time.time() - start
    # First call should be effectively instant (well under 100ms)
    assert elapsed < 0.1
    # And _last_request_time should be set
    assert client._last_request_time > 0.0


def test_pace_request_enforces_minimum_interval():
    """Second call within window sleeps until 2500ms after first."""
    client = _make_client()
    _run(client._pace_request())  # first — no wait
    first_end = client._last_request_time

    start = time.time()
    _run(client._pace_request())  # second — should wait
    elapsed = time.time() - start

    # Should have waited at least ~2500ms (or close to it)
    # Allow some tolerance for asyncio scheduling
    assert elapsed >= (GMGN_DEFAULT_REQUEST_DELAY_MS / 1000.0) - 0.3, (
        f"Second call returned in {elapsed:.3f}s, expected ~2.5s"
    )
    # And _last_request_time should be updated to after the wait
    assert client._last_request_time >= first_end


def test_pace_request_serializes_concurrent_callers():
    """Two concurrent callers cannot interleave — lock enforces order."""
    client = _make_client()
    call_log = []

    async def _make_call(name):
        await client._pace_request()
        call_log.append((name, time.time()))

    async def _all():
        t0 = time.time()
        await asyncio.gather(
            _make_call("a"),
            _make_call("b"),
            _make_call("c"),
        )
        return time.time() - t0

    total = _run(_all())
    # 3 calls serial = 2 gaps × 2.5s = ~5.0s minimum
    assert total >= 4.5, f"3 serial calls took {total:.2f}s, expected ~5.0s"
    # Each call must be at least 2.5s after previous
    for i in range(1, len(call_log)):
        gap = call_log[i][1] - call_log[i - 1][1]
        assert gap >= 2.3, f"Gap between call {i-1} and {i} was {gap:.2f}s"


def test_pace_request_updates_last_time():
    """_last_request_time is updated to current time after the wait."""
    client = _make_client()
    _run(client._pace_request())
    t1 = client._last_request_time
    # 2.5s+ must elapse to avoid waiting in the next call
    time.sleep(2.6)
    _run(client._pace_request())  # no wait, 2.5s already passed
    t2 = client._last_request_time
    # t2 should be > t1 (the second call updated it)
    assert t2 > t1
