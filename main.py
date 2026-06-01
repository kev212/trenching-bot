import asyncio
import json
import logging
import os
import signal
import time
from datetime import datetime, timezone

logger = logging.getLogger("main")

from config import settings
from storage.database import Database
from storage.cache import SharedState
from sources.gmgn import GMGNClient
from sources.twitter import TwitterClient
from sources.web_scraper import WebScraper
from analysis.models import TokenData, CallRecord, CallStatus, Verdict
from analysis.filters import run_all_filters, check_hard_gate
from llm.mimo_client import MiMoClient
from llm.prompts import DECISION_SYSTEM, DECISION_USER, SOCIAL_ANALYSIS_SYSTEM, SOCIAL_ANALYSIS_USER
from llm.parser import parse_decision
from tracking.price_monitor import price_monitor
from tracking.hourly_recap import hourly_recap
from learning.daily_optimizer import daily_optimizer
from learning.revert_monitor import revert_monitor
from alerts.formatter import format_alert
from alerts.dispatcher import dispatcher
from alerts.bot import bot_handler
from utils.logger import setup_logger
from utils.helpers import RateLimiter

logger = setup_logger("main")

MIN_FILTERS_FOR_LLM = 6


def _safe_float(val, default=0.0) -> float:
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _calculate_rug_score(security: dict) -> float:
    """Use GMGN's rug_ratio (ML-computed) with manual fallback."""
    if not security:
        return 0.0

    # Primary: GMGN's own rug_ratio
    rug_ratio = security.get("rug_ratio")
    if rug_ratio is not None and rug_ratio != "":
        try:
            return min(max(float(rug_ratio), 0.0), 1.0)
        except (TypeError, ValueError):
            pass

    # Fallback: manual calc
    score = 0.0
    if security.get("honeypot") in (1, True, "1"):
        score += 0.4
    if security.get("blacklist") in (1, True, "1"):
        score += 0.3
    if security.get("renounced_mint") in (False, 0, "0"):
        score += 0.1
    if security.get("renounced_freeze_account") in (False, 0, "0"):
        score += 0.1

    buy_tax = _safe_float(security.get("buy_tax"))
    sell_tax = _safe_float(security.get("sell_tax"))
    if buy_tax > 0.1 or sell_tax > 0.1:
        score += 0.2

    burn = _safe_float(security.get("burn_ratio"), default=1.0)
    if burn < 0.5:
        score += 0.1

    if security.get("is_wash_trading") in (True, "true", 1):
        score += 0.2
    lock = security.get("lock_summary", {}) or {}
    if not lock.get("is_locked"):
        score += 0.15
    if _safe_float(security.get("top_10_holder_rate")) > 0.5:
        score += 0.2
    if _safe_float(security.get("rat_trader_amount_rate")) > 0.3:
        score += 0.2
    if _safe_float(security.get("bundler_trader_amount_rate")) > 0.5:
        score += 0.15
    if int(security.get("sniper_count", 0) or 0) > 5:
        score += 0.1

    return min(score, 1.0)


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
        self.gmgn = GMGNClient(settings.gmgn_api_key, settings.http_proxy)
        logger.warning(f"GMGN init: proxy=[{self.gmgn.proxy[:50] if self.gmgn.proxy else 'NONE'}]")
        self.twitter = TwitterClient()
        self.scraper = WebScraper()
        self.influencers = _load_influencers()
        logger.warning(f"Loaded {len(self.influencers)} influencers")
        self.mimo = MiMoClient()
        self.rate_limiter = RateLimiter(15, 60)  # GMGN: 15 req/min
        self.llm_rate_limiter = RateLimiter(10, 60)  # LLM: 10 req/min
        self.tasks = {}
        self.workers = []
        self.seen_trenches = set()
        self.shutdown_event = asyncio.Event()

    async def start(self):
        logger.info("=" * 50)
        logger.info("TRENCHING BOT v3 - Starting...")
        logger.info("=" * 50)

        await self.db.init()
        await self.state.load_filter_params()

        # Test GMGN connection
        logger.info("Testing GMGN API...")
        test = await self.gmgn.get_trending(limit=1)
        if test:
            logger.info(f"GMGN API OK - got {len(test)} tokens")
        else:
            logger.warning("GMGN API returned no data, will retry")

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda: asyncio.create_task(self.shutdown()))
            except NotImplementedError:
                pass

        self.tasks = {
            "gmgn_poller": asyncio.create_task(self._gmgn_poller()),
            "trenches_poller": asyncio.create_task(self._trenches_poller()),
            "retry_scheduler": asyncio.create_task(self._retry_scheduler()),
            "worker_0": asyncio.create_task(self._token_worker(0)),
            "price_monitor": asyncio.create_task(self._run_forever("price_monitor", price_monitor)),
            "hourly_recap": asyncio.create_task(self._run_forever("hourly_recap", hourly_recap)),
            "daily_optimizer": asyncio.create_task(self._run_forever("daily_optimizer", daily_optimizer)),
            "revert_monitor": asyncio.create_task(self._run_forever("revert_monitor", revert_monitor)),
            "bot_handler": asyncio.create_task(self._run_forever("bot_handler", bot_handler)),
            "metrics": asyncio.create_task(self._metrics_loop()),
            "db_stats": asyncio.create_task(self._db_stats_loop()),
        }

        logger.info(f"Launched {len(self.tasks)} tasks")
        logger.info("Bot running! Press Ctrl+C to stop")

        try:
            await self.shutdown_event.wait()
        except asyncio.CancelledError:
            pass

    async def _run_forever(self, name, coro_func, *args):
        retries = 0
        while retries < 5:
            try:
                await coro_func(self.state, self.db, *args)
            except asyncio.CancelledError:
                break
            except Exception as e:
                retries += 1
                logger.error(f"{name} error ({retries}/5): {e}")
                await asyncio.sleep(min(2 ** retries, 60))

    async def _gmgn_poller(self):
        logger.info("GMGN Poller starting...")
        seen = set()
        poll_count = 0
        ban_count = 0
        base_delay = 60

        while True:
            try:
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

                    if addr in self.state.retry_queue:
                        retry_info = self.state.retry_queue[addr]
                        if retry_info["retries"] >= 3:
                            continue

                    seen.add(addr)
                    await self.queue.put(token)
                    new_count += 1

                poll_count += 1
                if new_count > 0:
                    logger.info(f"GMGN #{poll_count}: +{new_count} tokens (queue:{self.queue.qsize()})")

                if len(seen) > 10000:
                    seen.clear()

                await asyncio.sleep(base_delay)

            except Exception as e:
                err_str = str(e).upper()
                if "429" in err_str or "RATE_LIMIT" in err_str or "BANNED" in err_str:
                    ban_count += 1
                    wait_time = min(60 * (2 ** ban_count), 600)
                    logger.warning(f"GMGN rate limited (ban #{ban_count}), waiting {wait_time}s...")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"GMGN poller error: {e}")
                    await asyncio.sleep(base_delay)

    async def _trenches_poller(self):
        logger.info("Trenches Poller starting...")
        poll_count = 0
        ban_count = 0
        base_delay = 30

        while True:
            try:
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
                    if addr in self.state.retry_queue:
                        retry_info = self.state.retry_queue[addr]
                        if retry_info["retries"] >= 3:
                            continue
                    self.seen_trenches.add(addr)
                    await self.queue.put(token)
                    new_count += 1

                poll_count += 1
                if new_count > 0:
                    logger.info(f"Trenches #{poll_count}: +{new_count} tokens (queue:{self.queue.qsize()})")

                if len(self.seen_trenches) > 10000:
                    self.seen_trenches.clear()

                await asyncio.sleep(base_delay)

            except Exception as e:
                err_str = str(e).upper()
                if "429" in err_str or "RATE_LIMIT" in err_str or "BANNED" in err_str:
                    ban_count += 1
                    wait_time = min(60 * (2 ** ban_count), 600)
                    logger.warning(f"Trenches rate limited (ban #{ban_count}), waiting {wait_time}s...")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"Trenches poller error: {e}")
                    await asyncio.sleep(base_delay)

    async def _retry_scheduler(self):
        """Periodically re-queue tokens whose retry delay has expired.

        Without this, tokens marked for retry sit forever — pollers skip them
        (in their local `seen` set), and nothing wakes them up.
        """
        logger.info("Retry scheduler started")
        scan_interval = 30  # seconds

        while True:
            try:
                now = time.time()
                requeued = 0
                expired = []

                async with self.state._lock:
                    for addr, info in list(self.state.retry_queue.items()):
                        if info["retries"] >= 3:
                            continue
                        if await self.state.is_duplicate(addr):
                            expired.append(addr)
                            continue
                        if now - info["timestamp"] < 300:
                            continue
                        # Re-queue this token; do NOT update timestamp here —
                        # `should_retry` in worker checks `now - timestamp >= 300`,
                        # and `add_retry` will reset timestamp when it fails again.
                        await self.queue.put({"address": addr, "_retry": True, "retries": info["retries"]})
                        requeued += 1

                    for addr in expired:
                        self.state.retry_queue.pop(addr, None)

                if requeued > 0 or expired:
                    logger.info(
                        f"[RETRY-SCHED] requeued={requeued} expired={len(expired)} queue={self.queue.qsize()}"
                    )

                await self.state.cleanup_retry_queue()
                await asyncio.sleep(scan_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Retry scheduler error: {e}")
                await asyncio.sleep(scan_interval)

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

                if is_retry:
                    if not await self.state.should_retry(addr):
                        # Lost race — put back at end of queue, skip for now
                        await self.queue.put(token_info)
                        await asyncio.sleep(0.1)
                        continue

                processed += 1
                retry_count = (await self.state.get_retry_info(addr)).get("retries", 0)
                logger.info(
                    f"[W{worker_id}] #{processed} {symbol} ({addr[:8]}...) "
                    f"retry:{is_retry} ({retry_count}/3) q:{self.queue.qsize()}"
                )

                await self._process_token(addr, token_info)
                await asyncio.sleep(1)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker error: {e}")
                self.state.metrics.record_error()
                await asyncio.sleep(1)

    async def _process_token(self, address: str, token_info: dict):
        is_retry = token_info.get("_retry", False)
        symbol = token_info.get("symbol", "?") or "?"

        if not is_retry:
            # Quick pre-filter from trending data (no API call needed)
            mc = token_info.get("market_cap", 0) or 0
            holder_count = token_info.get("holder_count", 0) or 0
            is_wash = token_info.get("is_wash_trading", False)

            if mc <= 0:
                logger.info(f"[SKIP] {symbol} ({address[:8]}): mc=0")
                self.state.metrics.record_call("SKIP")
                return
            if mc < 7000:
                logger.info(f"[SKIP] {symbol} ({address[:8]}): mc=${mc:,.0f} < $7K")
                self.state.metrics.record_call("SKIP")
                return
            if mc > 200000:
                logger.info(f"[SKIP] {symbol} ({address[:8]}): mc=${mc:,.0f} > $200K")
                self.state.metrics.record_call("SKIP")
                return
            if holder_count < 100:
                logger.info(f"[SKIP] {symbol} ({address[:8]}): holders={holder_count} < 100")
                self.state.metrics.record_call("SKIP")
                return
            if is_wash:
                logger.info(f"[SKIP] {symbol} ({address[:8]}): wash_trading=True")
                self.state.metrics.record_call("SKIP")
                return

        # Acquire rate limit slot + fetch data sequentially (no burst)
        await self.rate_limiter.acquire()

        try:
            info = await self.gmgn.get_token_info(address)
        except Exception as e:
            logger.warning(f"GMGN info error for {address[:10]}: {e}")
            info = {}

        await asyncio.sleep(2)
        await self.rate_limiter.acquire()

        try:
            security = await self.gmgn.get_token_security(address)
        except Exception as e:
            logger.warning(f"GMGN security error for {address[:10]}: {e}")
            security = {}

        if not security:
            logger.warning(f"[SECURITY-EMPTY] {symbol} ({address[:8]}): rug_score will be 0")

        await asyncio.sleep(2)
        await self.rate_limiter.acquire()

        try:
            holders = await self.gmgn.get_token_holders(address)
        except Exception as e:
            logger.warning(f"GMGN holders error for {address[:10]}: {e}")
            holders = {}

        if not info:
            logger.info(f"[SKIP] {symbol} ({address[:8]}): no data")
            self.state.metrics.record_call("SKIP")
            return

        # Extract nested objects from GMGN response
        price_obj = info.get("price", {}) if isinstance(info.get("price"), dict) else {}
        stat_obj = info.get("stat", {}) if isinstance(info.get("stat"), dict) else {}
        dev_obj = info.get("dev", {}) if isinstance(info.get("dev"), dict) else {}
        holders_list = holders.get("list", []) if isinstance(holders.get("list"), list) else []

        # Calculate market cap from price * total_supply
        current_price = float(price_obj.get("price", 0) or 0)
        total_supply = float(info.get("total_supply", 0) or 0)
        market_cap = current_price * total_supply

        # Calculate holder stats from holders list
        if holders_list:
            top10 = holders_list[:10]
            # amount_percentage is decimal (0.1189 = 11.89%), convert to percentage
            top10_pct = sum(float(h.get("amount_percentage", 0)) for h in top10) * 100
            new_wallet_count = sum(1 for h in holders_list if h.get("is_new", False))
            new_wallet_pct = (new_wallet_count / len(holders_list) * 100) if holders_list else 0
            # native_balance is in lamports, convert to SOL (1 SOL = 1e9 lamports)
            top_holder_balance = float(holders_list[0].get("native_balance", 0)) / 1e9
        else:
            # stat rates are decimals (0.1847 = 18.47%), convert to percentage
            top10_pct = float(stat_obj.get("top_10_holder_rate", 0) or 0) * 100
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
            liquidity=float(info.get("liquidity", 0) or 0),
            holders_count=int(info.get("holder_count", 0) or 0),
            top10_hold_pct=top10_pct,
            insider_ratio=float(stat_obj.get("top_bundler_trader_percentage", 0) or 0),
            rug_probability=_calculate_rug_score(security),
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
        )

        # Run filters
        filter_params = await self.state.get_filter_params()
        fv = run_all_filters(token, filter_params)

        # Hard gate: ALL filters must pass
        all_passed, failures = check_hard_gate(fv)

        # Phase E2-Alert: log every hard-gate outcome (pass or fail) for retro-tuning
        try:
            retry_info_for_log = await self.state.get_retry_info(address)
            retry_count_for_log = retry_info_for_log.get("retries", 0)
            age_min_for_log = (
                (time.time() * 1000 - max(token.creation_timestamp or 0, token.open_timestamp or 0)) / 60000
                if (token.creation_timestamp or token.open_timestamp) else 0.0
            )
            filter_results_dict = {
                name: {
                    "passed": bool(getattr(fv, name, {}).get("passed", False)) if hasattr(fv, name) else False,
                    "value": getattr(fv, name, {}).get("note", "") if hasattr(fv, name) else "",
                }
                for name in [
                    "funded_wallet_age", "min_market_cap", "max_market_cap",
                    "insider_concentration", "fee_tier", "rug_probability",
                    "holder_distribution", "token_age", "min_holders", "min_total_fee",
                ]
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
            logger.debug(f"filter_outcome save failed for {address[:8]}: {e}")

        if all_passed:
            logger.info(f"[PASS] {token.symbol} ({address[:8]}): all filters passed")
            await self.state.mark_processed(address)
            await self.state.remove_retry(address)
        else:
            retry_info = await self.state.get_retry_info(address)
            retries = retry_info.get("retries", 0)
            logger.info(f"[RETRY {retries+1}/3] {token.symbol} ({address[:8]}): failed {len(failures)} filters: {failures}")
            await self.state.add_retry(address)
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
        await self.llm_rate_limiter.acquire()
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

        raw = await self.mimo.analyze_token(DECISION_SYSTEM, prompt)
        logger.debug(f"[LLM-RAW] {token.symbol} ({address[:8]}): {raw}")
        decision = parse_decision(raw)

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
                    (time.time() * 1000 - max(token.creation_timestamp or 0, token.open_timestamp or 0)) / 60000
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
                    top10_pct=token.top10_hold_pct,
                    social_score=social_score_skip,
                    feature_vector=fv_dict,
                )
            except Exception as e:
                logger.debug(f"skip_decision save failed for {address[:8]}: {e}")

        # Alert if APE or WATCH
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

            alert_text = format_alert(token, decision, fv_dict)
            await dispatcher.send_alert(alert_text)
            self.state.metrics.record_alert()

            logger.info(f"[ALERT SENT] {token.symbol} ({address[:8]}) ({decision.verdict.value})")

            await self.db.save_llm_decision(
                call_id, decision.score, decision.verdict.value,
                decision.reasoning, decision.confidence,
                decision.key_factors, decision.processing_time_ms,
            )

    async def _social_analysis(self, token: TokenData, info: dict):
        """Analyze social media presence for tokens that pass hard gate."""
        try:
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
                        # Add to recent_tweets if not already there
                        if not token.recent_tweets:
                            token.recent_tweets = [tweet]
                        else:
                            token.recent_tweets.insert(0, tweet)
                        # Check if tweet author is influencer
                        author = tweet.get("author", {}).get("screen_name", "").lower()
                        if author in self.influencers:
                            influencer_mentions.append({
                                "handle": author,
                                "name": self.influencers[author]["name"],
                                "weight": self.influencers[author]["weight"],
                                "tweet_text": tweet.get("text", "")[:100],
                                "likes": tweet.get("likes", 0),
                            })
                        logger.info(f"[SOCIAL] {token.symbol}: fetched tweet {parsed['tweet_id']} by @{author}")
                except Exception as e:
                    logger.warning(f"Twitter tweet fetch error for {token.symbol}: {e}")

            # 3. Community (if community URL)
            if parsed["community_id"]:
                token.has_community = True
                logger.info(f"[SOCIAL] {token.symbol}: has community {parsed['community_id']}")

            # 4. Website scraping
            if token.website_url:
                try:
                    token.website_text = await self.scraper.scrape_text(token.website_url)
                except Exception as e:
                    logger.warning(f"Website scrape error for {token.symbol}: {e}")

            # 3. Search FxTwitter by contract address
            try:
                search_results = await self.twitter.search_by_contract(token.address, 10)

                # 4. Influencer + organic mention detection (direct, no LLM)
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

            # 5. LLM #1: Social analysis
            # Calculate age description
            if token.creation_timestamp > 0:
                age_min = (datetime.now(timezone.utc).timestamp() - token.creation_timestamp) / 60
                if age_min < 60:
                    age_description = f"{age_min:.0f} minutes ago"
                else:
                    age_description = f"{age_min/60:.1f} hours ago"
            else:
                age_description = "unknown"

            social_prompt = SOCIAL_ANALYSIS_USER.format(
                token_name=token.name,
                token_symbol=token.symbol,
                market_cap=token.market_cap,
                age_description=age_description,
                holders_count=token.holders_count,
                twitter_username=token.twitter_username or "none",
                twitter_followers=token.twitter_followers,
                twitter_verified="Yes" if token.twitter_verified else "No",
                twitter_description=token.twitter_description[:200] or "No description",
                recent_tweets=json.dumps(token.recent_tweets[:3], indent=2) if token.recent_tweets else "No tweets from this account yet",
                website_text=token.website_text[:500] or "No website content",
                search_results=json.dumps(search_results[:5], indent=2) if search_results else "No search results yet",
                influencer_mentions=json.dumps(influencer_mentions, indent=2) if influencer_mentions else "No influencer mentions",
            )

            await self.llm_rate_limiter.acquire()
            social_result = await self.mimo.analyze_token(SOCIAL_ANALYSIS_SYSTEM, social_prompt)

            # 6. Parse LLM #1 response
            if social_result:
                try:
                    social_data = json.loads(social_result) if isinstance(social_result, str) else social_result
                    token.project_type = social_data.get("project_type", "unknown")
                    token.social_narrative_score = float(social_data.get("score", 0))
                    token.social_narrative_text = social_data.get("summary", "")
                    token.catalyst_match = bool(social_data.get("has_catalyst", False))
                    token.catalyst_description = social_data.get("catalyst_description", "")
                except Exception as e:
                    logger.warning(f"Social analysis parse error for {token.symbol}: {e}")
                    token.project_type = "unknown"
                    token.social_narrative_score = 0
                    token.social_narrative_text = ""
                    token.catalyst_match = False
                    token.catalyst_description = ""

            # Score floor: tokens with basic social links get minimum 15pts
            has_basic_social = bool(token.twitter_username or token.website_url or token.telegram_url)
            if has_basic_social and token.social_narrative_score < 15:
                token.social_narrative_score = 15

            # Compute social signals bonus (replaces simple max(weight))
            from llm.social_scoring import calculate_social_signals_bonus
            signals = calculate_social_signals_bonus(token)
            signals_bonus = signals["total_bonus"]
            token.social_narrative_score = min(100, token.social_narrative_score + signals_bonus)

            logger.info(
                f"[SOCIAL] {token.symbol} ({token.address[:8]}): score={token.social_narrative_score:.0f}/100, "
                f"project={token.project_type}, social_links={has_basic_social}, "
                f"influencers={len(influencer_mentions)}, organic={len(token.organic_mentions)}, "
                f"catalyst={token.catalyst_match}, signals_bonus=+{signals_bonus}"
            )

        except Exception as e:
            logger.error(f"Social analysis error for {token.symbol}: {e}")
            token.project_type = "unknown"
            token.social_narrative_score = 0
            token.social_narrative_text = ""

    async def _metrics_loop(self):
        while True:
            await asyncio.sleep(300)
            m = self.state.metrics
            retry_count = len(self.state.retry_queue)
            logger.info(
                f"STATS | calls:{m.calls_total} ape:{m.calls_ape} watch:{m.calls_watch} skip:{m.calls_skip} | "
                f"w/l:{m.wins}/{m.losses} alerts:{m.alerts_sent} q:{self.queue.qsize()} retry:{retry_count} err:{m.errors}"
            )
            await self.state.cleanup_retry_queue()

    async def _db_stats_loop(self):
        """Phase E2-Alert: log row counts for the 3 new tables every 5 min."""
        while True:
            await asyncio.sleep(300)
            try:
                cur = await self.db.db.execute(
                    "SELECT (SELECT COUNT(*) FROM filter_outcomes) as fo, "
                    "(SELECT COUNT(*) FROM skip_decisions) as sd, "
                    "(SELECT COUNT(*) FROM loss_analyses) as la"
                )
                row = await cur.fetchone()
                fo_passed = await (await self.db.db.execute(
                    "SELECT COUNT(*) as c FROM filter_outcomes WHERE passed = 1"
                )).fetchone()
                logger.info(
                    f"DB-STATS | filter_outcomes:{row['fo']} (pass={fo_passed['c']}) "
                    f"skip_decisions:{row['sd']} loss_analyses:{row['la']}"
                )
            except Exception as e:
                logger.warning(f"DB stats error: {e}")

    async def shutdown(self):
        logger.info("Shutdown initiated...")
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        await self.db.close()
        await dispatcher.close()
        logger.info("Shutdown complete")
        self.shutdown_event.set()


async def main():
    bot = TrenchingBot()
    await bot.start()


if __name__ == "__main__":
    asyncio.run(main())
