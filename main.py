import asyncio
import json
import logging
import os
import signal
from datetime import datetime

logger = logging.getLogger("main")

# DEBUG: print all proxy env vars
for k, v in sorted(os.environ.items()):
    if "proxy" in k.lower() or "PROXY" in k:
        logger.warning(f"ENV: {k}={v[:60]}")

from config import settings
from storage.database import Database
from storage.cache import SharedState
from sources.gmgn import GMGNClient
from analysis.models import TokenData, CallRecord, CallStatus, Verdict
from analysis.filters import run_all_filters, check_hard_gate
from llm.mimo_client import MiMoClient
from llm.prompts import DECISION_SYSTEM, DECISION_USER
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


def _calculate_rug_score(security: dict) -> float:
    """Calculate rug probability from GMGN security fields."""
    if not security:
        return 0.0

    score = 0.0

    if security.get("blacklist", 0):
        score += 0.3
    if security.get("honeypot", 0):
        score += 0.5
    if not security.get("renounced_freeze_account", True):
        score += 0.1
    if not security.get("renounced_mint", True):
        score += 0.1
    if float(security.get("buy_tax", 0) or 0) > 0.1:
        score += 0.2
    if float(security.get("sell_tax", 0) or 0) > 0.1:
        score += 0.2
    if float(security.get("burn_ratio", 1) or 1) < 0.5:
        score += 0.1

    return min(score, 1.0)


class TrenchingBot:
    def __init__(self):
        self.db = Database(settings.db_path)
        self.state = SharedState()
        self.queue = asyncio.Queue(maxsize=settings.max_queue_size)
        self.state.queue = self.queue
        self.gmgn = GMGNClient(settings.gmgn_api_key, settings.http_proxy)
        logger.warning(f"GMGN init: proxy=[{self.gmgn.proxy[:50] if self.gmgn.proxy else 'NONE'}]")
        self.mimo = MiMoClient()
        self.rate_limiter = RateLimiter(15, 60)  # 15 req/min (3 calls per token = ~5 tokens/min)
        self.tasks = {}
        self.workers = []
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
            "worker_0": asyncio.create_task(self._token_worker(0)),
            "price_monitor": asyncio.create_task(self._run_forever("price_monitor", price_monitor)),
            "hourly_recap": asyncio.create_task(self._run_forever("hourly_recap", hourly_recap)),
            "daily_optimizer": asyncio.create_task(self._run_forever("daily_optimizer", daily_optimizer)),
            "revert_monitor": asyncio.create_task(self._run_forever("revert_monitor", revert_monitor)),
            "bot_handler": asyncio.create_task(self._run_forever("bot_handler", bot_handler)),
            "metrics": asyncio.create_task(self._metrics_loop()),
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
        base_delay = 60  # 60 detik base delay

        while True:
            try:
                tokens = await self.gmgn.get_trending(limit=20)
                ban_count = 0  # Reset ban counter kalau sukses
                new_count = 0
                for token in tokens:
                    addr = token.get("address") or token.get("token_address")
                    if addr and addr not in seen:
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
                    wait_time = min(60 * (2 ** ban_count), 600)  # 60s, 120s, 240s, max 10min
                    logger.warning(f"GMGN rate limited (ban #{ban_count}), waiting {wait_time}s...")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"GMGN poller error: {e}")
                    await asyncio.sleep(base_delay)

    async def _token_worker(self, worker_id: int):
        logger.info(f"Worker {worker_id} started")
        processed = 0

        while True:
            try:
                token_info = await self.queue.get()
                addr = token_info.get("address") or token_info.get("token_address", "")
                symbol = token_info.get("symbol", "?") or "?"

                if await self.state.is_duplicate(addr):
                    continue

                processed += 1
                logger.info(f"[W{worker_id}] #{processed} {symbol} ({addr[:8]}...) q:{self.queue.qsize()}")

                await self._process_token(addr, token_info)
                await asyncio.sleep(1)  # Jeda antar token

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker error: {e}")
                self.state.metrics.record_error()
                await asyncio.sleep(1)

    async def _process_token(self, address: str, token_info: dict):
        # Quick pre-filter from trending data (no API call needed)
        mc = token_info.get("market_cap", 0) or 0
        holder_count = token_info.get("holder_count", 0) or 0
        is_wash = token_info.get("is_wash_trading", False)
        bot_rate = float(token_info.get("bot_degen_rate", 0) or 0)
        open_ts = token_info.get("open_timestamp", 0) or 0

        if mc <= 0:
            self.state.metrics.record_call("SKIP")
            return
        if mc < 7000:
            self.state.metrics.record_call("SKIP")
            return
        if mc > 200000:
            self.state.metrics.record_call("SKIP")
            return
        if holder_count < 100:
            self.state.metrics.record_call("SKIP")
            return
        if is_wash or bot_rate > 0.5:
            self.state.metrics.record_call("SKIP")
            return
        if open_ts > 0:
            age_min = (datetime.utcnow().timestamp() - open_ts) / 60
            if age_min > 30:
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

        await asyncio.sleep(2)
        await self.rate_limiter.acquire()

        try:
            holders = await self.gmgn.get_token_holders(address)
        except Exception as e:
            logger.warning(f"GMGN holders error for {address[:10]}: {e}")
            holders = {}

        if not info:
            logger.info(f"[SKIP] {token_info.get('symbol','?')}: no data")
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
        # Parse creation timestamp for token age filter
        creation_ts = int(info.get("creation_timestamp", 0) or 0)
        created_at = datetime.utcfromtimestamp(creation_ts) if creation_ts > 0 else None

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
            raw_gmgn=info,
        )

        # Run filters
        filter_params = await self.state.get_filter_params()
        fv = run_all_filters(token, filter_params)

        # Hard gate: ALL filters must pass
        all_passed, failures = check_hard_gate(fv)

        if all_passed:
            logger.info(f"[PASS] {token.symbol}: all filters passed")
        else:
            logger.info(f"[SKIP] {token.symbol}: failed {len(failures)} filters: {failures}")
            self.state.metrics.record_call("SKIP")
            return

        # LLM Decision
        await self.rate_limiter.acquire()
        fv_dict = fv.to_dict()

        prompt = DECISION_USER.format(
            name=token.name,
            symbol=token.symbol,
            address=address,
            timestamp=datetime.utcnow().isoformat(),
            feature_vector_json=json.dumps(fv_dict, indent=2),
            sol_price="$150",
            network_status="normal",
            historical_patterns="",
        )

        raw = await self.mimo.analyze_token(DECISION_SYSTEM, prompt)
        decision = parse_decision(raw)

        logger.info(f"[LLM] {token.symbol} = {decision.score}/100 ({decision.verdict.value})")
        self.state.metrics.record_call(decision.verdict.value)

        # Alert if APE or WATCH
        if decision.verdict in (Verdict.APE, Verdict.WATCH):
            call = CallRecord(
                token_address=address,
                token_name=token.name,
                token_symbol=token.symbol,
                call_time=datetime.utcnow(),
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

            logger.info(f"[ALERT SENT] {token.symbol} ({decision.verdict.value})")

            await self.db.save_llm_decision(
                call_id, decision.score, decision.verdict.value,
                decision.reasoning, decision.confidence,
                decision.key_factors, decision.processing_time_ms,
            )

    async def _metrics_loop(self):
        while True:
            await asyncio.sleep(300)
            m = self.state.metrics
            logger.info(
                f"STATS | calls:{m.calls_total} ape:{m.calls_ape} watch:{m.calls_watch} skip:{m.calls_skip} | "
                f"w/l:{m.wins}/{m.losses} alerts:{m.alerts_sent} q:{self.queue.qsize()} err:{m.errors}"
            )

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
