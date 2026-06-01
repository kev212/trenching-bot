import asyncio
import logging
from datetime import datetime, timezone
from config import settings
from analysis.models import CallStatus, PriceSnapshot
from sources.dexscreener import fetch_pair_data, extract_pair_info
from llm.mimo_client import MiMoClient
from llm.prompts import LOSS_ANALYSIS_SYSTEM, LOSS_ANALYSIS_USER
from llm.parser import parse_loss_analysis

logger = logging.getLogger(__name__)

CHECK_INTERVAL = settings.price_check_interval
WIN_MULTIPLIER = settings.win_target_multiplier
WIN_TIME_LIMIT = settings.win_time_limit_seconds


async def price_monitor(state, db):
    logger.info("Price monitor started")
    mimo = MiMoClient()

    while True:
        try:
            await asyncio.sleep(CHECK_INTERVAL)
            active_calls = await db.get_active_calls()

            if not active_calls:
                continue

            logger.info(f"Checking {len(active_calls)} active calls...")

            tasks = [_check_single_call(call, state, db, mimo) for call in active_calls]
            await asyncio.gather(*tasks, return_exceptions=True)

        except Exception as e:
            logger.error(f"Price monitor error: {e}")
            await asyncio.sleep(30)


async def _check_single_call(call, state, db, mimo: MiMoClient):
    try:
        pair_data = await fetch_pair_data(call.token_address)
        info = extract_pair_info(pair_data)

        if not info or info.get("price_usd", 0) <= 0:
            return

        current_price = info["price_usd"]
        gain = current_price / call.entry_price if call.entry_price > 0 else 1.0

        snapshot = PriceSnapshot(
            call_id=call.id,
            price=current_price,
            gain=gain,
            snapshot_time=datetime.now(timezone.utc),
        )
        await db.save_price_snapshot(snapshot)

        if gain > call.max_gain:
            await db.update_call_status(call.id, CallStatus.PENDING, max_gain=gain)

        if gain >= WIN_MULTIPLIER:
            logger.info(f"WIN: {call.token_symbol} hit {gain:.2f}x")
            await db.update_call_status(call.id, CallStatus.WIN, max_gain=gain)
            state.metrics.record_outcome("WIN")
            await state.remove_active_call(call.token_address)
            return

        elapsed = (datetime.now(timezone.utc) - call.call_time).total_seconds()
        if elapsed >= WIN_TIME_LIMIT and gain < WIN_MULTIPLIER:
            logger.info(f"LOSS: {call.token_symbol} at {gain:.2f}x after {elapsed:.0f}s")
            await db.update_call_status(call.id, CallStatus.LOSS, max_gain=gain)
            state.metrics.record_outcome("LOSS")
            await state.remove_active_call(call.token_address)

            asyncio.create_task(_analyze_loss(call, gain, elapsed, mimo, db))

    except Exception as e:
        logger.error(f"Error checking {call.token_address}: {e}")


async def _analyze_loss(call, final_gain: float, elapsed: float, mimo: MiMoClient, db):
    try:
        snapshots = []
        call_json = {
            "token": call.token_name,
            "symbol": call.token_symbol,
            "address": call.token_address,
            "entry_price": call.entry_price,
            "score": call.llm_score,
            "verdict": call.llm_verdict,
        }

        price_history = [{"gain": final_gain, "elapsed_seconds": elapsed}]

        prompt = LOSS_ANALYSIS_USER.format(
            call_json=__import__("json").dumps(call_json, indent=2),
            filter_params_json="{}",
            feature_vector_json=call.feature_vector or "{}",
            price_history_json=__import__("json").dumps(price_history, indent=2),
            max_gain=f"{final_gain:.2f}",
            elapsed_minutes=f"{elapsed / 60:.0f}",
        )

        result = await mimo.analyze_token(LOSS_ANALYSIS_SYSTEM, prompt, temperature=0.2)
        analysis = parse_loss_analysis(result)

        logger.info(f"Loss analysis for {call.token_symbol}: {analysis.get('root_cause', 'N/A')}")

        # Phase E2-Alert: persist loss analysis for optimizer + retroactive review
        try:
            import json as _json
            await db.save_loss_analysis(
                call_id=call.id,
                token_address=call.token_address,
                token_symbol=call.token_symbol,
                root_cause=analysis.get("root_cause", ""),
                wrong_filter=analysis.get("wrong_filter", ""),
                suggestion=analysis.get("suggestion", ""),
                pattern=analysis.get("pattern", ""),
                confidence=analysis.get("confidence", 0.0),
                llm_raw=_json.dumps(result) if result else "",
                max_gain=final_gain,
                elapsed_seconds=elapsed,
            )
        except Exception as e:
            logger.error(f"loss_analysis save failed for {call.token_symbol}: {e}")

    except Exception as e:
        logger.error(f"Loss analysis error for {call.token_symbol}: {e}")
