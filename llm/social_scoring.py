"""Social signals bonus calculation.

Replaces the old simple `max(weight)` with a multi-component scoring
system that values organic engagement and real-world catalysts over
single celebrity name-drops.
"""
import time
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("social_scoring")

CONFIG_PATH = Path(__file__).parent.parent / "config" / "social_scoring.json"

_config_cache: Optional[dict] = None


def load_social_scoring_config() -> dict:
    """Load config/social_scoring.json (cached)."""
    global _config_cache
    if _config_cache is None:
        try:
            with open(CONFIG_PATH) as f:
                _config_cache = json.load(f)
        except Exception as e:
            logger.error(f"Failed to load social_scoring.json: {e}")
            _config_cache = _default_config()
    return _config_cache


def _default_config() -> dict:
    """Fallback if config file is missing."""
    return {
        "caps": {"mega": 12, "major": 8, "mid": 6, "niche_no_cap": True},
        "consensus_multiplier": 0.3,
        "organic_spread": {
            "three_authors": 5,
            "seven_authors": 10,
            "fifteen_authors": 15,
        },
        "engagement": {
            "tier1_likes": 20,
            "tier2_likes": 100,
            "tier3_likes": 500,
            "tier1_min_count": 2,
            "tier2_min_count": 5,
            "tier1_bonus": 5,
            "tier2_bonus": 10,
            "tier3_bonus": 5,
        },
        "catalyst_bonus": 8,
        "time_decay": {
            "fresh_hours": 6,
            "aging_hours": 24,
            "fresh_multiplier": 1.0,
            "aging_multiplier": 0.5,
            "stale_multiplier": 0.25,
        },
        "max_total_bonus": 40,
    }


def _tweet_decay(age_min: float, decay_cfg: dict) -> float:
    """Apply time decay multiplier based on tweet age."""
    fresh_h = decay_cfg["fresh_hours"]
    aging_h = decay_cfg["aging_hours"]
    if age_min < fresh_h * 60:
        return decay_cfg["fresh_multiplier"]
    elif age_min < aging_h * 60:
        return decay_cfg["aging_multiplier"]
    else:
        return decay_cfg["stale_multiplier"]


def _cap_for_weight(weight: int, caps_cfg: dict) -> int:
    """Tiered cap based on influencer weight."""
    if weight >= 20:
        return caps_cfg["mega"]
    elif weight >= 10:
        return caps_cfg["major"]
    elif weight >= 5:
        return caps_cfg["mid"]
    else:
        # Niche tier: no cap (weight = cap)
        return weight if caps_cfg.get("niche_no_cap", True) else caps_cfg["mid"]


def _influencer_bonus(token, cfg: dict) -> int:
    """Compute influencer consensus bonus with caps and decay."""
    if not token.influencer_mentions:
        return 0

    decay_cfg = cfg["time_decay"]
    caps_cfg = cfg["caps"]
    consensus_mult = cfg["consensus_multiplier"]

    # Group by handle (one author may have multiple tweets)
    unique_handles = {}
    for inf in token.influencer_mentions:
        handle = inf.get("handle", "")
        age_min = inf.get("tweet_age_min", 0)
        decay = _tweet_decay(age_min, decay_cfg)
        effective_weight = inf.get("weight", 0) * decay
        if handle not in unique_handles or unique_handles[handle] < effective_weight:
            unique_handles[handle] = effective_weight

    if not unique_handles:
        return 0

    # Cap each individual influencer
    capped = []
    for handle, eff_weight in unique_handles.items():
        raw_weight = next((inf["weight"] for inf in token.influencer_mentions
                           if inf.get("handle") == handle), int(eff_weight))
        cap = _cap_for_weight(raw_weight, caps_cfg)
        capped.append(min(eff_weight, cap))

    # Sort desc, take max for base, add consensus bonus
    capped.sort(reverse=True)
    base = capped[0]
    extra_authors = len(capped) - 1
    consensus_bonus = base * consensus_mult * extra_authors

    return int(base + consensus_bonus)


def _organic_spread_bonus(token, cfg: dict) -> int:
    """Count unique non-influencer authors and apply tiered bonus."""
    if not token.organic_mentions:
        return 0

    unique_authors = {m.get("handle", "") for m in token.organic_mentions if m.get("handle")}
    n = len(unique_authors)

    spread = cfg["organic_spread"]
    if n >= 15:
        return spread["fifteen_authors"]
    elif n >= 7:
        return spread["seven_authors"]
    elif n >= 3:
        return spread["three_authors"]
    return 0


def _engagement_quality_bonus(token, cfg: dict) -> int:
    """3-tier engagement quality (20+, 100+, 500+ likes)."""
    if not token.organic_mentions:
        return 0

    eng = cfg["engagement"]
    bonus = 0

    # Tier 1: 2+ tweets with 20+ likes
    tier1_count = sum(1 for m in token.organic_mentions if m.get("likes", 0) >= eng["tier1_likes"])
    if tier1_count >= eng["tier1_min_count"]:
        bonus += eng["tier1_bonus"]

    # Tier 2: 5+ tweets with 100+ likes (viral)
    tier2_count = sum(1 for m in token.organic_mentions if m.get("likes", 0) >= eng["tier2_likes"])
    if tier2_count >= eng["tier2_min_count"]:
        bonus += eng["tier2_bonus"]

    # Tier 3: any single tweet with 500+ likes (mega viral)
    if any(m.get("likes", 0) >= eng["tier3_likes"] for m in token.organic_mentions):
        bonus += eng["tier3_bonus"]

    return bonus


def _catalyst_bonus(token, cfg: dict) -> int:
    """Bonus if LLM #1 detected a real-world catalyst."""
    if token.catalyst_match:
        return cfg["catalyst_bonus"]
    return 0


def calculate_social_signals_bonus(token) -> dict:
    """Compute total social signals bonus for a token.

    Returns:
        {
            "total_bonus": int (capped at max_total_bonus),
            "breakdown": {
                "influencer": int,
                "organic_spread": int,
                "engagement": int,
                "catalyst": int,
            }
        }
    """
    cfg = load_social_scoring_config()

    breakdown = {
        "influencer": _influencer_bonus(token, cfg),
        "organic_spread": _organic_spread_bonus(token, cfg),
        "engagement": _engagement_quality_bonus(token, cfg),
        "catalyst": _catalyst_bonus(token, cfg),
    }

    raw_total = sum(breakdown.values())
    capped_total = min(raw_total, cfg["max_total_bonus"])

    return {
        "total_bonus": capped_total,
        "breakdown": breakdown,
    }
