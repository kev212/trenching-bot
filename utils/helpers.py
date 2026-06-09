import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any, Optional

logger = logging.getLogger(__name__)


class RateLimiter:
    """Async token-bucket rate limiter with event-based waiter notification.

    Uses an internal ``asyncio.Lock`` so that *all* token bookkeeping
    (refill, check, deduct) is atomic.  A background ``asyncio.Event``
    lets waiting coroutines sleep efficiently and wake up as soon as
    tokens become available rather than busy-looping on a fixed tick.
    """

    def __init__(self, rate: int, period: float):
        self.rate = rate
        self.period = period
        self._tokens = float(rate)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()
        self._event = asyncio.Event()
        if self._tokens >= 1:
            self._event.set()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _refill(self) -> None:
        """Refill tokens based on elapsed time.

        **Must** be called while ``self._lock`` is held.
        """
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed <= 0:
            return
        new_tokens = elapsed * (self.rate / self.period)
        if new_tokens > 0:
            self._tokens = min(float(self.rate), self._tokens + new_tokens)
            self._last_refill = now
            if self._tokens >= 1:
                self._event.set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def acquire(self, n: int = 1) -> None:
        """Acquire *n* tokens, blocking until they are available.

        Raises ``ValueError`` if *n* exceeds the burst size (``self.rate``).
        """
        if n < 1:
            return
        if n > self.rate:
            raise ValueError(
                f"Cannot acquire {n} tokens at once (max burst: {self.rate})"
            )

        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= n:
                    self._tokens -= n
                    if self._tokens < 1:
                        # No tokens left — clear the event so future
                        # callers block instead of spinning.
                        self._event.clear()
                    return

            # Not enough tokens yet — sleep for a short tick.  The event
            # lets us wake early if a refill happens (triggered by another
            # caller's _refill inside the lock).
            try:
                await asyncio.wait_for(self._event.wait(), timeout=0.05)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                continue

    async def available(self) -> int:
        """Return the number of tokens currently available.

        This is a snapshot — the value may change immediately after
        returning.
        """
        async with self._lock:
            self._refill()
            return int(self._tokens)


async def safe_task(
    name: str,
    coro_func: Callable[..., Awaitable[Any]],
    *args: Any,
    max_retries: int = 5,
    on_error: Optional[Callable[[Exception, int], Awaitable[None]]] = None,
) -> None:
    """Run *coro_func* with exponential-backoff retries.

    Parameters
    ----------
    name:
        Human-readable label used in log messages.
    coro_func:
        Async callable to invoke.
    *args:
        Positional arguments forwarded to *coro_func*.
    max_retries:
        Maximum number of retries before giving up (default 5).
    on_error:
        Optional async callback invoked on each error.  Receives the
        exception instance and the attempt number (1-based).
    """
    retries = 0
    while retries < max_retries:
        try:
            await coro_func(*args)
            return  # success
        except asyncio.CancelledError:
            logger.info("%s cancelled", name)
            raise  # let cancellation propagate
        except Exception as e:
            retries += 1
            logger.error("%s error (attempt %d/%d): %s", name, retries, max_retries, e)
            if on_error is not None:
                try:
                    await on_error(e, retries)
                except Exception:
                    logger.exception("on_error callback failed for %s", name)
            if retries < max_retries:
                await asyncio.sleep(min(2**retries, 60))

    logger.critical("%s failed after %d retries", name, max_retries)


async def _wrap_coro(coro: Awaitable[Any]) -> Any:
    """Wrap a coroutine so regular exceptions are returned as values.

    :class:`asyncio.CancelledError`, :exc:`KeyboardInterrupt` and
    :exc:`SystemExit` are **not** caught so they propagate normally.
    """
    try:
        return await coro
    except Exception as e:
        return e


async def safe_gather(
    *coros: Awaitable[Any],
    timeout: Optional[float] = None,
) -> list[Any]:
    """Gather multiple coroutines with a timeout and clean cancellation.

    Unlike ``asyncio.wait_for(asyncio.gather(...))`` this helper avoids
    ``_GatheringFuture exception was never retrieved`` warnings (see
    cpython#102988 for Python 3.9--3.11).

    Results are returned in the same order as the input coroutines.
    Exceptions raised by individual coroutines are returned as-is (not
    re-raised).  If a timeout occurs, incomplete coroutines get
    ``asyncio.TimeoutError`` placeholders.
    """
    if not coros:
        return []

    wrapped = [_wrap_coro(c) for c in coros]

    if timeout is None:
        # return_exceptions=True ensures that even CancelledError from an
        # internally-cancelled coroutine is captured rather than crashing
        # the whole gather.  (CancelledError from *external* cancellation
        # of the whole gather task still propagates normally.)
        return await asyncio.gather(*wrapped, return_exceptions=True)

    tasks = [asyncio.ensure_future(w) for w in wrapped]
    done, pending = await asyncio.wait(tasks, timeout=timeout)

    # Clean up pending tasks — cancel them and let them finish quietly.
    if pending:
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    # Collect results in input order.
    results: list[Any] = []
    for t in tasks:
        if t.cancelled():
            results.append(asyncio.TimeoutError())
        else:
            exc = t.exception()
            if exc is not None:
                results.append(exc)
            else:
                results.append(t.result())
    return results


def _log_exception(task: asyncio.Task[Any]) -> None:
    """Log an unhandled exception from a task.

    Use as ``task.add_done_callback(_log_exception)`` to ensure no
    exception is silently swallowed.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "Unhandled exception in task %s: %s",
            task.get_name(),
            exc,
            exc_info=exc,
        )


class LRUSet:
    """OrderedDict-based set with max size and TTL expiry.

    When full, oldest entries are evicted first (LRU behavior).
    Entries older than *ttl_seconds* are evicted on access/check.
    Safe for single-threaded async use (no locking needed).
    """

    def __init__(self, max_size: int = 10000, ttl_seconds: float = 3600):
        self._dict: OrderedDict[str, float] = OrderedDict()
        self.max_size = max_size
        self.ttl = ttl_seconds

    def add(self, item: str) -> None:
        self._dict[item] = time.time()
        self._evict_if_needed()

    def __contains__(self, item: str) -> bool:
        ts = self._dict.get(item)
        if ts is None:
            return False
        if time.time() - ts > self.ttl:
            del self._dict[item]
            return False
        return True

    def discard(self, item: str) -> None:
        self._dict.pop(item, None)

    def _evict_if_needed(self) -> None:
        while len(self._dict) > self.max_size:
            self._dict.popitem(last=False)

    def __len__(self) -> int:
        return len(self._dict)

    def clear(self) -> None:
        self._dict.clear()
