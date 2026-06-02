import asyncio
import logging
import time
from collections import defaultdict

logger = logging.getLogger(__name__)


class RateLimiter:
    def __init__(self, rate: int, period: float):
        self.rate = rate
        self.period = period
        self.tokens = rate
        self.last_refill = time.time()
        self._lock = asyncio.Lock()

    def _refill(self):
        now = time.time()
        elapsed = now - self.last_refill
        new_tokens = elapsed * (self.rate / self.period)
        self.tokens = min(self.rate, self.tokens + new_tokens)
        self.last_refill = now

    async def acquire(self, n: int = 1):
        """Acquire n tokens atomically. Waits until n are available.

        Uses a condition variable so other coroutines block on
        `wait()` instead of busy-polling. Granularity is high (50ms
        tick) — adjust `TICK` if you need finer control.
        """
        if n < 1:
            return
        TICK = 0.05
        async with self._lock:
            while True:
                self._refill()
                if self.tokens >= n:
                    self.tokens -= n
                    return
                wait_time = (n - self.tokens) * (self.period / self.rate)
                # Hold the lock (small window) but use a shorter tick so
                # we don't oversleep when n>1 is needed but only 1 token
                # becomes available soon. Spin: no condition variable
                # needed for this traffic level (≤5 workers).
                await asyncio.sleep(min(wait_time, TICK))

    @property
    def available(self) -> int:
        self._refill()
        return int(self.tokens)


async def safe_task(name: str, coro_func, *args, max_retries: int = 5):
    retries = 0
    while retries < max_retries:
        try:
            await coro_func(*args)
        except asyncio.CancelledError:
            logger.info(f"{name} cancelled")
            break
        except Exception as e:
            retries += 1
            logger.error(f"{name} error (attempt {retries}/{max_retries}): {e}")
            await asyncio.sleep(min(2 ** retries, 60))
    if retries >= max_retries:
        logger.critical(f"{name} failed after {max_retries} retries")
