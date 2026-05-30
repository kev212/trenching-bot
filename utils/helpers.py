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

    async def acquire(self):
        async with self._lock:
            self._refill()
            while self.tokens < 1:
                wait_time = (1 - self.tokens) * (self.period / self.rate)
                await asyncio.sleep(min(wait_time, 0.1))
                self._refill()
            self.tokens -= 1

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
