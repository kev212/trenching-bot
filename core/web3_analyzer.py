"""Web3 project substance analysis (LLM #3).

Dedicated LLM call to evaluate project substance for web3_project tokens.
Returns substance_score 0-100 plus red_flags. Falls back to neutral 50
on LLM error so the token isn't auto-rejected.
"""
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from llm.pioneer_client import PioneerLLMClient
from llm.prompts import WEB3_SUBSTANCE_SYSTEM, WEB3_SUBSTANCE_USER

logger = logging.getLogger(__name__)


async def analyze_web3_substance(token, llm: PioneerLLMClient) -> dict:
    """Run LLM #3 to evaluate web3 project substance.

    Returns dict:
        {
            "substance_score": float 0-100,
            "red_flags": list[str],
            "reasoning": str,
            "team_visible": bool,
            "has_github": bool,
            "has_audit": bool,
            "audit_firm": str,
            "_processing_time_ms": int,
        }
    """
    start = time.time()
    # B3 fix: was 50 (neutral) which biased tokens toward WATCH. A failed
    # LLM #3 means we have *no* signal — treat that as missing data and
    # let downstream filters make the call. Substance score 0 with no
    # red flags is the correct representation of "we don't know".
    fallback = {
        "substance_score": 0.0,  # missing-data signal, not bias
        "red_flags": [],
        "reasoning": "LLM #3 fallback (no signal)",
        "team_visible": False,
        "has_github": False,
        "has_audit": False,
        "audit_firm": "",
        "_processing_time_ms": 0,
    }

    if not getattr(token, "project_type", "") == "web3_project":
        return fallback

    try:
        if token.creation_timestamp > 0:
            age_min = (datetime.now(timezone.utc).timestamp() - token.creation_timestamp) / 60
            if age_min < 60:
                age_description = f"{age_min:.0f} minutes ago"
            else:
                age_description = f"{age_min/60:.1f} hours ago"
        else:
            age_description = "unknown"

        gmgn_raw_str = json.dumps(getattr(token, "raw_gmgn", {}) or {}, default=str)[:2000]

        prompt = WEB3_SUBSTANCE_USER.format(
            token_name=token.name,
            token_symbol=token.symbol,
            market_cap=token.market_cap,
            age_description=age_description,
            website_text=(token.website_text or "")[:1500] or "No website content",
            twitter_description=(token.twitter_description or "")[:500] or "No Twitter description",
            gmgn_raw=gmgn_raw_str,
        )

        result = await llm.analyze_token(WEB3_SUBSTANCE_SYSTEM, prompt)

        elapsed_ms = int((time.time() - start) * 1000)

        # LLM #3 failure is a separate signal from a 0 score. B3 fix:
        # don't bias toward WATCH with neutral 50 — return missing-data 0.
        if result is None:
            logger.warning(
                f"[LLM-3-FAIL] {token.symbol}: LLM #3 returned no result; "
                "defaulting to substance 0 (no signal), no red flags"
            )
            return {
                "substance_score": 0.0,
                "red_flags": [],
                "reasoning": "LLM error (no signal)",
                "team_visible": False,
                "has_github": False,
                "has_audit": False,
                "audit_firm": "",
                "_processing_time_ms": elapsed_ms,
            }

        substance_score = float(result.get("substance_score", 50))
        substance_score = max(0, min(100, substance_score))

        # Apply red-flag cap (per the rubric: cap at 40)
        # Match by keyword substring (LLM emits free-text like "Fake/plagiarized
        # whitepaper" — don't rely on exact snake_case keys).
        red_flags = result.get("red_flags", []) or []
        if isinstance(red_flags, str):
            red_flags = [red_flags]
        critical_keywords = ["fake_audit", "stolen", "plagiar", "fake_team", "fake team", "copied"]
        flags_text = " ".join(str(f).lower() for f in red_flags)
        if any(kw in flags_text for kw in critical_keywords):
            substance_score = min(substance_score, 40)
            logger.warning(
                f"[LLM-3] {token.symbol}: critical red flags detected, capped at 40: {red_flags}"
            )

        return {
            "substance_score": substance_score,
            "red_flags": red_flags,
            "reasoning": result.get("reasoning", ""),
            "team_visible": bool(result.get("team_visible", False)),
            "has_github": bool(result.get("has_github", False)),
            "has_audit": bool(result.get("has_audit", False)),
            "audit_firm": result.get("audit_firm", "") or "",
            "_processing_time_ms": elapsed_ms,
        }

    except Exception as e:
        elapsed_ms = int((time.time() - start) * 1000)
        logger.error(f"[LLM-3] {token.symbol} error: {e}")
        fallback["_processing_time_ms"] = elapsed_ms
        return fallback
