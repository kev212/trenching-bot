import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from config import settings
from llm.mimo_client import MiMoClient
from llm.prompts import RECAP_LOSS_ANALYSIS_SYSTEM, RECAP_LOSS_ANALYSIS_USER
from llm.parser import parse_recap_loss_reasons

logger = logging.getLogger(__name__)


async def hourly_recap(state, db):
    logger.info("Hourly recap scheduler started")
    mimo = MiMoClient()

    while True:
        try:
            now = datetime.now(timezone.utc)
            next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
            wait_seconds = (next_hour - now).total_seconds()
            logger.info(f"Next recap at {next_hour.isoformat()} (in {wait_seconds:.0f}s)")
            await asyncio.sleep(wait_seconds)

            await _generate_recap(state, db, mimo)

        except Exception as e:
            logger.error(f"Hourly recap error: {e}")
            await asyncio.sleep(60)


async def _generate_recap(state, db, mimo: MiMoClient):
    try:
        now = datetime.now(timezone.utc)
        period_end = now
        period_start = now - timedelta(hours=1)

        calls = await db.get_calls_in_range(period_start, period_end)

        if not calls:
            logger.info("No calls in the last hour, skipping recap")
            return

        wins = [c for c in calls if c.status.value == "WIN"]
        losses = [c for c in calls if c.status.value == "LOSS"]
        pending = [c for c in calls if c.status.value == "PENDING"]

        total = len(calls)
        win_count = len(wins)
        loss_count = len(losses)
        pending_count = len(pending)
        win_rate = (win_count / (win_count + loss_count) * 100) if (win_count + loss_count) > 0 else 0

        gains = [c.max_gain for c in calls if c.max_gain > 1.0]
        avg_gain = sum(gains) / len(gains) if gains else 1.0

        best_call = max(calls, key=lambda c: c.max_gain) if calls else None
        best_token = best_call.token_symbol if best_call else "N/A"
        best_gain = best_call.max_gain if best_call else 1.0

        loss_reasons = {}
        if losses:
            loss_reasons = await _analyze_losses_batch(losses, mimo)

        recap = {
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "total": total,
            "wins": win_count,
            "losses": loss_count,
            "pending": pending_count,
            "win_rate": win_rate,
            "avg_gain": avg_gain,
            "best_token": best_token,
            "best_gain": best_gain,
            "calls": calls,
            "loss_reasons": loss_reasons,
        }

        await db.save_recap({
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "total": total,
            "wins": win_count,
            "losses": loss_count,
            "pending": pending_count,
            "win_rate": win_rate,
            "avg_gain": avg_gain,
            "best_token": best_token,
            "best_gain": best_gain,
            "llm_loss_analysis": json.dumps(loss_reasons),
        })

        await db.save_daily_stats(now.strftime("%Y-%m-%d"), {
            "total": total,
            "wins": win_count,
            "losses": loss_count,
            "pending": pending_count,
            "win_rate": win_rate,
            "avg_gain": avg_gain,
            "best_token": best_token,
            "best_gain": best_gain,
        })

        state.metrics.record_alert()
        logger.info(f"Recap generated: {total} calls, {win_rate:.1f}% win rate")

        return recap

    except Exception as e:
        logger.error(f"Generate recap error: {e}")
        return None


async def _analyze_losses_batch(losses: list, mimo: MiMoClient) -> dict:
    try:
        losses_data = []
        for call in losses:
            losses_data.append({
                "token": call.token_symbol,
                "score": call.llm_score,
                "verdict": call.llm_verdict,
                "max_gain": call.max_gain,
                "reasoning": call.llm_reasoning,
            })

        prompt = RECAP_LOSS_ANALYSIS_USER.format(
            losses_json=json.dumps(losses_data, indent=2)
        )

        result = await mimo.analyze_token(RECAP_LOSS_ANALYSIS_SYSTEM, prompt, temperature=0.2)

        if isinstance(result, list):
            return parse_recap_loss_reasons(result)
        elif isinstance(result, dict) and "reasons" in result:
            return parse_recap_loss_reasons(result["reasons"])
        return {}

    except Exception as e:
        logger.error(f"Batch loss analysis error: {e}")
        return {}
