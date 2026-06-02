import time
import asyncio
from collections import OrderedDict
from typing import Optional


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

    @property
    def size(self) -> int:
        return len(self._cache)


RETRY_BACKOFF = {0: 60, 1: 180, 2: 300}  # retry index -> delay in seconds
MAX_RETRIES = 3


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

    async def is_duplicate(self, address: str) -> bool:
        return await self.processed_cache.has(address)

    async def mark_processed(self, address: str):
        await self.processed_cache.set(address, True)

    async def add_retry(self, address: str, symbol: str = "?", name: str = "?", failed_filters: list[str] = None):
        async with self._lock:
            if address in self.retry_queue:
                self.retry_queue[address]["retries"] += 1
                self.retry_queue[address]["timestamp"] = time.time()
            else:
                self.retry_queue[address] = {
                    "timestamp": time.time(), "retries": 0,
                    "symbol": symbol, "name": name,
                    "failed_filters": failed_filters or [],
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

    async def cleanup_retry_queue(self):
        from analysis.filters import is_permanent_failure
        async with self._lock:
            now = time.time()
            max_stale = get_retry_delay(MAX_RETRIES - 1) * 2
            expired = [k for k, v in self.retry_queue.items()
                       if v["retries"] >= MAX_RETRIES
                       or now - v["timestamp"] > max_stale
                       or is_permanent_failure(v.get("failed_filters", []))]
            for k in expired:
                del self.retry_queue[k]

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
