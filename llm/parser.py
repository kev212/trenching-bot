import json
import logging
from analysis.models import LLMDecision, Verdict

logger = logging.getLogger(__name__)


def parse_decision(raw: dict) -> LLMDecision:
    if raw is None:
        return LLMDecision(
            score=0,
            verdict=Verdict.SKIP,
            reasoning="LLM returned None",
            confidence=0.0,
            key_factors=[],
        )
    try:
        score = int(raw.get("score", 0))
        score = max(0, min(100, score))

        verdict_str = raw.get("verdict", "SKIP").upper()
        try:
            verdict = Verdict(verdict_str)
        except ValueError:
            verdict = Verdict.SKIP

        # Enforce score→verdict rules (override LLM if inconsistent)
        if score >= 70:
            verdict = Verdict.APE
        elif score >= 50:
            verdict = Verdict.WATCH
        elif score < 40 and verdict == Verdict.APE:
            verdict = Verdict.SKIP

        return LLMDecision(
            score=score,
            verdict=verdict,
            reasoning=raw.get("reasoning", ""),
            confidence=float(raw.get("confidence", 0.0)),
            key_factors=raw.get("key_factors", []),
            processing_time_ms=raw.get("_processing_time_ms", 0),
        )
    except Exception as e:
        logger.error(f"Error parsing LLM decision: {e}")
        return LLMDecision(
            score=0,
            verdict=Verdict.SKIP,
            reasoning=f"Parse error: {str(e)}",
            confidence=0.0,
            key_factors=[],
        )


def parse_loss_analysis(raw) -> dict:
    if raw is None:
        return {
            "root_cause": "LLM unavailable",
            "wrong_filter": "Unknown",
            "suggestion": "Retry when LLM recovers",
            "pattern": "LLM failure — no analysis available",
            "confidence": 0.0,
        }
    return {
        "root_cause": raw.get("root_cause", "Unknown"),
        "wrong_filter": raw.get("wrong_filter", "Unknown"),
        "suggestion": raw.get("suggestion", "No suggestion"),
        "pattern": raw.get("pattern", "No pattern identified"),
        "confidence": float(raw.get("confidence", 0.0)),
    }


def parse_optimizer_suggestions(raw: dict):
    adjustments = raw.get("adjustments", [])
    valid = []
    for adj in adjustments:
        if all(k in adj for k in ["filter", "param", "old_value", "new_value", "reason", "confidence"]):
            valid.append(adj)
    return valid


def parse_recap_loss_reasons(raw: list) -> dict[str, str]:
    if not isinstance(raw, list):
        return {}
    result = {}
    for item in raw:
        token = item.get("token", "")
        reason = item.get("reason", "")
        if token:
            result[token] = reason
    return result
