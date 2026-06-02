import asyncio
import logging
from datetime import datetime, timedelta, timezone

from config import load_filter_params, save_filter_params, load_adjustment_rules
from alerts.dispatcher import dispatcher

logger = logging.getLogger(__name__)


async def revert_monitor(state, db):
    logger.info("Revert monitor started")
    rules = load_adjustment_rules()
    # Use the configured interval (in hours) instead of a hardcoded 1h
    check_interval_hours = rules.get("revert_check_after_hours", 24)
    check_interval_seconds = max(60, check_interval_hours * 3600)  # at least 1min
    min_cohort = rules.get("min_per_filter_samples", 5)

    while True:
        try:
            await asyncio.sleep(check_interval_seconds)

            since = datetime.now(timezone.utc) - timedelta(hours=check_interval_hours)
            adjustments = await db.get_adjustments_since(since)

            if not adjustments:
                continue

            for adj in adjustments:
                # Per-version cohort: measure the impact of THIS adjustment
                # against calls made under the version IT produced. This
                # avoids confounding the win-rate comparison with old-param
                # calls (the previous global-snapshot logic could revert
                # a good adjustment because of pre-existing losses).
                resulting_version = adj.get("resulting_version") or 0
                if resulting_version <= 0:
                    # Legacy adjustments before this column was added.
                    # Skip rather than mis-attribute a global snapshot.
                    logger.debug(
                        f"Skip revert check for adj id={adj.get('id')}: no resulting_version"
                    )
                    continue

                total, wins, cohort_wr = await db.get_win_rate_for_version(resulting_version)
                if total < min_cohort:
                    # Not enough data under the new version yet — wait.
                    logger.info(
                        f"Revert check: version {resulting_version} has {total} "
                        f"resolved calls (need {min_cohort}), skipping"
                    )
                    continue

                wr_before = adj.get("win_rate_before", 0)
                threshold_pct = rules.get("auto_revert_threshold", 0.10) * 100

                if wr_before > 0 and (wr_before - cohort_wr) > threshold_pct:
                    logger.warning(
                        f"Cohort v{resulting_version} WR dropped "
                        f"{wr_before - cohort_wr:.1f}pp (>{threshold_pct:.0f}pp threshold), "
                        f"reverting {adj['filter_name']}.{adj['param_name']}"
                    )
                    await _revert_adjustment(adj, state, db, wr_before, cohort_wr, threshold_pct)

        except Exception as e:
            logger.error(f"Revert monitor error: {e}")


async def _revert_adjustment(
    adj: dict, state, db, wr_before: float, wr_after: float, threshold_pct: float
):
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
            current_params["updated_at"] = datetime.now(timezone.utc).isoformat()

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
                f"Threshold: {threshold_pct:.0f}%"
            )
            await dispatcher.send_message(msg)

            logger.info(f"Reverted {filter_name}.{param_name}: {new_value} -> {old_value}")

    except Exception as e:
        logger.error(f"Revert adjustment error: {e}")
