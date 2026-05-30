import asyncio
import logging
from datetime import datetime, timedelta

from config import load_filter_params, save_filter_params, load_adjustment_rules
from alerts.dispatcher import dispatcher

logger = logging.getLogger(__name__)


async def revert_monitor(state, db):
    logger.info("Revert monitor started")
    rules = load_adjustment_rules()
    check_interval = rules.get("revert_check_after_hours", 24) * 3600

    while True:
        try:
            await asyncio.sleep(3600)

            since = datetime.utcnow() - timedelta(hours=24)
            adjustments = await db.get_adjustments_since(since)

            if not adjustments:
                continue

            total, wins, current_wr = await db.get_win_rate_since(since)

            if total < 5:
                continue

            for adj in adjustments:
                wr_before = adj.get("win_rate_before", 0)
                threshold = rules.get("auto_revert_threshold", 0.10)

                if wr_before > 0 and (wr_before - current_wr) > threshold * 100:
                    logger.warning(f"Win rate dropped {wr_before - current_wr:.1f}%, reverting {adj['filter_name']}.{adj['param_name']}")
                    await _revert_adjustment(adj, state, db, wr_before, current_wr)

        except Exception as e:
            logger.error(f"Revert monitor error: {e}")


async def _revert_adjustment(adj: dict, state, db, wr_before: float, wr_after: float):
    try:
        filter_name = adj["filter_name"]
        param_name = adj["param_name"]
        old_value = adj["old_value"]
        new_value = adj["new_value"]

        current_params = load_filter_params()
        filters = current_params.get("filters", {})

        if filter_name in filters and param_name in filters[filter_name]:
            filters[filter_name][param_name] = old_value

            current_params["filters"] = filters
            current_params["version"] = current_params.get("version", 0) + 1
            current_params["updated_at"] = datetime.utcnow().isoformat()

            save_filter_params(current_params)
            await state.set_filter_params(filters, current_params["version"])

            await db.revert_adjustment(
                adj["id"],
                f"Win rate dropped from {wr_before:.1f}% to {wr_after:.1f}%"
            )

            msg = (
                f"⚠️ AUTO-REVERT\n\n"
                f"Filter: {filter_name}.{param_name}\n"
                f"Reverted: {new_value} → {old_value}\n"
                f"Reason: Win rate dropped {wr_before - wr_after:.1f}%\n"
                f"Threshold: {10}%"
            )
            await dispatcher.send_message(msg)

            logger.info(f"Reverted {filter_name}.{param_name}: {new_value} -> {old_value}")

    except Exception as e:
        logger.error(f"Revert adjustment error: {e}")
