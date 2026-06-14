import asyncio
import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("main")

from config import settings
from storage.database import Database
from storage.cache import SharedState, MAX_RETRIES
from sources.gmgn import GMGNClient
from sources.twitter import TwitterClient
from core.gmgn_cli import GMGNCli
from sources.web_scraper import WebScraper
from analysis.models import TokenData, CallRecord, CallStatus, Verdict
from analysis.filters import run_all_filters, check_hard_gate
from llm.pioneer_client import PioneerLLMClient
from llm.prompts import DECISION_SYSTEM, DECISION_USER, SOCIAL_ANALYSIS_SYSTEM, SOCIAL_ANALYSIS_USER
from llm.parser import parse_decision
from tracking.price_monitor import price_monitor
from tracking.strategy_poller import strategy_poller
from tracking.hourly_recap import hourly_recap
from learning.daily_optimizer import daily_optimizer
from learning.revert_monitor import revert_monitor
from alerts.formatter import format_alert
from alerts.dispatcher import dispatcher
from alerts.bot import bot_handler
from core.monitoring import FailureTracker
from utils.logger import setup_logger
from utils.helpers import RateLimiter, LRUSet, _log_exception

from core.wallet import Wallet
from core.jupiter_client import JupiterClient
from core.position_manager import PositionManager
from core.risk_manager import RiskManager
from core.trade_executor import TradeExecutor

from config import load_trading_config, load_risk_rules

logger = setup_logger("main")

MIN_FILTERS_FOR_LLM = 6


def _safe_float(val, default=0.0) -> float:
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _load_influencers() -> dict:
    """Load influencer list from config."""
    config_path = os.path.join(os.path.dirname(__file__), "config", "influencers.json")
    try:
        with open(config_path) as f:
            data = json.load(f)
            return data.get("influencers", {})
    except Exception as e:
        logger.warning(f"Failed to load influencers config: {e}")
        return {}


class TrenchingBot:
    def __init__(self):
        self.db = Database(settings.db_path)
        self.state = SharedState()
        self.queue = asyncio.Queue(maxsize=settings.max_queue_size)
        self.state.queue = self.queue
        self._live_paused = settings.live_paused_at_start  # /live_pause flag (set LIVE_PAUSED_AT_START=true for safe initial deploy)
        self.state._live_paused = self._live_paused
        self.rate_limiter = RateLimiter(15, 60)  # GMGN: 15 req/min
        self.gmgn = GMGNClient(settings.gmgn_api_key, settings.http_proxy, rate_limiter=self.rate_limiter)
        logger.warning(f"GMGN init: proxy=[{self.gmgn.proxy[:50] if self.gmgn.proxy else 'NONE'}]")
        self.twitter = TwitterClient()
        self.scraper = WebScraper()
        self.influencers = _load_influencers()
        logger.warning(f"Loaded {len(self.influencers)} influencers")
        self.llm_client = PioneerLLMClient()
        self.llm_rate_limiter = RateLimiter(10, 60)  # LLM: 10 req/min
        self.tasks = {}
        self.workers = []
        self.seen_trenches = LRUSet(max_size=10000, ttl_seconds=3600)
        self.shutdown_event = asyncio.Event()
        # Failure trackers for long-running tasks (Item #3)
        self.trackers: dict[str, FailureTracker] = {}
        # C4 fix: store shutdown task reference to prevent GC
        self._shutdown_task = None
        # Watchdog: track last activity timestamp
        self._last_activity = time.monotonic()
        # Track background tasks to prevent 'exception was never retrieved' warnings
        self._background_tasks: set[asyncio.Task] = set()

        # Trading components (Phase 1 paper mode)
        self.trading_config = load_trading_config()
        self.risk_rules = load_risk_rules()
        # paper_mode from Settings (auto-reads PAPER_MODE env var). trading.json paper_mode kept for backward compat only.
        self.paper_mode = settings.paper_mode if hasattr(settings, "paper_mode") else self.trading_config.get("paper_mode", True)
        self.state.paper_mode = self.paper_mode
        self.wallet = Wallet(
            paper=self.paper_mode,
            starting_balance_sol=settings.paper_starting_balance_sol,
            private_key_b58=settings.wallet_private_key,
            helius_api_key=settings.helius_api_key,
            helius_rpc_url=settings.helius_rpc_url,
        )
        self.jupiter = JupiterClient(
            proxy=settings.http_proxy,
            rate_limiter=self.rate_limiter,
        )
        self.position_manager = PositionManager(self.db)
        self.state.position_manager = self.position_manager
        self.risk_manager = RiskManager(
            self.trading_config,
            db=self.db,
            risk_rules=self.risk_rules,
            position_manager=self.position_manager,
        )
        self.state.risk_manager = self.risk_manager
        from core.price_oracle import PriceOracle
        self.price_oracle = PriceOracle(
            gmgn=self.gmgn,
            jupiter=self.jupiter,
            proxy=settings.http_proxy,
        )
        if not self.paper_mode and settings.wallet_pubkey:
            try:
                self.gmgn_cli = GMGNCli()
                if self.gmgn_cli.is_ready():
                    logger.warning(
                        f"[GMGN-CLI] initialized: pubkey={settings.wallet_pubkey[:12]}..."
                    )
                else:
                    logger.warning(
                        "[GMGN-CLI] installed but credentials missing "
                        "(~/.config/gmgn/.env); live trades disabled"
                    )
                    self.gmgn_cli = None
            except FileNotFoundError as e:
                logger.warning(f"[GMGN-CLI] not installed: {e}")
                self.gmgn_cli = None
        else:
            self.gmgn_cli = None
        self.state.gmgn_cli = self.gmgn_cli
        self.executor = TradeExecutor(
            paper=self.paper_mode,
            wallet=self.wallet,
            jupiter=self.jupiter,
            positions=self.position_manager,
            risk=self.risk_manager,
            config=self.trading_config,
            gmgn=self.gmgn,
            price_oracle=self.price_oracle,
            gmgn_cli=self.gmgn_cli,
        )
        # FIX C3: expose executor on SharedState so /close_all (alerts/bot.py:548)
        # can read `state.executor`. Previously only self.executor was set;
        # getattr(state, "executor", None) always returned None, making
        # /close_all reply "Live trading not initialized" forever.
        self.state.executor = self.executor
        logger.warning(
            f"Trading: paper_mode={self.paper_mode}, "
            f"position_size={self.trading_config.get('position_size_sol')} SOL, "
            f"reserve={self.wallet.RESERVE_SOL if hasattr(self.wallet, 'RESERVE_SOL') else 0.1} SOL"
        )

    def _make_tracker(self, name: str) -> FailureTracker:
        """Lazy-create a FailureTracker for a long-running task.

        Tracker alerts via Telegram via dispatcher.send_alert after 3
        consecutive failures, with 5-minute cooldown between alerts.
        """
        if name not in self.trackers:
            async def alert_fn(msg: str):
                try:
                    await dispatcher.send_alert(msg)
                except Exception as e:
                    logger.error(f"[{name}] telegram alert failed: {e}")
            self.trackers[name] = FailureTracker(
                name=name,
                alert_fn=alert_fn,
                threshold=3,
                cooldown_seconds=300.0,
            )
        return self.trackers[name]

    async def start(self):
        logger.info("=" * 50)
        logger.info("TRENCHING BOT v3 - Starting...")
        logger.info("=" * 50)

        await self.db.init()
        await self.state.load_filter_params()

        # C5 fix: eagerly initialize all HTTP sessions to prevent lazy-init race
        # that can leak AsyncSession instances and exhaust file descriptors.
        await self.jupiter.start()
        await self.price_oracle.start()
        await self.gmgn.start()
        await self.twitter.start()
        await self.scraper.start()
        # GMGNCli is a subprocess wrapper, no start() needed
        try:
            from sources.dexscreener import start_shared_session
            await start_shared_session()
        except Exception as e:
            logger.warning(f"DexScreener eager start failed (non-fatal): {e}")

        # Test GMGN connection
        logger.info("Testing GMGN API...")
        test = await self.gmgn.get_trending(limit=1)
        if test:
            logger.info(f"GMGN API OK - got {len(test)} tokens")
        else:
            logger.warning("GMGN API returned no data, will retry")

        loop = asyncio.get_event_loop()

        # C4 fix: robust signal handler — store task ref, set event on create_task failure
        def _on_signal():
            try:
                self._shutdown_task = asyncio.create_task(self.shutdown())
            except RuntimeError as e:
                logger.error(f"Signal handler create_task failed: {e}; setting shutdown event directly")
                self.shutdown_event.set()
            except Exception as e:
                logger.error(f"Signal handler unexpected error: {e}; setting shutdown event directly")
                self.shutdown_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _on_signal)
            except NotImplementedError:
                pass

        self._update_activity()  # initial heartbeat

        # Create all background tasks with tracking to prevent silent exception loss
        task_defs = {
            "gmgn_poller": self._gmgn_poller(),
            "trenches_poller": self._trenches_poller(),
            "retry_scheduler": self._retry_scheduler(),
            **{f"worker_{i}": self._token_worker(i)
               for i in range(settings.min_workers)},
            "price_monitor": self._run_forever("price_monitor", price_monitor, self.state, self.db),
            **({"strategy_poller": self._run_forever(
                "strategy_poller", strategy_poller,
                self.state, self.db, self.position_manager, self.gmgn_cli,
            )} if not self.paper_mode and self.gmgn_cli else {}),
            "hourly_recap": self._run_forever("hourly_recap", hourly_recap, self.state, self.db),
            "daily_optimizer": self._run_forever("daily_optimizer", daily_optimizer, self.state, self.db),
            "revert_monitor": self._run_forever("revert_monitor", revert_monitor, self.state, self.db),
            "bot_handler": self._run_forever("bot_handler", bot_handler, self.state, self.db),
            "metrics": self._metrics_loop(),
            "db_stats": self._db_stats_loop(),
            "watchdog": self._watchdog_loop(),
        }
        self.tasks = {}
        for name, coro in task_defs.items():
            task = asyncio.create_task(coro, name=name)
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
            task.add_done_callback(_log_exception)
            self.tasks[name] = task

        # Periodic cleanup for background tasks (runs every 5 min)
        async def _cleanup_background_tasks():
            while True:
                await asyncio.sleep(300)
                # trim any stray references; the set is self-cleaning via discard callback
                _ = len(self._background_tasks)

        cleanup = asyncio.create_task(_cleanup_background_tasks(), name="_cleanup_background_tasks")
        self._background_tasks.add(cleanup)
        cleanup.add_done_callback(self._background_tasks.discard)
        cleanup.add_done_callback(_log_exception)
        self.tasks["_cleanup_background_tasks"] = cleanup

        logger.info(f"Launched {len(self.tasks)} tasks")
        logger.info("Bot running! Press Ctrl+C to stop")

        try:
            await self.shutdown_event.wait()
        except asyncio.CancelledError:
            pass

    async def _run_forever(self, name, coro_func, *args):
        """Run coro_func(*args) forever with retry on failure.

        Caller passes exactly the args the coro_func expects — no auto-prepend.
        """
        retries = 0
        while True:
            try:
                await coro_func(*args)
                retries = 0
            except asyncio.CancelledError:
                raise  # Let shutdown proceed
            except Exception as e:
                retries += 1
                logger.error(f"{name} error ({retries}): {e}")
                if retries >= 5:
                    logger.critical(f"{name} failed 5 times, restarting in 60s")
                    await asyncio.sleep(60)
                    retries = 0
                else:
                    await asyncio.sleep(min(2 ** retries, 60))
            except BaseException:
                raise  # SystemExit/KeyboardInterrupt should propagate

    async def _gmgn_poller(self):
        logger.info("[TRENDING] Poller starting...")
        seen = LRUSet(max_size=10000, ttl_seconds=3600)
        poll_count = 0
        ban_count = 0
        base_delay = 60
        tracker = self._make_tracker("gmgn_poller")

        while True:
            self._update_activity()  # watchdog heartbeat
            try:
                # Backpressure: slow down or skip when queue is deep
                qsize = self.queue.qsize()
                if qsize >= 500:
                    sleep_time = min(120, base_delay * 4)
                    logger.warning(f"[TRENDING] backpressure: queue={qsize}, sleeping {sleep_time}s")
                    await asyncio.sleep(sleep_time)
                    continue
                elif qsize >= 200:
                    delay = min(base_delay * 3, 120)
                elif qsize >= 100:
                    delay = base_delay * 2
                else:
                    delay = base_delay

                tokens = await self.gmgn.get_trending(limit=20)
                ban_count = 0
                new_count = 0
                for token in tokens:
                    addr = token.get("address") or token.get("token_address")
                    if not addr:
                        continue

                    if addr in seen:
                        continue

                    if await self.state.is_duplicate(addr):
                        continue

                    # Skip if currently being processed by a worker (in-flight set, C3 fix)
                    if await self.state.is_in_flight(addr):
                        continue

                    # Skip ALL tokens currently in retry — retry scheduler handles them
                    if addr in self.state.retry_queue:
                        continue

                    seen.add(addr)

                    if not self._passes_prefilter(token):
                        continue

                    try:
                        self.queue.put_nowait(token)
                        new_count += 1
                    except asyncio.QueueFull:
                        logger.warning(f"[TRENDING] queue full ({self.queue.qsize()}), dropping new token {addr[:8]}")
                        break

                poll_count += 1
                if new_count > 0:
                    logger.info(f"[TRENDING] #{poll_count}: +{new_count} tokens (queue:{self.queue.qsize()})")

                await asyncio.sleep(delay)
                # Track success in failure tracker
                await tracker.run(self._ok_async)

            except Exception as e:
                err_str = str(e).upper()
                if "429" in err_str or "RATE_LIMIT" in err_str or "BANNED" in err_str:
                    # Rate-limit hits are NOT counted in FailureTracker — they
                    # are an expected, normal condition handled by GMGN's own
                    # backoff system. Only unexpected errors should alert.
                    ban_count += 1
                    wait_time = min(60 * (2 ** ban_count), 600)
                    logger.warning(f"[TRENDING] rate limited (ban #{ban_count}), waiting {wait_time}s...")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"[TRENDING] poller error: {e}")
                    await asyncio.sleep(base_delay)
                    await tracker.run(self._fail_with(e))

    async def _ok_async(self):
        """No-op success coroutine for tracker."""
        return None

    async def _fail_with(self, exc: Exception):
        """Re-raise a captured exception for tracker to count."""
        raise exc

    async def _trenches_poller(self):
        if not settings.enable_trenches_poller:
            logger.info("[TRENCHES] Poller disabled (set ENABLE_TRENCHES_POLLER=true to override)")
            return
        logger.info("[TRENCHES] Poller starting...")
        poll_count = 0
        ban_count = 0
        base_delay = 30
        tracker = self._make_tracker("trenches_poller")

        while True:
            self._update_activity()  # watchdog heartbeat
            try:
                # Backpressure: slow down or skip when queue is deep
                qsize = self.queue.qsize()
                if qsize >= 500:
                    sleep_time = min(120, base_delay * 6)
                    logger.warning(f"[TRENCHES] backpressure: queue={qsize}, sleeping {sleep_time}s")
                    await asyncio.sleep(sleep_time)
                    continue
                elif qsize >= 200:
                    delay = min(base_delay * 4, 120)
                elif qsize >= 100:
                    delay = base_delay * 2
                else:
                    delay = base_delay

                tokens = await self.gmgn.get_trenches(limit=20)
                ban_count = 0
                new_count = 0
                for token in tokens:
                    addr = token.get("address") or token.get("token_address")
                    if not addr:
                        continue
                    if addr in self.seen_trenches:
                        continue
                    if await self.state.is_duplicate(addr):
                        continue
                    # Skip if currently being processed by a worker (in-flight set, C3 fix)
                    if await self.state.is_in_flight(addr):
                        continue
                    # Skip ALL tokens currently in retry — retry scheduler handles them
                    if addr in self.state.retry_queue:
                        continue
                    self.seen_trenches.add(addr)

                    if not self._passes_prefilter(token):
                        continue

                    try:
                        self.queue.put_nowait(token)
                        new_count += 1
                    except asyncio.QueueFull:
                        logger.warning(f"[TRENCHES] queue full ({self.queue.qsize()}), dropping new token {addr[:8]}")
                        break

                poll_count += 1
                if new_count > 0:
                    logger.info(f"[TRENCHES] #{poll_count}: +{new_count} tokens (queue:{self.queue.qsize()})")

                await asyncio.sleep(delay)
                # Track success in failure tracker
                await tracker.run(self._ok_async)

            except Exception as e:
                err_str = str(e).upper()
                if "429" in err_str or "RATE_LIMIT" in err_str or "BANNED" in err_str:
                    # Rate-limit hits are expected — don't count in FailureTracker.
                    ban_count += 1
                    wait_time = min(60 * (2 ** ban_count), 600)
                    logger.warning(f"[TRENCHES] rate limited (ban #{ban_count}), waiting {wait_time}s...")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"[TRENCHES] poller error: {e}")
                    await asyncio.sleep(base_delay)
                    await tracker.run(self._fail_with(e))

    async def _retry_scheduler(self):
        """Periodically re-queue tokens whose retry delay has expired.

        Uses exponential backoff: retry 1 → 60s, retry 2 → 180s, retry 3 → 300s.
        Tokens with permanent filter failures (min_total_fee, market_cap bounds,
        min_holders) are dead-lettered immediately — they'll never pass.
        Respects queue backpressure: pauses re-queue when queue is deep.

        Fix #6 (June 2026 audit cycle 3): the inner work is chunked by
        `lock_budget` addresses per lock-hold so a retry_queue with
        thousands of entries can't starve all other SharedState consumers
        (workers calling is_duplicate/mark_processed/claim) for several
        seconds at a time.
        """
        from storage.cache import get_retry_delay, MAX_RETRIES
        from analysis.filters import is_permanent_failure
        logger.info("Retry scheduler started")
        base_interval = 30
        lock_budget = settings.retry_scheduler_lock_budget

        while True:
            try:
                now = time.time()
                requeued = 0
                skipped = 0
                dead_letter = 0
                processed_dedup = 0  # FIX B3: count silently-removed tokens
                # FIX NameError: cleanup_removed must be initialized here so
                # the [RETRY-SCHED] if-condition (line ~673) can reference it
                # before the assignment at line ~683. The previous placement
                # caused a NameError on every scan because Python evaluates
                # the if-condition BEFORE the cleanup_removed = await ... line.
                cleanup_removed = 0  # FIX B2: count cleanup removals (refilled below)

                qsize = self.queue.qsize()
                if qsize >= 300:
                    scan_interval = 120
                    await asyncio.sleep(scan_interval)
                    continue
                elif qsize >= 150:
                    scan_interval = 60
                else:
                    scan_interval = base_interval

                # A1: take an atomic snapshot of in_flight + processed_cache
                # so we can use them inside the retry_queue iteration without
                # re-acquiring SharedState._lock (deadlock avoidance).
                in_flight_snapshot = await self.state.snapshot_in_flight()
                processed_snapshot = await self.state.snapshot_processed()

                # Fix #6: chunked lock-hold. Process up to `lock_budget`
                # addresses per lock acquisition so other SharedState
                # consumers (workers) don't starve while we sweep a
                # large retry_queue.
                # FIX H1: track visited addresses to prevent infinite loop
                # when retry_queue has >= lock_budget items that are all
                # skipped (delay not met, in_flight, etc.). Without this,
                # the chunks re-process the same N oldest items every
                # iteration and the inner while never exits (because
                # len(items) stays == lock_budget, not less than). Now
                # each address is processed at most once per scan.
                visited = set()
                remaining = True
                while remaining:
                    async with self.state._lock:
                        # FIX #4: sort by timestamp ascending (oldest first) so a
                        # stuck front-of-queue entry can't starve newer entries at
                        # the back. dict insertion order is FIFO from add_retry,
                        # so if the first 50 entries are all in_flight or not-yet-
                        # expired, the back 50+ wait indefinitely until the front
                        # clears. Sorting by oldest timestamp ensures fair retry
                        # distribution across all queued tokens.
                        # FIX H1 (cont): filter out already-visited items so
                        # each address is only acted on once per scan cycle.
                        items = sorted(
                            (item for item in self.state.retry_queue.items()
                             if item[0] not in visited),
                            key=lambda x: x[1]["timestamp"],
                        )[:lock_budget]
                        if not items:
                            remaining = False
                            break
                        local_requeued = 0
                        local_skipped = 0
                        local_dead = 0
                        local_processed = 0  # FIX B3: count deduped tokens
                        expired_local = []
                        for addr, info in items:
                            # FIX H1: mark as visited as soon as we consider
                            # the address, so subsequent chunks within the
                            # same scan don't re-process it. Even items that
                            # are skipped (delay not met, in_flight) are
                            # marked visited — they'll be re-evaluated in the
                            # NEXT scan (30s later) when visited is reset.
                            visited.add(addr)
                            if info["retries"] >= MAX_RETRIES:
                                local_dead += 1
                                expired_local.append(addr)
                                continue
                            if is_permanent_failure(info.get("failed_filters", [])):
                                local_dead += 1
                                expired_local.append(addr)
                                dsym = info.get("symbol", "?")
                                logger.info(
                                    f"[DEAD-LETTER] {dsym} ({addr[:8]}): "
                                    "permanent filter, skipping retry"
                                )
                                continue
                            if addr in processed_snapshot:
                                expired_local.append(addr)
                                # FIX B3: count silent dedup so [RETRY-SCHED]
                                # log reflects all removals. Previously these
                                # tokens were popped from retry_queue without
                                # any counter increment — invisible in logs.
                                local_processed += 1
                                continue
                            delay = get_retry_delay(info["retries"])
                            if now - info["timestamp"] < delay:
                                # FIX #5: count delay-not-met as skipped so
                                # the [RETRY-SCHED] log accurately reflects
                                # the true number of tokens considered-but-
                                # not-acted-on this scan. Without this the
                                # log undercounts whenever the retry_queue
                                # has a backlog of tokens waiting for their
                                # backoff to expire.
                                local_skipped += 1
                                continue
                            if addr in in_flight_snapshot:
                                local_skipped += 1
                                continue
                            if qsize + local_requeued >= 200:
                                local_skipped += 1
                                continue
                            symbol = info.get("symbol", "?")
                            name = info.get("name", "?")
                            try:
                                # FIX #7: don't pass `retries` in queue item
                                # — the worker reads it from state.get_retry_info
                                # anyway (line 697), so the field was dead data.
                                # `_retry` is the only flag the worker actually
                                # checks (line 681: `if is_retry:`).
                                self.queue.put_nowait({
                                    "address": addr,
                                    "symbol": symbol,
                                    "name": name,
                                    "_retry": True,
                                })
                                local_requeued += 1
                            except asyncio.QueueFull:
                                local_skipped += 1
                        for addr in expired_local:
                            dead = self.state.retry_queue.pop(addr, None)
                            if dead and dead.get("retries", 0) >= MAX_RETRIES:
                                dsym = dead.get("symbol", "?")
                                logger.info(
                                    f"[DEAD-LETTER] {dsym} ({addr[:8]}): "
                                    f"exhausted {MAX_RETRIES} retries"
                                )
                        requeued += local_requeued
                        skipped += local_skipped
                        dead_letter += local_dead
                        processed_dedup += local_processed  # FIX B3
                        # If we got less than a full budget, queue is done
                        # (or nearly so) for this scan.
                        if len(items) < lock_budget:
                            remaining = False

                if requeued > 0 or dead_letter > 0 or skipped > 0 or processed_dedup > 0 or cleanup_removed > 0:
                    # FIX B1: re-read qsize AFTER re-queues so log shows the
                    # true post-scan queue state instead of the stale start-of-
                    # scan snapshot. Previously `queue={qsize}` always showed
                    # the value from line 556 (start), which was usually 0 even
                    # when we just re-queued 2 tokens. Now logs delta format
                    # `queue:0→2` so the user can see both pre and post values.
                    qsize_end = self.queue.qsize()
                    logger.info(
                        f"[RETRY-SCHED] requeued={requeued} skipped={skipped} "
                        f"dead={dead_letter} deduped={processed_dedup} "
                        f"cleanup={cleanup_removed} queue:{qsize}→{qsize_end}"
                    )

                # FIX B2: capture count returned by cleanup_retry_queue so
                # the [RETRY-SCHED] log reflects total removals (scheduler-
                # loop dead-letter + cleanup-stale). Previously cleanup was
                # completely silent — no count, no log. Note: cleanup only
                # triggers after tokens sit > 10min stale (max_stale) or hit
                # permanent failure, so cleanup_removed is usually 0.
                cleanup_removed = await self.state.cleanup_retry_queue()
                await asyncio.sleep(scan_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Retry scheduler error: {e}")
                await asyncio.sleep(base_interval)

    async def _token_worker(self, worker_id: int):
        logger.info(f"Worker {worker_id} started")
        processed = 0

        while True:
            try:
                token_info = await self.queue.get()
                addr = token_info.get("address") or token_info.get("token_address", "")
                symbol = token_info.get("symbol", "?") or "?"
                is_retry = bool(token_info.get("_retry"))

                if not addr:
                    continue

                if await self.state.is_duplicate(addr):
                    continue

                # C3 fix: atomically claim the address to prevent duplicate processing
                if not await self.state.claim(addr):
                    # Another worker is already processing this; skip
                    continue

                try:
                    if is_retry:
                        if not await self.state.should_retry(addr):
                            # Lost race — put back at end of queue, skip for now
                            # Block briefly waiting for space; if queue stays full
                            # too long, drop and log (item already dequeued once)
                            for _ in range(50):
                                try:
                                    self.queue.put_nowait(token_info)
                                    break
                                except asyncio.QueueFull:
                                    await asyncio.sleep(0.1)
                            else:
                                logger.warning(f"Worker {worker_id}: queue full on retry-back, dropped {addr[:8]}")
                                # FIX #6: bump the timestamp so the next scheduler
                                # scan doesn't immediately try to re-queue the same
                                # token (and the put_nowait that just failed). This
                                # spreads out the dropped-token retry attempts and
                                # prevents a tight loop where the same N tokens are
                                # repeatedly dropped because the queue stays full.
                                # Without this, dropped tokens retain their original
                                # timestamp and the scheduler would try to re-queue
                                # them every scan (30s) until max_stale=10min kicks
                                # in via cleanup_retry_queue. Bumping by RETRY_BACKOFF
                                # [0]=60s effectively pushes the next attempt to
                                # 60s out, matching normal retry cadence.
                                await self.state.bump_retry_timestamp(addr, delay=60)
                            continue

                    processed += 1
                    retry_count = (await self.state.get_retry_info(addr)).get("retries", 0)
                    # FIX A: align worker log with _process_token log [RETRY N/4].
                    # Previously worker showed "(retry_count/3)" which counted
                    # retries-done-so-far against MAX_RETRIES — different semantics
                    # from _process_token's "RETRY N/4" which counts current
                    # attempt against MAX_RETRIES+1=4. For SWALL on 3rd attempt
                    # the two lines showed "(1/3)" vs "RETRY 3/4" — both correct
                    # under their own formula but inconsistent to a reader.
                    # Now both use attempt = (retries+1)+(1 if is_retry else 0)
                    # and denominator MAX_RETRIES+1=4.
                    attempt = (retry_count + 1) + (1 if is_retry else 0)
                    logger.info(
                        f"[W{worker_id}] #{processed} {symbol} ({addr[:8]}...) "
                        f"attempt:{attempt}/{MAX_RETRIES + 1} q:{self.queue.qsize()}"
                    )

                    self._update_activity()  # watchdog heartbeat
                    try:
                        # FIX H2: no longer pass is_retry (signature is now
                        # 2-arg, derives is_retry from token_info internally).
                        await asyncio.wait_for(self._process_token(addr, token_info), timeout=180.0)
                    except asyncio.TimeoutError:
                        logger.warning(
                            f"[W{worker_id}] {symbol} ({addr[:8]}): "
                            f"_process_token timed out after 180s, skipping"
                        )
                    await asyncio.sleep(0.1)
                finally:
                    # C3 fix: always release the in-flight claim
                    await self.state.release(addr)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker error: {e}")
                self.state.metrics.record_error()
                await asyncio.sleep(1)

    def _passes_prefilter(self, token: dict) -> bool:
        """Quick pre-filter from poller data — no API call needed.

        Returns True if token passes hard gates (mc, holders, wash).
        Returns False + logs SKIP + records metric if rejected.
        """
        mc = token.get("market_cap", 0) or 0
        holder_count = token.get("holder_count", 0) or 0
        is_wash = token.get("is_wash_trading", False)
        symbol = token.get("symbol", "?") or "?"
        addr = (token.get("address") or token.get("token_address", ""))[:8]

        if mc <= 0:
            logger.info(f"[SKIP] {symbol} ({addr}): mc=0")
            self.state.metrics.record_call("SKIP"); return False
        if mc < 7000:
            logger.info(f"[SKIP] {symbol} ({addr}): mc=${mc:,.0f} < $7K")
            self.state.metrics.record_call("SKIP"); return False
        if mc > 200000:
            logger.info(f"[SKIP] {symbol} ({addr}): mc=${mc:,.0f} > $200K")
            self.state.metrics.record_call("SKIP"); return False
        if holder_count < 100:
            logger.info(f"[SKIP] {symbol} ({addr}): holders={holder_count} < 100")
            self.state.metrics.record_call("SKIP"); return False
        if is_wash:
            logger.info(f"[SKIP] {symbol} ({addr}): wash_trading=True")
            self.state.metrics.record_call("SKIP"); return False
        return True

    async def _process_token(self, address: str, token_info: dict):
        # FIX H2: removed dead `is_retry` parameter. Previously the
        # signature had `is_retry: bool = False` but it was immediately
        # overwritten on the next line from token_info["_retry"]. A future
        # caller passing is_retry=True would have it silently ignored.
        # Now we derive is_retry once from token_info (the source of
        # truth — the scheduler sets _retry=True when re-queueing).
        is_retry = bool(token_info.get("_retry"))
        symbol = (token_info.get("symbol") or token_info.get("name", "?")) or "?"
        # Rate limiting is handled internally by GMGNClient._get().
        # Stagger 0.3s prevents GMGN per-second burst (4 parallel calls).
        await asyncio.sleep(0.3)

        # Phase B: fetch token data in parallel batches
        try:
            from sources.gmgn import GMGN_GATHER_TIMEOUT
            from utils.helpers import safe_gather

            batch1 = safe_gather(
                self.gmgn.get_token_info(address),
                timeout=GMGN_GATHER_TIMEOUT,
            )
            batch2 = safe_gather(
                self.gmgn.get_token_ath(address),
                self.gmgn.get_kol_holders(address),
                timeout=GMGN_GATHER_TIMEOUT,
            )
            (info_r,), (ath_r, kol_holders_r) = await safe_gather(batch1, batch2, timeout=GMGN_GATHER_TIMEOUT)
            info = info_r if not isinstance(info_r, Exception) else {}
            ath_data = ath_r if not isinstance(ath_r, Exception) else {}
            kol_holders_list = kol_holders_r if not isinstance(kol_holders_r, Exception) else []
            if isinstance(info_r, Exception):
                logger.debug(f"GMGN info error for {address[:10]}: {info_r}")
            if isinstance(ath_r, Exception):
                logger.debug(f"GMGN ath error for {address[:10]}: {ath_r}")
            if isinstance(kol_holders_r, Exception):
                logger.debug(f"GMGN kol_holders error for {address[:10]}: {kol_holders_r}")
        except Exception as e:
            logger.warning(f"GMGN gather error for {address[:10]}: {e}")
            info, ath_data = {}, {}

        if not info:
            logger.info(f"[SKIP] {symbol} ({address[:8]}): no data")
            self.state.metrics.record_call("SKIP")
            await self.state.remove_retry(address)
            return

        # Extract nested objects from GMGN response
        price_obj = info.get("price", {}) if isinstance(info.get("price"), dict) else {}
        stat_obj = info.get("stat", {}) if isinstance(info.get("stat"), dict) else {}
        dev_obj = info.get("dev", {}) if isinstance(info.get("dev"), dict) else {}
        wallet_tags = info.get("wallet_tags_stat", {}) or {}
        renowned_wallets = int(wallet_tags.get("renowned_wallets", 0) or 0)
        kol_still_holding = (
            sum(
                1 for w in (kol_holders_list if isinstance(kol_holders_list, list) else [])
                if w.get("end_holding_at") is None
                and float(w.get("amount_percentage", 0) or 0) > 0
            )
            if isinstance(kol_holders_list, list) else 0
        )

        # Calculate market cap from price * total_supply
        current_price = float(price_obj.get("price", 0) or 0)
        total_supply = float(info.get("total_supply", 0) or 0)
        market_cap = current_price * total_supply

        # Phase B: compute drawdown from ATH (0 if no ATH data — fresh tokens)
        ath_price_val = ath_data.get("ath_price", 0.0) if ath_data else 0.0
        ath_fetch_failed = isinstance(ath_r, Exception)
        if ath_price_val > 0 and current_price > 0:
            drawdown = (current_price - ath_price_val) / ath_price_val * 100
        elif ath_fetch_failed:
            drawdown = -999.0  # ATH fetch gagal — conservatively block
        else:
            drawdown = 0.0  # Fresh token, no ATH yet
        logger.info(
            f"[ATH] {symbol} ({address[:8]}): drawdown={drawdown:.1f}%, "
            f"ath=${ath_price_val:.8f}, current=${current_price:.8f}, "
            f"failed={ath_fetch_failed}, candles={ath_data.get('candles_checked', 0) if ath_data else 0}"
        )

        # Holder stats: use GMGN stat aggregates (top_10_holder_rate, fresh_wallet_rate).
        # Previously computed from get_token_holders (per-wallet), but that call
        # was dropped to save GMGN API calls. top_10_holder_rate is the same
        # aggregate the filter actually needs (slightly more conservative than
        # top 15 sum, but within tolerance for filter pass/fail).
        top15_pct = float(stat_obj.get("top_10_holder_rate", 0) or 0) * 100
        new_wallet_pct = float(stat_obj.get("fresh_wallet_rate", 0) or 0) * 100
        top_holder_balance = 0

        # Detect wash trading from GMGN data
        # Check bot_degen_rate or trending wash_trading flag
        bot_degen_rate = float(stat_obj.get("bot_degen_rate", 0) or 0)
        is_wash_trading = bot_degen_rate > 0.5  # >50% bot activity = wash trading

        # Build token data with correct GMGN field mapping
        # Parse timestamps for tiered age filter
        creation_ts = int(info.get("creation_timestamp", 0) or 0)
        open_ts_from_info = int(info.get("open_timestamp", 0) or 0)
        migrated_ts = int(info.get("migrated_timestamp", 0) or 0)
        created_at = datetime.fromtimestamp(creation_ts, timezone.utc) if creation_ts > 0 else None

        token = TokenData(
            address=address,
            name=info.get("name", "") or token_info.get("name", ""),
            symbol=info.get("symbol", "") or token_info.get("symbol", ""),
            market_cap=market_cap,
            volume_1h=float(price_obj.get("volume_1h", 0) or 0),
            volume_5m=float(price_obj.get("volume_5m", 0) or 0),
            liquidity=float(info.get("liquidity", 0) or 0),
            holders_count=int(info.get("holder_count", 0) or 0),
            renowned_wallets=renowned_wallets,
            kol_still_holding=kol_still_holding,
            top15_hold_pct=top15_pct,
            insider_ratio=float(stat_obj.get("top_bundler_trader_percentage", 0) or 0),
            rug_probability=0.0,
            funded_wallet_new_pct=new_wallet_pct,
            top_holder_balance_sol=top_holder_balance,
            fee_collected=float(info.get("total_fee", 0) or 0),
            total_volume=float(price_obj.get("volume_24h", 0) or 0),
            dex_paid=bool(dev_obj.get("dexscr_ad", 0)),
            is_wash_trading=is_wash_trading,
            created_at=created_at,
            creation_timestamp=creation_ts,
            open_timestamp=open_ts_from_info,
            migrated_timestamp=migrated_ts,
            raw_gmgn=info,
            ath_price=ath_price_val,
            ath_timestamp=ath_data.get("ath_timestamp", 0) if ath_data else 0,
            drawdown_from_ath_pct=drawdown,
        )

        # Early permanent skip — before running expensive filters.
        from analysis.filters import is_compound_permanent_failure
        filter_params = await self.state.get_filter_params()
        filters_cfg = filter_params.get("filters", filter_params)

        # fee < 0.1 SOL → dead-letter, gak perlu filter.
        if token.fee_collected < 0.1:
            logger.info(
                f"[RETRY-SKIP] {token.symbol} ({address[:8]}): "
                f"fee={token.fee_collected:.2f} SOL < 0.1 — permanent"
            )
            self.state.metrics.record_call("SKIP_PERMANENT")
            await self.state.remove_retry(address)
            return

        # Compound rule — age > 30m + fee < 1.0 SOL → dead-letter
        if is_compound_permanent_failure(token):
            logger.info(
                f"[RETRY-SKIP] {token.symbol} ({address[:8]}): "
                f"age > 30m fee={token.fee_collected:.2f} SOL < 1.0 — no traction"
            )
            self.state.metrics.record_call("SKIP_PERMANENT")
            await self.state.remove_retry(address)
            return

        # token_age pre-check — hardcoded loose values, NOT from filter config
        # Decoupled from filters.token_age (which has strict 5/5 for hard gate).
        # Pre-check is fast permanent skip for truly ancient tokens. Hard gate +
        # retry handles migration race: token 10min pre-migrate → passes pre-check,
        # fails hard gate, retries; if token migrates during retry delay, next
        # attempt sees post-migrate young age → passes.
        max_pre = 120
        max_post = 45
        now = time.time()
        if token.migrated_timestamp > 0:
            age_min = (now - token.open_timestamp) / 60 if token.open_timestamp > 0 else 999
            max_min = max_post
        else:
            age_min = (now - token.creation_timestamp) / 60 if token.creation_timestamp > 0 else 999
            max_min = max_pre
        if age_min > max_min:
            logger.info(
                f"[RETRY-SKIP] {token.symbol} ({address[:8]}): "
                f"age={age_min:.0f}m > {max_min}m — too old"
            )
            self.state.metrics.record_call("SKIP_PERMANENT")
            await self.state.remove_retry(address)
            return

        # Run filters
        fv = run_all_filters(token, filter_params)

        # Hard gate: ALL filters must pass.
        # Note: top_10_holder_rate / fresh_wallet_rate from stat_obj always
        # provides a value (0 if missing), so no "holder_data_missing" gate
        # is needed — the filters evaluate on actual data.
        all_passed, failures = check_hard_gate(fv)

        # Phase E2-Alert: log every hard-gate outcome (pass or fail) for retro-tuning
        try:
            retry_info_for_log = await self.state.get_retry_info(address)
            retry_count_for_log = retry_info_for_log.get("retries", 0)
            age_min_for_log = (
                (time.time() - max(token.creation_timestamp or 0, token.open_timestamp or 0)) / 60
                if (token.creation_timestamp or token.open_timestamp) else 0.0
            )
            filter_results_dict = {
                name: {
                    "passed": bool(getattr(fv, name, {}).get("passed", False)) if hasattr(fv, name) else False,
                    "value": getattr(fv, name, {}).get("note", "") if hasattr(fv, name) else "",
                }
                for name in [
                    "funded_wallet_age",
                    "insider_concentration", "fee_tier", "rug_probability",
                    "holder_distribution", "min_holders", "min_total_fee",
                    "min_volume_5m", "token_age",
                ]
            }
            # If we forced a fail due to missing holder data, record it
            if "holder_data_missing" in failures:
                filter_results_dict["holder_data_missing"] = {
                    "passed": False,
                    "value": "no holder data and no stat fallback",
                }
            await self.db.save_filter_outcome(
                token_address=address,
                token_name=token.name,
                token_symbol=token.symbol,
                market_cap=token.market_cap,
                holders_count=token.holders_count,
                age_minutes=age_min_for_log,
                filter_results=filter_results_dict,
                passed=all_passed,
                failed_filters=failures,
                was_retried=is_retry,
                retry_count=retry_count_for_log,
                filter_params_version=await self.state.get_filter_version(),
            )
        except Exception as e:
            logger.warning(f"filter_outcome save failed for {address[:8]}: {e}")

        if all_passed:
            logger.info(f"[PASS] {token.symbol} ({address[:8]}): all filters passed")
            self.state.metrics.record_retry(passed=True)
            await self.state.mark_processed(address)
            await self.state.remove_retry(address)
        else:
            retry_info = await self.state.get_retry_info(address)
            retries = retry_info.get("retries", 0)
            # Attempt = 1-indexed attempt number. Original fail is 1, first re-queue
            # is 2, second re-queue is 3, third (last) re-queue is 4. Max attempts =
            # 1 (original) + MAX_RETRIES (3) = 4. Previously this used `retries+1`
            # which double-counted: because add_retry's else-branch sets retries=0
            # for NEW entries, the 2nd attempt also showed `0+1=1` ("RETRY 1/3"),
            # making "1/3" appear twice for the same token. Now we add +1 more
            # when is_retry=True to disambiguate original fail from 1st re-queue.
            attempt = (retries + 1) + (1 if is_retry else 0)

            logger.info(f"[RETRY {attempt}/{MAX_RETRIES + 1}] {token.symbol} ({address[:8]}): failed {len(failures)} filters: {failures}")
            self.state.metrics.record_retry(passed=False)
            await self.state.add_retry(
                address, symbol=token.symbol, name=token.name,
                failed_filters=failures,
            )
            self.state.metrics.record_call("SKIP")
            return

        # Social analysis (only for tokens that pass hard gate)
        await self._social_analysis(token, info)

        # Re-run social_narrative filter — token.social_narrative_score was just
        # updated by _social_analysis, but fv.social_narrative was computed
        # before _social_analysis ran (in run_all_filters above). Without this
        # refresh, the LLM sees score=0 in the feature_vector.
        from analysis.filters import _filter_social_narrative
        fv.social_narrative = _filter_social_narrative(
            token, filter_params.get("social_narrative", {})
        )

        # LLM Decision
        try:
            await self.llm_rate_limiter.acquire(timeout=30.0)
        except asyncio.TimeoutError:
            logger.warning(f"[LLM-RATE] {token.symbol} ({address[:8]}): rate limit timeout (data LLM)")
            return
        fv_dict = fv.to_dict()

        prompt = DECISION_USER.format(
            name=token.name,
            symbol=token.symbol,
            address=address,
            timestamp=datetime.now(timezone.utc).isoformat(),
            feature_vector_json=json.dumps(fv_dict, indent=2),
            sol_price="$150",
            network_status="normal",
            historical_patterns="",
        )

        raw = await self.llm_client.analyze_token(DECISION_SYSTEM, prompt)
        self._update_activity()  # watchdog heartbeat
        logger.debug(f"[LLM-RAW] {token.symbol} ({address[:8]}): {raw}")
        decision = parse_decision(raw)

        # Dual-LLM scoring: social (LLM #1) × 0.5 + data (LLM #2) × 0.5
        social_score = token.social_narrative_score  # 0-100 from LLM #1
        data_score = decision.score  # 0-100 from LLM #2 (weights used as scoring guidance)

        final_score = (social_score * 0.5) + (data_score * 0.5)
        final_score = max(0, min(100, final_score))

        if final_score >= 60:   # was 70, lowered 2026-06-14
            final_verdict = Verdict.APE
        elif final_score >= 50:
            final_verdict = Verdict.WATCH
        else:
            final_verdict = Verdict.SKIP

        # B4 fix: record original LLM verdict before override. The formatter
        # uses this to note "(LLM said: X, overridden by scoring)" so users
        # see WHY a token was upgraded from SKIP→WATCH or downgraded APE→WATCH
        # by the 50:50 rule (their reasoning said something different).
        decision._llm_original_verdict = decision.verdict.value

        decision.verdict = final_verdict
        decision.confidence = final_score / 100.0

        logger.info(
            f"[SCORING] {token.symbol} ({address[:8]}): "
            f"social={social_score:.0f}×0.5={social_score*0.5:.1f} + "
            f"data={data_score}×0.5={data_score*0.5:.1f} = "
            f"FINAL={final_score:.1f}/100 ({final_verdict.value})"
        )
        logger.info(
            f"[LLM] {token.symbol} ({address[:8]}) = {decision.score}/100 ({decision.verdict.value}) "
            f"conf={decision.confidence:.2f} | {decision.reasoning[:120]}"
        )
        if decision.key_factors:
            logger.info(f"[LLM-FACTORS] {token.symbol}: {decision.key_factors}")
        self.state.metrics.record_call(decision.verdict.value)

        # Phase E2-Alert: log every SKIP from LLM #2 for retro-tuning
        if decision.verdict == Verdict.SKIP:
            try:
                age_min_skip = (
                    (time.time() - max(token.creation_timestamp or 0, token.open_timestamp or 0)) / 60
                    if (token.creation_timestamp or token.open_timestamp) else 0.0
                )
                social_score_skip = float(fv.social_narrative.get("score", 0)) if hasattr(fv, "social_narrative") else 0.0
                await self.db.save_skip_decision(
                    token_address=address,
                    token_name=token.name,
                    token_symbol=token.symbol,
                    llm_score=decision.score,
                    llm_reasoning=decision.reasoning,
                    llm_key_factors=decision.key_factors,
                    market_cap=token.market_cap,
                    holders_count=token.holders_count,
                    age_minutes=age_min_skip,
                    top15_pct=token.top15_hold_pct,
                    social_score=social_score_skip,
                    feature_vector=fv_dict,
                )
            except Exception as e:
                logger.warning(f"skip_decision save failed for {address[:8]}: {e}")

            # Alert + CallRecord only for WATCH (APE alert is post-execution).
            # APE verdict → _maybe_execute_live_buy sends ✅ EXECUTED or ❌ BLOCKED
            if decision.verdict in (Verdict.APE, Verdict.WATCH):
                call = CallRecord(
                    token_address=address,
                    token_name=token.name,
                    token_symbol=token.symbol,
                    call_time=datetime.now(timezone.utc),
                    entry_price=current_price,
                    market_cap_at_call=token.market_cap,
                    volume_1h=token.volume_1h,
                    liquidity=token.liquidity,
                    holders_count=token.holders_count,
                    llm_score=decision.score,
                    llm_verdict=decision.verdict.value,
                    llm_reasoning=decision.reasoning,
                    llm_confidence=decision.confidence,
                    llm_key_factors=json.dumps(decision.key_factors),
                    filter_params_version=await self.state.get_filter_version(),
                    feature_vector=json.dumps(fv_dict),
                    status=CallStatus.PENDING,
                )

                call_id = await self.db.save_call(call)
                call.id = call_id
                await self.state.add_active_call(address, call)

                # WATCH verdict: pre-alert (no buy attempt). APE: skip pre-alert,
                # post-execution alert sent by _maybe_execute_live_buy.
                if decision.verdict == Verdict.WATCH:
                    alert_text = format_alert(token, decision, fv_dict,
                                               social_score=social_score)
                    await dispatcher.send_alert(alert_text)
                    self.state.metrics.record_alert()
                    logger.info(
                        f"[ALERT SENT] {token.symbol} ({address[:8]}) (WATCH)"
                    )

                await self.db.save_llm_decision(
                    call_id, decision.score, decision.verdict.value,
                    decision.reasoning, decision.confidence,
                    decision.key_factors, decision.processing_time_ms,
                )

            # Live execution (Step 2): for APE verdict, attempts buy and sends
            # 1 post-execution alert (✅ EXECUTED or ❌ BLOCKED).
            await self._maybe_execute_live_buy(token, address, decision)

    async def get_open_positions_summary(self) -> list[dict]:
        """Public accessor for open positions (used by Telegram /positions command)."""
        return await self.position_manager.get_open_positions_summary()

    async def _maybe_execute_live_buy(self, token: TokenData, address: str,
                                       decision) -> None:
        """Execute live buy for high-conviction APE calls (live mode only).

        Pre-trade guard chain (in order):
          0. _live_paused must be False (set via /live_pause)
          1. paper_mode must be False
          2. verdict must be APE (high conviction; WATCH stays alert-only)
          3. confidence must be >= confidence_auto_execute (default 0.60)
          4. gmgn_cli must be ready
          5. balance must cover position_size + reserve
          6. risk_manager.can_trade() must approve

        After execution (success OR block), sends 1 Telegram alert:
        - ✅ BUY EXECUTED (with position details + strategy)
        - ❌ BUY BLOCKED (with reason + active position context)
        WATCH verdict keeps its pre-alert (no buy attempt).
        """
        from analysis.models import Verdict
        from alerts.formatter import format_buy_executed_alert, format_buy_blocked_alert

        block_reason: Optional[str] = None
        position = None

        # Gate 0 — FIX C1: read from self.state._live_paused (SharedState) to
        # match the /live_pause, /live_resume, /close_all commands which all
        # WRITE to state._live_paused (alerts/bot.py:518,533,566). Previously
        # this checked self._live_paused (TrenchingBot attribute) which is
        # set once at __init__ from settings.live_paused_at_start and never
        # updated — so /live_pause was a no-op in production (the gate never
        # flipped). Now both writer and reader use state._live_paused.
        if self.state._live_paused:
            block_reason = "Live trading paused via /live_pause"
        # Gate 1
        elif self.paper_mode:
            logger.info(
                f"[BUY-DECISION] {token.symbol} ({address[:8]}): "
                f"action=NO_TRADE (paper mode)"
            )
            return  # No alert in paper mode
        # Gate 2 — WATCH/SKIP verdicts have pre-alert in _process_token.
        # No post-exec alert here (WATCH = alert-only, never attempts buy).
        elif decision.verdict != Verdict.APE:
            logger.info(
                f"[BUY-DECISION] {token.symbol} ({address[:8]}): "
                f"verdict={decision.verdict.value}, action=NO_TRADE (not APE)"
            )
            return
        # Gate 4 (was Gate 3 — conf check removed; APE threshold is the gate)
        elif not self.gmgn_cli or not self.gmgn_cli.is_ready():
            block_reason = "GMGN CLI not ready"
        else:
            # All non-risk gates passed — check balance + risk + size
            try:
                open_positions = await self.position_manager.get_open_positions()
                open_count = len(open_positions)
            except Exception as e:
                block_reason = f"Position count failed: {e}"
                open_count = 0

            if block_reason is None:
                try:
                    balance = await self.gmgn_cli.get_sol_balance()
                except Exception as e:
                    block_reason = f"Balance check failed: {e}"
                    balance = 0

            if block_reason is None and balance <= 0:
                block_reason = "Insufficient GMGN balance (0 SOL)"

            size_sol = 0.0
            if block_reason is None:
                size_sol = self.risk_manager.get_position_size(balance)
                min_position = self.trading_config.get("min_position_sol", 0.02)
                if size_sol < min_position:
                    block_reason = (
                        f"Position size {size_sol:.4f} SOL < min {min_position}"
                    )

            if block_reason is None:
                can_trade, risk_reason = self.risk_manager.can_trade(
                    open_position_count=open_count
                )
                if not can_trade:
                    block_reason = f"Risk gate: {risk_reason}"

            # All gates passed — execute
            if block_reason is None:
                logger.info(
                    f"[BUY-EXECUTING] {token.symbol} ({address[:8]}): "
                    f"size={size_sol:.4f} SOL, balance={balance:.4f} SOL"
                )
                try:
                    position = await self.executor.execute_buy(
                        token, size_sol,
                        filter_params_version=await self.state.get_filter_version(),
                    )
                    if position is None:
                        block_reason = "Executor returned None (swap failed)"
                        logger.warning(
                            f"[BUY-FAILED] {token.symbol} ({address[:8]}): "
                            f"executor returned None"
                        )
                    else:
                        logger.info(
                            f"[BUY-EXECUTED] {token.symbol} ({address[:8]}): "
                            f"position_id={position.id}, size={size_sol:.4f} SOL"
                        )
                except Exception as e:
                    block_reason = f"Exception: {e}"
                    logger.error(
                        f"[BUY-ERROR] {token.symbol} ({address[:8]}): {e}",
                        exc_info=True,
                    )

        # Send 1 post-execution alert
        await self._send_post_execution_alert(
            token, address, decision, position, block_reason
        )

    async def _send_post_execution_alert(
        self,
        token: TokenData,
        address: str,
        decision,
        position,
        block_reason: Optional[str],
    ) -> None:
        """Send 1 Telegram alert post-execution: ✅ EXECUTED or ❌ BLOCKED.

        Skipped in paper mode (silent). Skipped on pause-blocked (no spam).
        """
        from alerts.formatter import format_buy_executed_alert, format_buy_blocked_alert

        if self.paper_mode:
            return
        # FIX C1: read from self.state._live_paused (matching the write in
        # alerts/bot.py:518 / 533 / 566) so /live_pause actually blocks
        # the post-exec alert. Previously read self._live_paused which is
        # never updated after init → /live_pause was a no-op.
        if block_reason is not None and self.state._live_paused:
            # User explicitly paused — don't spam
            return

        try:
            if position is not None:
                # ✅ EXECUTED
                msg = format_buy_executed_alert(token, position)
            else:
                # ❌ BLOCKED — fetch active position for context
                active_pos = None
                try:
                    open_positions = await self.position_manager.get_open_positions()
                    live_positions = [
                        p for p in open_positions if not p.get("paper", 1)
                    ]
                    if live_positions:
                        p = live_positions[0]
                        active_pos = {
                            "token_symbol": p.get("token_symbol", "?"),
                            "entry_time": p.get("entry_time", "?"),
                        }
                except Exception:
                    pass
                msg = format_buy_blocked_alert(
                    token, address,
                    verdict=decision.verdict.value,
                    confidence=decision.confidence,
                    block_reason=block_reason or "unknown",
                    active_position=active_pos,
                )
            await dispatcher.send_alert(msg)
            self.state.metrics.record_alert()
            logger.info(
                f"[ALERT SENT] {'✅ EXECUTED' if position else '❌ BLOCKED'} "
                f"{token.symbol} ({address[:8]})"
            )
        except Exception as e:
            logger.warning(f"[ALERT] Failed to send post-execution alert: {e}")

    async def _social_analysis(self, token: TokenData, info: dict):
        """Analyze social media presence for tokens that pass hard gate."""
        try:
            await asyncio.wait_for(
                self._social_analysis_inner(token, info),
                timeout=45,
            )
        except asyncio.TimeoutError:
            logger.warning(f"Social analysis timeout for {token.symbol} (> 45s), using partial data")
        except Exception as e:
            logger.warning(f"Social analysis error for {token.symbol}: {e}")

    async def _social_analysis_inner(self, token: TokenData, info: dict):
        link = info.get("link", {})
        raw_twitter = link.get("twitter_username", "")
        token.website_url = link.get("website", "")
        token.telegram_url = link.get("telegram", "")

        # Parse Twitter input into structured data
        parsed = self.twitter.parse_twitter_input(raw_twitter)
        token.twitter_username = parsed["handle"]

        influencer_mentions = []

        logger.info(f"[SOCIAL] {token.symbol}: twitter parsed={parsed}")

        # 1. Profile + recent tweets (if valid handle)
        if parsed["handle"]:
            try:
                profile = await self.twitter.get_profile(parsed["handle"])
                if profile:
                    token.twitter_followers = profile.get("followers", 0)
                    token.twitter_verified = profile.get("verification", {}).get("verified", False)
                    token.twitter_description = profile.get("description", "")
            except Exception as e:
                logger.warning(f"Twitter profile error for {token.symbol}: {e}")

            try:
                tweets = await self.twitter.get_recent_tweets(parsed["handle"], 3)
                token.recent_tweets = tweets
            except Exception as e:
                logger.warning(f"Twitter tweets error for {token.symbol}: {e}")

        # 2. Specific tweet (if tweet URL)
        if parsed["tweet_id"]:
            try:
                tweet = await self.twitter.get_tweet(parsed["tweet_id"])
                if tweet:
                    if not token.recent_tweets:
                        token.recent_tweets = [tweet]
                    else:
                        token.recent_tweets.insert(0, tweet)
                    author = tweet.get("author", {}).get("screen_name", "").lower()
                    if author in self.influencers:
                        influencer_mentions.append({
                            "handle": author,
                            "name": self.influencers[author]["name"],
                            "weight": self.influencers[author]["weight"],
                            "tweet_text": tweet.get("text", "")[:280],
                            "likes": tweet.get("likes", 0),
                        })
                    logger.info(f"[SOCIAL] {token.symbol}: fetched tweet {parsed['tweet_id']} by @{author}")
            except Exception as e:
                logger.warning(f"Twitter tweet fetch error for {token.symbol}: {e}")

        # 3. Community (if community URL)
        if parsed["community_id"]:
            token.has_community = True
            token.community_id = parsed["community_id"]
            logger.info(f"[SOCIAL] {token.symbol}: has community {parsed['community_id']}")

            if not parsed["handle"]:
                try:
                    creator_handle = await self.twitter.get_community_creator(
                        parsed["community_id"]
                    )
                    if creator_handle:
                        token.community_creator = creator_handle
                        try:
                            profile = await self.twitter.get_profile(creator_handle)
                            if profile:
                                token.twitter_followers = profile.get("followers", 0)
                                token.twitter_verified = profile.get(
                                    "verification", {}
                                ).get("verified", False)
                                token.twitter_description = profile.get(
                                    "description", ""
                                )
                                token.twitter_username = creator_handle
                        except Exception as e:
                            logger.warning(
                                f"Creator profile error for {token.symbol}: {e}"
                            )
                        try:
                            tweets = await self.twitter.get_recent_tweets(
                                creator_handle, 3
                            )
                            token.recent_tweets = tweets
                        except Exception as e:
                            logger.warning(
                                f"Creator tweets error for {token.symbol}: {e}"
                            )
                except Exception as e:
                    logger.warning(
                        f"Community scrape error for {token.symbol}: {e}"
                    )

        # 4. Website scraping
        if token.website_url:
            try:
                token.website_text = await self.scraper.scrape_text(token.website_url)
            except Exception as e:
                logger.warning(f"Website scrape error for {token.symbol}: {e}")

        # 5. Search FxTwitter by contract address
        search_results = []
        try:
            search_results = await self.twitter.search_by_contract(token.address, 10)

            for tweet in search_results:
                author = tweet.get("author", {}).get("screen_name", "").lower()
                if not author:
                    continue
                created_ts = tweet.get("created_timestamp", 0)
                tweet_age_min = (time.time() - created_ts) / 60 if created_ts else 0

                if author in self.influencers:
                    influencer_mentions.append({
                        "handle": author,
                        "name": self.influencers[author]["name"],
                        "weight": self.influencers[author]["weight"],
                        "tweet_text": tweet.get("text", "")[:100],
                        "likes": tweet.get("likes", 0),
                        "tweet_age_min": tweet_age_min,
                    })
                    if author == "elonmusk":
                        token.has_elon_tweet = True
                    elif author == "aeyakovenko":
                        token.has_toly_tweet = True
                else:
                    token.organic_mentions.append({
                        "handle": author,
                        "followers": tweet.get("author", {}).get("followers", 0),
                        "likes": tweet.get("likes", 0),
                        "tweet_text": tweet.get("text", "")[:100],
                        "tweet_age_min": tweet_age_min,
                    })
        except Exception as e:
            logger.warning(f"Twitter search error for {token.symbol}: {e}")

        token.influencer_mentions = influencer_mentions

        # 6. LLM #1: Social analysis
        social_prompt = SOCIAL_ANALYSIS_USER.format(
            token_name=token.name,
            token_symbol=token.symbol,
            twitter_username=token.twitter_username or "none",
            twitter_followers=token.twitter_followers,
            twitter_verified="Yes" if token.twitter_verified else "No",
            twitter_description=token.twitter_description[:200] or "No description",
            twitter_community=f"Yes (community/{token.community_id})" if token.has_community else "No",
            recent_tweets=json.dumps(token.recent_tweets[:3], indent=2) if token.recent_tweets else "No tweets from this account yet",
            website_text=token.website_text[:500] or "No website content",
            search_results=json.dumps(search_results[:5], indent=2) if search_results else "No search results yet",
            influencer_mentions=json.dumps(influencer_mentions, indent=2) if influencer_mentions else "No influencer mentions",
        )

        try:
            await self.llm_rate_limiter.acquire(timeout=30.0)
        except asyncio.TimeoutError:
            logger.warning(f"[LLM-RATE] {token.symbol} ({token.address[:8]}): rate limit timeout (social LLM)")
            return
        social_result = await self.llm_client.analyze_token(SOCIAL_ANALYSIS_SYSTEM, social_prompt)
        self._update_activity()  # watchdog heartbeat

        # 7. Parse LLM #1 response
        llm1_succeeded = social_result is not None
        if llm1_succeeded:
            social_data = social_result
            token.project_type = social_data.get("project_type", "unknown")
            token.social_narrative_score = float(social_data.get("score", 0))
            token.social_narrative_text = social_data.get("summary", "")
            token.catalyst_match = bool(social_data.get("has_catalyst", False))
            token.catalyst_description = social_data.get("catalyst_description", "")
        else:
            logger.warning(
                f"[LLM-1-FAIL] {token.symbol} ({token.address[:8]}): "
                "LLM #1 returned no result; defaulting to neutral 50/unknown"
            )
            token.project_type = "unknown"
            token.social_narrative_score = 50.0
            token.social_narrative_text = ""
            token.catalyst_match = False
            token.catalyst_description = ""

        has_basic_social = bool(token.twitter_username or token.website_url)
        is_scam_signal = (
            token.project_type == "scam" or token.social_narrative_score == 0
        )
        if (
            has_basic_social
            and not is_scam_signal
            and token.social_narrative_score < 15
        ):
            token.social_narrative_score = 15

        # Stage 4: For web3_project tokens, run LLM #3 (substance analysis)
        if token.project_type == "web3_project":
            try:
                from core.web3_analyzer import analyze_web3_substance
                substance = await analyze_web3_substance(token, self.llm_client)
                token.substance_score = substance["substance_score"]
                token.substance_red_flags = substance["red_flags"]
                token.substance_team_visible = substance["team_visible"]
                token.substance_has_github = substance["has_github"]
                token.substance_has_audit = substance["has_audit"]
                token.substance_audit_firm = substance["audit_firm"]

                llm1_score = token.social_narrative_score
                llm3_score = substance["substance_score"]
                token._llm1_social_raw = llm1_score
                token._llm3_substance_raw = llm3_score
                combined_no_mult = (llm1_score * 0.4) + (llm3_score * 0.6)
                token.social_narrative_score = max(0, min(100, combined_no_mult))
                logger.info(
                    f"[LLM-3] {token.symbol} ({token.address[:8]}): "
                    f"substance={llm3_score:.0f}/100, team={substance['team_visible']}, "
                    f"github={substance['has_github']}, audit={substance['has_audit']} "
                    f"({substance['audit_firm']}), red_flags={substance['red_flags']}, "
                    f"reasoning={substance['reasoning'][:120]}"
                )
            except Exception as e:
                logger.error(f"[LLM-3] {token.symbol} error: {e}, using LLM #1 only")
                token._llm1_social_raw = token.social_narrative_score
                token._llm3_substance_raw = None

        # Compute social multiplier
        from llm.social_scoring import compute_social_multiplier
        from core.trench_signals import detect_negative_signals
        negative_penalty = detect_negative_signals(token)
        mult = compute_social_multiplier(token, token.address, negative_penalty=negative_penalty)
        volume_multiplier = mult["multiplier"]
        signals_bonus = mult["signals_bonus"]
        pre_mult_score = token.social_narrative_score

        if (
            token.project_type == "web3_project"
            and getattr(token, "_llm3_substance_raw", None) is not None
        ):
            llm1_boosted = max(0, min(100, token._llm1_social_raw * volume_multiplier))
            combined = (llm1_boosted * 0.4) + (token._llm3_substance_raw * 0.6)
            token.social_narrative_score = max(0, min(100, combined))
        else:
            token.social_narrative_score = min(100, max(0, pre_mult_score * volume_multiplier))

        logger.info(
            f"[SOCIAL] {token.symbol} ({token.address[:8]}): "
            f"llm={pre_mult_score:.0f} × {volume_multiplier:.2f} = {token.social_narrative_score:.0f}/100, "
            f"project={token.project_type}, social_links={has_basic_social}, "
            f"influencers={len(influencer_mentions)}, organic={len(token.organic_mentions)}, "
            f"catalyst={token.catalyst_match}, signals_bonus=+{signals_bonus}, "
            f"penalty=-{negative_penalty}, breakdown={mult['breakdown']}"
        )

    async def _metrics_loop(self):
        while True:
            await asyncio.sleep(300)
            m = self.state.metrics
            retry_count = len(self.state.retry_queue)
            self._update_activity()  # watchdog heartbeat
            logger.info(
                f"STATS | calls:{m.calls_total} ape:{m.calls_ape} watch:{m.calls_watch} "
                f"skip:{m.calls_skip} perm_skip:{m.calls_skip_permanent} | "
                f"retry:{m.retry_attempts} pass:{m.retry_passes} fail:{m.retry_fails} "
                f"({m.retry_success_rate:.0f}%) | "
                f"w/l:{m.wins}/{m.losses} alerts:{m.alerts_sent} "
                f"q:{self.queue.qsize()} rq:{retry_count} err:{m.errors}"
            )
            await self.state.cleanup_retry_queue()

    def _update_activity(self):
        """Update last activity timestamp. Call from any processing path."""
        self._last_activity = time.monotonic()

    async def _watchdog_loop(self):
        """Watchdog: force-restart bot if no activity for WATCHDOG_TIMEOUT seconds.

        Defends against silent event loop deadlock. Uses os._exit(1) so Railway
        can restart the container cleanly.
        """
        WATCHDOG_TIMEOUT = 300  # 5 min
        HEARTBEAT_INTERVAL = 60  # check every 60s
        logger.info(f"Watchdog started (timeout={WATCHDOG_TIMEOUT}s, check={HEARTBEAT_INTERVAL}s)")
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            try:
                idle = time.monotonic() - self._last_activity
                in_flight = await self.state.in_flight_count()
                logger.info(
                    f"[WATCHDOG] idle={idle:.0f}s in_flight={in_flight} "
                    f"q={self.queue.qsize()} rq:{len(self.state.retry_queue)}"
                )
                if idle > WATCHDOG_TIMEOUT and in_flight == 0:
                    logger.critical(
                        f"[WATCHDOG] No activity for {idle:.0f}s and no tokens in flight. "
                        f"Forcing restart via os._exit(1)."
                    )
                    import sys
                    sys.stdout.flush()
                    sys.stderr.flush()
                    os._exit(1)
            except asyncio.CancelledError:
                break
            except Exception as e:
                # Watchdog tick failed — log and continue. No FailureTracker
                # wrapping (would be circular: alert about the alert system).
                logger.error(f"Watchdog tick error: {e}")

    async def _db_stats_loop(self):
        """Phase E2-Alert: log row counts for the 3 new tables every 5 min."""
        while True:
            await asyncio.sleep(300)
            try:
                counts = await self.db.get_table_counts()
                logger.info(
                    f"DB-STATS | filter_outcomes:{counts['filter_outcomes']} "
                    f"(pass={counts['filter_outcomes_passed']}) "
                    f"skip_decisions:{counts['skip_decisions']} "
                    f"loss_analyses:{counts['loss_analyses']}"
                )
            except Exception as e:
                logger.warning(f"DB stats error: {e}")

    async def shutdown(self):
        logger.info("Shutdown initiated...")
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        await self.gmgn.close()
        await self.twitter.close()
        await self.scraper.close()
        await self.jupiter.close()
        await self.price_oracle.close()
        # GMGNCli is a subprocess wrapper, no close() needed
        await self.db.close()
        await dispatcher.close()
        logger.info("Shutdown complete")
        self.shutdown_event.set()


async def main():
    bot = TrenchingBot()
    await bot.start()


if __name__ == "__main__":
    asyncio.run(main())
