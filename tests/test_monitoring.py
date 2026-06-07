"""Tests for FailureTracker."""
import asyncio
import sys
import time

sys.path.insert(0, "/Users/khezuma/workspace/trenching")

from core.monitoring import FailureTracker


class FakeAlertFn:
    def __init__(self):
        self.calls = []

    async def __call__(self, msg: str):
        self.calls.append(msg)


def test_tracker_alerts_on_3rd_consecutive_failure():
    async def go():
        alert = FakeAlertFn()
        tracker = FailureTracker("test", alert, threshold=3, cooldown_seconds=300)
        async def fail():
            raise RuntimeError("boom")
        # 1st and 2nd failures should NOT alert
        await tracker.run(fail)
        await tracker.run(fail)
        assert alert.calls == [], f"should not alert before threshold, got {len(alert.calls)}"
        # 3rd failure triggers alert
        await tracker.run(fail)
        assert len(alert.calls) == 1, f"should alert at threshold"
        assert "3x in a row" in alert.calls[0]
        assert "RuntimeError: boom" in alert.calls[0]
    asyncio.run(go())


def test_tracker_resets_counter_on_success():
    async def go():
        alert = FakeAlertFn()
        tracker = FailureTracker("test", alert, threshold=3, cooldown_seconds=300)
        async def fail():
            raise RuntimeError("x")
        async def succeed():
            return "ok"
        # 2 fails
        await tracker.run(fail)
        await tracker.run(fail)
        # success resets
        await tracker.run(succeed)
        assert tracker.consecutive_failures == 0
        # 2 more fails → still not at threshold (counter was reset)
        await tracker.run(fail)
        await tracker.run(fail)
        assert alert.calls == [], f"counter should have been reset, no alert yet"
    asyncio.run(go())


def test_tracker_respects_alert_cooldown():
    async def go():
        alert = FakeAlertFn()
        tracker = FailureTracker("test", alert, threshold=3, cooldown_seconds=0.5)
        async def fail():
            raise RuntimeError("x")
        # 3 fails → 1st alert
        for _ in range(3):
            await tracker.run(fail)
        assert len(alert.calls) == 1
        # 3 more fails immediately → no 2nd alert (within cooldown)
        for _ in range(3):
            await tracker.run(fail)
        assert len(alert.calls) == 1, f"cooldown should prevent 2nd alert, got {len(alert.calls)}"
        # Wait for cooldown
        await asyncio.sleep(0.6)
        # 3 more fails → 2nd alert
        for _ in range(3):
            await tracker.run(fail)
        assert len(alert.calls) == 2, f"after cooldown, should alert again"
    asyncio.run(go())


def test_tracker_alert_fn_failure_does_not_crash():
    async def go():
        async def broken_alert(msg):
            raise ConnectionError("telegram down")
        tracker = FailureTracker("test", broken_alert, threshold=3, cooldown_seconds=300)
        async def fail():
            raise RuntimeError("x")
        # Should not raise even though alert_fn fails
        for _ in range(3):
            err = await tracker.run(fail)
            assert err is not None
    asyncio.run(go())


def test_tracker_returns_none_on_success():
    async def go():
        alert = FakeAlertFn()
        tracker = FailureTracker("test", alert)
        async def succeed():
            return "ok"
        result = await tracker.run(succeed)
        assert result is None
        assert tracker.total_successes == 1
    asyncio.run(go())


def test_tracker_returns_exception_on_failure():
    async def go():
        alert = FakeAlertFn()
        tracker = FailureTracker("test", alert)
        async def fail():
            raise ValueError("bad")
        result = await tracker.run(fail)
        assert result is not None
        assert isinstance(result, ValueError)
        assert tracker.total_failures == 1
    asyncio.run(go())


def test_tracker_stats():
    async def go():
        alert = FakeAlertFn()
        tracker = FailureTracker("test", alert, threshold=10)
        async def fail():
            raise RuntimeError("x")
        async def succeed():
            return None
        await tracker.run(succeed)
        await tracker.run(fail)
        await tracker.run(succeed)
        stats = tracker.stats()
        assert stats["name"] == "test"
        assert stats["total_successes"] == 2
        assert stats["total_failures"] == 1
        assert stats["consecutive_failures"] == 0
        assert stats["last_error"] is None
    asyncio.run(go())
