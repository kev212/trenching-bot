"""Failure tracker for long-running tasks.

Wraps a coroutine with consecutive-failure tracking. After N consecutive
failures, sends a Telegram alert via alert_fn. Resets counter on first
success. Cooldown prevents alert spam during prolonged outages.

Adopted from Charon's `makeFailureTracker` pattern.
"""
import asyncio
import logging
import time
from typing import Callable, Awaitable, Any, Optional

logger = logging.getLogger("monitoring")


class FailureTracker:
    """Tracks consecutive failures of a long-running task; alerts on threshold."""

    def __init__(
        self,
        name: str,
        alert_fn: Callable[[str], Awaitable[None]],
        threshold: int = 3,
        cooldown_seconds: float = 300.0,
    ):
        self.name = name
        self.alert_fn = alert_fn
        self.threshold = threshold
        self.cooldown_seconds = cooldown_seconds
        self.consecutive_failures: int = 0
        self.last_alert_at: float = 0.0
        self.last_error: Optional[str] = None
        self.total_failures: int = 0
        self.total_successes: int = 0

    async def run(self, fn: Callable[[], Awaitable[Any]]) -> Optional[Exception]:
        """Run fn. Returns the exception (or None on success).

        On success: resets consecutive_failures counter, clears last_error.
        On failure: increments counter, may send alert if threshold reached
                    and cooldown elapsed.
        """
        try:
            await fn()
            if self.consecutive_failures > 0:
                logger.info(
                    f"[{self.name}] recovered after {self.consecutive_failures} consecutive failures"
                )
            self.consecutive_failures = 0
            self.last_error = None
            self.total_successes += 1
            return None
        except Exception as e:
            self.consecutive_failures += 1
            self.total_failures += 1
            self.last_error = f"{type(e).__name__}: {e}"
            logger.warning(
                f"[{self.name}] failed {self.consecutive_failures}/{self.threshold}: {self.last_error}"
            )

            if self.consecutive_failures >= self.threshold:
                now = time.time()
                if now - self.last_alert_at >= self.cooldown_seconds:
                    try:
                        msg = (
                            f"🚨 [{self.name}] failed {self.consecutive_failures}x in a row\n"
                            f"Last error: {self.last_error}\n"
                            f"Total failures: {self.total_failures} | "
                            f"Total successes: {self.total_successes}"
                        )
                        await self.alert_fn(msg)
                        self.last_alert_at = now
                        logger.info(
                            f"[{self.name}] Telegram alert sent (cooldown {self.cooldown_seconds}s)"
                        )
                    except Exception as alert_err:
                        logger.error(f"[{self.name}] alert_fn failed: {alert_err}")
            return e

    def stats(self) -> dict:
        """Return current state for inspection / metrics."""
        return {
            "name": self.name,
            "consecutive_failures": self.consecutive_failures,
            "total_failures": self.total_failures,
            "total_successes": self.total_successes,
            "last_error": self.last_error,
            "last_alert_at": self.last_alert_at,
        }
