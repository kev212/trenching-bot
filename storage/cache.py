import time
import asyncio
import logging
from collections import OrderedDict
from typing import Optional

logger = logging.getLogger("cache")


class LRUCache:
    def __init__(self, max_size: int = 10000, ttl: int = 3600):
        self.max_size = max_size
        self.ttl = ttl
        self._cache: OrderedDict[str, tuple[float, any]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[any]:
        async with self._lock:
            if key not in self._cache:
                return None
            ts, value = self._cache[key]
            if time.time() - ts > self.ttl:
                del self._cache[key]
                return None
            self._cache.move_to_end(key)
            return value

    async def set(self, key: str, value: any):
        async with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            self._cache[key] = (time.time(), value)
            if len(self._cache) > self.max_size:
                self._cache.popitem(last=False)

    async def has(self, key: str) -> bool:
        return (await self.get(key)) is not None

    async def remove(self, key: str):
        async with self._lock:
            self._cache.pop(key, None)

    async def cleanup(self):
        async with self._lock:
            now = time.time()
            expired = [k for k, (ts, _) in self._cache.items() if now - ts > self.ttl]
            for k in expired:
                del self._cache[k]

    async def keys_snapshot(self) -> set:
        """Atomic snapshot of all non-expired keys.

        For callers that need to read membership without acquiring the lock
        themselves (lock-free read path). Returns a fresh set.
        """
        async with self._lock:
            now = time.time()
            return {k for k, (ts, _) in self._cache.items() if now - ts <= self.ttl}

    @property
    def size(self) -> int:
        return len(self._cache)


RETRY_BACKOFF = {0: 60, 1: 180, 2: 300}  # retry index -> delay in seconds
MAX_RETRIES = 3
IN_FLIGHT_TTL = 300  # 5 min — auto-purge stale in-flight entries


def get_retry_delay(retries: int) -> int:
    return RETRY_BACKOFF.get(retries, 300)


class SharedState:
    def __init__(self):
        self._lock = asyncio.Lock()
        self.processed_cache = LRUCache(max_size=50000, ttl=3600)
        self.active_calls: dict[str, any] = {}
        self._filter_params: dict = {}
        self._filter_params_version: int = 0
        self.metrics = Metrics()
        self.queue: asyncio.Queue = None
        self.retry_queue: dict[str, dict] = {}  # {address: {timestamp, retries}}
        # In-flight set: addresses currently being processed by a worker.
        # {address: timestamp} — auto-purged after IN_FLIGHT_TTL to prevent leaks.
        self._in_flight: dict[str, float] = {}
        # Live trading refs (set by TrenchingBot after init so that Telegram
        # /live_* commands can read them through bot_data['state']). Defaults
        # keep them safe to read even before TrenchingBot has wired them up.
        self.position_manager = None
        self.risk_manager = None
        self.gmgn_cli = None
        self.paper_mode: bool | None = None
        self._live_paused: bool = False

    async def _purge_stale_in_flight(self):
        """Auto-release in-flight entries older than TTL. Called inside _lock."""
        now = time.time()
        stale = [k for k, ts in self._in_flight.items() if now - ts > IN_FLIGHT_TTL]
        for k in stale:
            del self._in_flight[k]

    async def is_in_flight(self, address: str) -> bool:
        """Check if address is currently being processed. Auto-purges stale entries."""
        async with self._lock:
            await self._purge_stale_in_flight()
            return address in self._in_flight

    async def claim(self, address: str) -> bool:
        """Atomically claim address for processing. Returns True if claimed, False if already in flight."""
        async with self._lock:
            await self._purge_stale_in_flight()
            if address in self._in_flight:
                return False
            self._in_flight[address] = time.time()
            return True

    async def release(self, address: str):
        """Release address from in-flight set."""
        async with self._lock:
            self._in_flight.pop(address, None)

    async def in_flight_count(self) -> int:
        async with self._lock:
            await self._purge_stale_in_flight()
            return len(self._in_flight)

    async def snapshot_in_flight(self) -> set:
        """Atomic snapshot of current in-flight addresses.

        For callers that need to read is_in_flight without re-acquiring the lock
        (e.g. when the caller is ALREADY holding it for a different operation).
        Returns a fresh set so callers can mutate freely.
        """
        async with self._lock:
            await self._purge_stale_in_flight()
            return set(self._in_flight.keys())

    async def snapshot_processed(self) -> set:
        """Atomic snapshot of processed (already-handled) addresses.

        For callers that need to read is_duplicate without re-acquiring locks.
        Returns a fresh set; safe to mutate.
        """
        return set(await self.processed_cache.keys_snapshot())

    async def is_duplicate(self, address: str) -> bool:
        return await self.processed_cache.has(address)

    async def mark_processed(self, address: str):
        await self.processed_cache.set(address, True)

    async def add_retry(self, address: str, symbol: str = "?", name: str = "?", failed_filters: list[str] = None):
        async with self._lock:
            if address in self.retry_queue:
                # C8 fix: accumulate failed_filters across attempts (set union).
                # Previously `failed_filters` was OVERWRITTEN each call, so
                # a retry that passed filter A but failed filter B would
                # lose the A pass history. is_permanent_failure() then
                # could not correctly see all historical failures.
                existing = self.retry_queue[address]
                merged = set(existing.get("failed_filters", []) or []) | \
                         set(failed_filters or [])
                existing["retries"] += 1
                existing["timestamp"] = time.time()
                existing["failed_filters"] = list(merged)
            else:
                self.retry_queue[address] = {
                    "timestamp": time.time(), "retries": 0,
                    "symbol": symbol, "name": name,
                    "failed_filters": list(failed_filters or []),
                }

    async def should_retry(self, address: str) -> bool:
        async with self._lock:
            if address not in self.retry_queue:
                return False
            info = self.retry_queue[address]
            if info["retries"] >= MAX_RETRIES:
                return False
            delay = get_retry_delay(info["retries"])
            if time.time() - info["timestamp"] < delay:
                return False
            return True

    async def get_retry_info(self, address: str) -> dict:
        async with self._lock:
            return self.retry_queue.get(address, {})

    async def remove_retry(self, address: str):
        async with self._lock:
            self.retry_queue.pop(address, None)

    async def bump_retry_timestamp(self, address: str, delay: int = 60) -> None:
        """Push back the next-eligible time for a retry entry by `delay` seconds.

        Used when a worker had to drop a re-queued token (e.g. queue stayed full
        for the put_nowait backoff window). Without bumping, the scheduler would
        keep trying to re-queue the same token every scan (30s) until the 10-min
        cleanup_retry_queue max_stale kicks in. With a bump, the token stays in
        retry_queue but its backoff timer resets so the next attempt happens
        after `delay` seconds instead of immediately on next scan.

        FIX #6: see main.py worker retry-back path.
        """
        async with self._lock:
            if address in self.retry_queue:
                self.retry_queue[address]["timestamp"] = time.time() + delay

    async def cleanup_retry_queue(self) -> int:
        """Clean up stale retry entries. Holds lock briefly — expensive checks run outside lock.

        Bug #14 fix: Previously called is_permanent_failure() while holding
        self._lock, which could block all workers for seconds if the queue
        had thousands of entries. Now we snapshot the cheap checks under lock,
        process expensive is_permanent_failure() outside, then delete under lock.

        FIX B2: now returns the count of removed entries so the caller
        (retry_scheduler) can include it in the [RETRY-SCHED] log. Previously
        cleanup was completely silent — no count, no log — so the user had
        no visibility into tokens force-removed by age > 10min or permanent
        failure. Also emits [CLEANUP] log when removals > 0.
        """
        from analysis.filters import is_permanent_failure
        # Phase 1: snapshot cheap checks under lock (timer-based expiry)
        async with self._lock:
            now = time.time()
            max_stale = get_retry_delay(MAX_RETRIES - 1) * 2
            expired_by_age = [
                k for k, v in self.retry_queue.items()
                if v["retries"] >= MAX_RETRIES
                or now - v["timestamp"] > max_stale
            ]
            # Snapshot items needing expensive is_permanent_failure check
            to_check = {
                k: v.get("failed_filters", [])
                for k, v in self.retry_queue.items()
                if k not in expired_by_age
            }
        # Phase 2: process expensive check outside lock
        expired_by_failure = [
            k for k, failed_filters in to_check.items()
            if is_permanent_failure(failed_filters)
        ]
        # Phase 3: delete under lock
        async with self._lock:
            for k in expired_by_age:
                self.retry_queue.pop(k, None)
            for k in expired_by_failure:
                self.retry_queue.pop(k, None)
        removed = len(expired_by_age) + len(expired_by_failure)
        if removed > 0:
            logger.info(
                f"[CLEANUP] removed={removed} stale retry entries "
                f"(age={len(expired_by_age)} permanent={len(expired_by_failure)})"
            )
        return removed

    async def add_active_call(self, address: str, call):
        async with self._lock:
            self.active_calls[address] = call

    async def remove_active_call(self, address: str):
        async with self._lock:
            self.active_calls.pop(address, None)

    async def get_active_calls(self) -> dict:
        async with self._lock:
            return dict(self.active_calls)

    async def get_filter_params(self) -> dict:
        async with self._lock:
            return dict(self._filter_params)

    async def set_filter_params(self, params: dict, version: int):
        async with self._lock:
            self._filter_params = params
            self._filter_params_version = version

    async def get_filter_version(self) -> int:
        async with self._lock:
            return self._filter_params_version

    async def save_active_calls(self):
        """No-op: state.active_calls is in-memory only (Telegram /active sources
        from db.get_active_calls() instead — DB is the source of truth, this
        dict is reserved for future use).
        """
        pass

    async def load_filter_params(self):
        from config import load_filter_params
        data = load_filter_params()
        async with self._lock:
            self._filter_params = data.get("filters", {})
            self._filter_params_version = data.get("version", 1)


class Metrics:
    def __init__(self):
        self.calls_total = 0
        self.calls_ape = 0
        self.calls_watch = 0
        self.calls_skip = 0
        self.calls_skip_permanent = 0
        self.retry_attempts = 0
        self.retry_passes = 0
        self.retry_fails = 0
        self.wins = 0
        self.losses = 0
        self.alerts_sent = 0
        self.errors = 0
        self._start_time = time.time()

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self._start_time

    def record_call(self, verdict: str):
        self.calls_total += 1
        if verdict == "APE":
            self.calls_ape += 1
        elif verdict == "WATCH":
            self.calls_watch += 1
        elif verdict == "SKIP_PERMANENT":
            self.calls_skip_permanent += 1
        else:
            self.calls_skip += 1

    def record_retry(self, passed: bool):
        self.retry_attempts += 1
        if passed:
            self.retry_passes += 1
        else:
            self.retry_fails += 1

    def record_outcome(self, status: str):
        if status == "WIN":
            self.wins += 1
        elif status == "LOSS":
            self.losses += 1

    def record_alert(self):
        self.alerts_sent += 1

    def record_error(self):
        self.errors += 1

    @property
    def retry_success_rate(self) -> float:
        if self.retry_attempts == 0:
            return 0.0
        return self.retry_passes / self.retry_attempts * 100.0

    def to_dict(self) -> dict:
        return {
            "calls_total": self.calls_total,
            "calls_ape": self.calls_ape,
            "calls_watch": self.calls_watch,
            "calls_skip": self.calls_skip,
            "calls_skip_permanent": self.calls_skip_permanent,
            "retry_attempts": self.retry_attempts,
            "retry_passes": self.retry_passes,
            "retry_fails": self.retry_fails,
            "retry_success_rate": f"{self.retry_success_rate:.1f}%",
            "wins": self.wins,
            "losses": self.losses,
            "alerts_sent": self.alerts_sent,
            "errors": self.errors,
            "uptime_seconds": self.uptime_seconds,
        }
