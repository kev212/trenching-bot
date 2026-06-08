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

        Lock is only held during the check-and-decrement, never during
        sleep.  This lets other workers proceed in parallel instead of
        serialising behind a single sleeping holder.
        """
        if n < 1:
            return
        TICK = 0.05
        while True:
            async with self._lock:
                self._refill()
                if self.tokens >= n:
                    self.tokens -= n
                    return
                wait_time = (n - self.tokens) * (self.period / self.rate)
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


async def _wrap_coro(coro):
    """Wrap a coroutine so exceptions are returned as values (not raised).
    Needed because asyncio.wait(FIRST_COMPLETED) doesn't support
    return_exceptions natively like asyncio.gather does.
    """
    try:
        return await coro
    except BaseException as e:
        return e


async def safe_gather(*coros, timeout: float = None):
    """asyncio.gather dengan timeout yang handle cancellation cleanly
    AND bisa return partial results.

    Workaround untuk Python 3.9-3.11 bug (cpython#102988): 
    ``asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=N)``
    gak ngerapihin CancelledError di inner tasks saat timeout, ngehasilin
    ``_GatheringFuture exception was never retrieved`` warning.

    Pakai ``asyncio.wait(ALL_COMPLETED)`` instead.
    """
    if not coros:
        return []

    wrapped = [_wrap_coro(c) for c in coros]

    if timeout is None:
        return await asyncio.gather(*wrapped)

    tasks = [asyncio.ensure_future(w) for w in wrapped]
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    for t in pending:
        t.cancel()
        try:
            await t
        except BaseException:
            pass

    results = []
    for t in tasks:
        if t in done and not t.cancelled():
            results.append(t.result())
        else:
            results.append(asyncio.TimeoutError())
    return results
