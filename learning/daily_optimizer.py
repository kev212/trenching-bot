import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from config import settings, load_filter_params, save_filter_params, load_filter_params_async, save_filter_params_async, load_adjustment_rules
from llm.pioneer_client import PioneerLLMClient
from llm.prompts import OPTIMIZER_SYSTEM, OPTIMIZER_USER
from llm.parser import parse_optimizer_suggestions

logger = logging.getLogger(__name__)


async def daily_optimizer(state, db):
    logger.info("Daily optimizer started")
    llm = PioneerLLMClient()

    while True:
        try:
            await asyncio.sleep(3600)
            await _run_optimization(state, db, llm)
        except Exception as e:
            logger.error(f"Daily optimizer error: {e}")
            await asyncio.sleep(300)


async def _run_optimization(state, db, llm: PioneerLLMClient):
    try:
        rules = load_adjustment_rules()

        recent_adjustments = await db.get_adjustments_since(
            datetime.now(timezone.utc) - timedelta(hours=rules["cooldown_hours"])
        )
        if recent_adjustments:
            logger.info("Skipping optimization: cooldown period active")
            return

        yesterday = datetime.now(timezone.utc) - timedelta(hours=24)
        calls = await db.get_calls_in_range(yesterday, datetime.now(timezone.utc))

        if len(calls) < rules["min_samples"]:
            logger.info(f"Not enough samples ({len(calls)} < {rules['min_samples']}), skipping")
            return

        wins = [c for c in calls if c.status.value == "WIN"]
        losses = [c for c in calls if c.status.value == "LOSS"]
        total_resolved = len(wins) + len(losses)

        # CRITICAL: gate on RESOLVED outcomes (WIN/LOSS), not PENDING.
        # Optimizing on pending calls = optimizing on noise = dangerous
        # directional adjustments based on zero information.
        min_resolved = rules.get("min_resolved_outcomes", 5)
        if total_resolved < min_resolved:
            logger.info(
                f"Skipping optimization: only {total_resolved} resolved outcomes "
                f"(W={len(wins)}, L={len(losses)}); need >= {min_resolved}"
            )
            return

        total = total_resolved

        win_rate = len(wins) / total * 100

        current_params = await load_filter_params_async()

        loss_data = []
        for call in losses:
            loss_data.append({
                "token": call.token_symbol,
                "score": call.llm_score,
                "verdict": call.llm_verdict,
                "max_gain": call.max_gain,
                "reasoning": call.llm_reasoning,
            })

        win_data = []
        for call in wins:
            win_data.append({
                "token": call.token_symbol,
                "score": call.llm_score,
                "max_gain": call.max_gain,
            })

        filter_perf = _calculate_filter_performance(calls, current_params.get("filters", {}))

        # Phase E2-Alert: enrich with filter_outcomes pass rates (from data, not theory)
        try:
            real_filter_perf = await db.get_filter_performance_since(
                datetime.now(timezone.utc) - timedelta(hours=24)
            )
            for fname, stats in real_filter_perf.items():
                if fname in filter_perf:
                    filter_perf[fname].update(stats)
                else:
                    filter_perf[fname] = stats
        except Exception as e:
            logger.warning(f"Could not load filter_outcomes for perf analysis: {e}")

        # Phase E2-Alert: pull actual saved loss analyses (LLM #3 root causes)
        try:
            real_loss_analyses = await _get_recent_loss_analyses(db, limit=10)
            if real_loss_analyses:
                loss_data = real_loss_analyses
        except Exception as e:
            logger.warning(f"Could not load loss_analyses: {e}")

        prompt = OPTIMIZER_USER.format(
            total_calls=len(calls),
            wins=len(wins),
            win_rate=win_rate,
            losses=len(losses),
            pending=len([c for c in calls if c.status.value == "PENDING"]),
            current_params_json=json.dumps(current_params.get("filters", {}), indent=2),
            loss_analysis_json=json.dumps(loss_data[:10], indent=2),
            win_patterns_json=json.dumps(win_data[:10], indent=2),
            filter_performance_json=json.dumps(filter_perf, indent=2),
        )

        result = await llm.analyze_token(OPTIMIZER_SYSTEM, prompt, temperature=0.3)
        suggestions = parse_optimizer_suggestions(result)

        if not suggestions:
            logger.info("No optimization suggestions from LLM")
            return

        valid_suggestions = _validate_adjustments(suggestions, rules, current_params.get("filters", {}))

        if not valid_suggestions:
            logger.info("No valid adjustments after validation")
            return

        filters = current_params.get("filters", {})
        # The version that will result from these adjustments is current+1.
        # We compute it ONCE here (all adjustments in a single cycle share
        # a version) and record it so revert monitor can do per-version
        # cohort analysis.
        new_version = current_params.get("version", 0) + 1
        for adj in valid_suggestions:
            filter_name = adj["filter"]
            param_name = adj["param"]
            new_value = adj["new_value"]

            if filter_name in filters and param_name in filters[filter_name]:
                old_value = filters[filter_name][param_name]
                filters[filter_name][param_name] = new_value

                await db.save_adjustment(
                    filter_name, param_name, old_value, new_value,
                    adj["reason"], adj["confidence"], win_rate,
                    resulting_version=new_version,
                )

                logger.info(f"Adjusted {filter_name}.{param_name}: {old_value} -> {new_value}")

        current_params["filters"] = filters
        current_params["version"] = new_version
        current_params["updated_at"] = datetime.now(timezone.utc).isoformat()

        await save_filter_params_async(current_params)
        await state.set_filter_params(filters, current_params["version"])

        logger.info(f"Optimization complete. Applied {len(valid_suggestions)} adjustments. New version: {current_params['version']}")

    except Exception as e:
        logger.error(f"Optimization run error: {e}")


def _calculate_filter_performance(calls: list, filters: dict) -> dict:
    perf = {}
    for name in filters:
        perf[name] = {"enabled": filters[name].get("enabled", True)}
    return perf


async def _get_recent_loss_analyses(db, limit: int = 10) -> list:
    """Pull recent LLM #3 loss analyses from DB (Phase E2-Alert)."""
    cursor = await db.db.execute(
        """SELECT token_symbol, root_cause, wrong_filter, suggestion, pattern,
                  confidence, max_gain
           FROM loss_analyses
           ORDER BY analyzed_at DESC
           LIMIT ?""",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [
        {
            "token": row["token_symbol"],
            "root_cause": row["root_cause"],
            "wrong_filter": row["wrong_filter"],
            "suggestion": row["suggestion"],
            "pattern": row["pattern"],
            "confidence": row["confidence"],
            "max_gain": row["max_gain"],
        }
        for row in rows
    ]


def _validate_adjustments(suggestions: list, rules: dict, current_filters: dict) -> list:
    valid = []
    max_change = rules.get("max_change_pct", 0.20)
    min_confidence = rules.get("min_confidence", 0.70)
    max_per_cycle = rules.get("max_adjustments_per_cycle", 3)
    param_floors = rules.get("param_floors", {})
    param_ceilings = rules.get("param_ceilings", {})
    never_disable = rules.get("never_disable_filters", False)

    for adj in suggestions:
        if len(valid) >= max_per_cycle:
            break

        # Defensive shape check
        if not all(k in adj for k in ("filter", "param", "new_value", "confidence")):
            logger.info(f"Skipping malformed adjustment: missing required keys")
            continue

        confidence = adj.get("confidence", 0)
        if confidence < min_confidence:
            logger.info(f"Skipping {adj['filter']}.{adj['param']}: confidence {confidence} < {min_confidence}")
            continue

        filter_name = adj["filter"]
        param_name = adj["param"]
        new_value = adj.get("new_value")

        if filter_name not in current_filters:
            continue
        if param_name not in current_filters[filter_name]:
            continue

        current_value = current_filters[filter_name][param_name]

        # never_disable: refuse to flip `enabled` (bool) AND refuse to
        # effectively disable a numeric filter by setting it to 0/1.
        if isinstance(current_value, bool):
            if never_disable and new_value != current_value:
                logger.info(
                    f"Skipping {filter_name}.{param_name}: never_disable_filters=True "
                    f"and adjustment flips bool (would disable)"
                )
                continue
        if never_disable and isinstance(current_value, (int, float)):
            # If a numeric param has a known minimum that still allows
            # the filter to function, the floor config enforces that.
            pass

        if not isinstance(current_value, (int, float)):
            continue

        # Magnitude clamp
        if current_value != 0:
            change_pct = abs(new_value - current_value) / abs(current_value)
            if change_pct > max_change:
                direction = 1 if new_value > current_value else -1
                new_value = current_value * (1 + direction * max_change)
                adj["new_value"] = new_value

        # Per-param FLOOR (safety: never below this) and CEILING (safety: never above).
        # E.g. min_holders can never drop below 75 — that's the rug-rug floor.
        if param_name in param_floors and new_value < param_floors[param_name]:
            logger.info(
                f"Clamping {filter_name}.{param_name}: {new_value} -> floor {param_floors[param_name]}"
            )
            new_value = param_floors[param_name]
            adj["new_value"] = new_value
        if param_name in param_ceilings and new_value > param_ceilings[param_name]:
            logger.info(
                f"Clamping {filter_name}.{param_name}: {new_value} -> ceiling {param_ceilings[param_name]}"
            )
            new_value = param_ceilings[param_name]
            adj["new_value"] = new_value

        valid.append(adj)

    return valid
