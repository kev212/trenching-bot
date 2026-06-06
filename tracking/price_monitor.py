"""Price snapshot tracker for ALERT calls (legacy `calls` table, USD-denominated).

A4 fix: This module is now a READ-ONLY snapshot updater. It does NOT mark
WIN/LOSS — that's the responsibility of the position-level monitors
(`position_monitor` for paper/live positions, which use SOL-denominated SL/TP
rules from trading.json). This module only:
  1. Fetches current price for active calls (calls.status = 'PENDING')
  2. Saves price_snapshot records for retro-analysis
  3. Updates calls.max_gain to track the highest gain seen

Legacy note: This module predates the position-trading system. The actual
buy/sell lifecycle (and the WIN/LOSS resolution that matters for PnL) lives
in `tracking/position_monitor`.
"""
import asyncio
import logging
from datetime import datetime, timezone
from config import settings
from analysis.models import PriceSnapshot
from sources.dexscreener import fetch_pair_data, extract_pair_info
from llm.pioneer_client import PioneerLLMClient
from llm.prompts import LOSS_ANALYSIS_SYSTEM, LOSS_ANALYSIS_USER
from llm.parser import parse_loss_analysis

logger = logging.getLogger(__name__)

CHECK_INTERVAL = settings.price_check_interval


async def price_monitor(state, db):
    """A4: snapshot-only. Resolution is owned by position_monitor."""
    logger.info("Price monitor started (snapshot-only — position_monitor owns exits)")
    llm = PioneerLLMClient()  # noqa: F841 — reserved for future loss analysis

    while True:
        try:
            await asyncio.sleep(CHECK_INTERVAL)
            active_calls = await db.get_active_calls()

            if not active_calls:
                continue

            logger.info(f"Updating snapshots for {len(active_calls)} active calls...")

            tasks = [_check_single_call(call, db) for call in active_calls]
            await asyncio.gather(*tasks, return_exceptions=True)

        except Exception as e:
            logger.error(f"Price monitor error: {e}")
            await asyncio.sleep(30)


async def _check_single_call(call, db):
    """Snapshot-only check. Does NOT mark WIN/LOSS."""
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
            await db.update_call_max_gain(call.id, max_gain=gain)

        # A4: do NOT resolve calls here. See module docstring.

    except Exception as e:
        logger.error(f"Error checking {call.token_address}: {e}")
