import time
import logging
from datetime import datetime, timezone
from analysis.models import TokenData, FeatureVector

logger = logging.getLogger(__name__)

# Filters whose value will NOT change in the retry window (60-300s).
# Tokens that fail these filters should NOT be retried — they'll
# fail again at the same value, wasting rate-limit budget.
PERMANENT_FILTERS = frozenset({
    "min_total_fee",
})

# Compound rule: token (pre or post migrate) older than COMPOUND_AGE_MINUTES
# with fee below COMPOUND_FEE_MIN_SOL has had enough time to prove traction
# but hasn't — skip retry permanently.
COMPOUND_FEE_MIN_SOL = 1.0
COMPOUND_AGE_MINUTES = 30.0


def is_permanent_failure(failures: list[str]) -> bool:
    """True if any of the failures is a permanent filter."""
    return any(f in PERMANENT_FILTERS for f in failures)


def is_compound_permanent_failure(
    failures: list[str],
    token: TokenData,
) -> bool:
    """Check if a token qualifies for compound permanent skip.

    Rule: min_total_fee failed AND age > 30min AND fee < 1 SOL (pre or post
    migrate). Such a token has had enough time to prove traction but hasn't.
    No point retrying — skip permanently.
    """
    if "min_total_fee" not in failures:
        return False
    age_seconds = time.time() - max(
        token.creation_timestamp or 0, token.open_timestamp or 0
    )
    age_minutes = age_seconds / 60
    return (
        age_minutes > COMPOUND_AGE_MINUTES
        and token.fee_collected < COMPOUND_FEE_MIN_SOL
    )


def run_all_filters(token: TokenData, filter_params: dict) -> FeatureVector:
    fv = FeatureVector(token_data=token)

    fv.token_age = _filter_token_age(token, filter_params.get("token_age", {}))
    fv.min_market_cap = _filter_min_market_cap(token, filter_params.get("min_market_cap", {}))
    fv.max_market_cap = _filter_max_market_cap(token, filter_params.get("max_market_cap", {}))
    fv.min_total_fee = _filter_min_total_fee(token, filter_params.get("min_total_fee", {}))
    fv.fee_tier = _filter_fee_tier(token, filter_params.get("fee_tier", {}))
    fv.insider_concentration = _filter_insider_concentration(token, filter_params.get("insider_concentration", {}))
    fv.min_holders = _filter_min_holders(token, filter_params.get("min_holders", {}))
    fv.funded_wallet_age = _filter_funded_wallet_age(token, filter_params.get("funded_wallet_age", {}))
    fv.rug_probability = _filter_rug_probability(token, filter_params.get("rug_probability", {}))
    fv.holder_distribution = _filter_holder_distribution(token, filter_params.get("holder_distribution", {}))
    fv.social_narrative = _filter_social_narrative(token, filter_params.get("social_narrative", {}))
    fv.ath_drawdown = _filter_ath_drawdown(token, filter_params.get("ath_drawdown", {}))

    return fv


def _filter_token_age(token: TokenData, params: dict) -> dict:
    max_pre_migrate = params.get("max_pre_migrate_minutes", 120)
    max_post_migrate = params.get("max_post_migrate_minutes", 45)
    now = datetime.now(timezone.utc).timestamp()

    if token.migrated_timestamp > 0:
        # Post-migrate: use open_timestamp, max 45 min
        if token.open_timestamp > 0:
            age_min = (now - token.open_timestamp) / 60
            max_minutes = max_post_migrate
            status = "post-migrate"
        else:
            return {
                "age_minutes": None,
                "threshold": max_post_migrate,
                "passed": False,
                "enabled": True,
                "note": "Post-migrate but no open_timestamp",
            }
    else:
        # Pre-migrate: use creation_timestamp, max 120 min
        if token.creation_timestamp > 0:
            age_min = (now - token.creation_timestamp) / 60
            max_minutes = max_pre_migrate
            status = "pre-migrate"
        elif token.created_at:
            age_min = (now - token.created_at.timestamp()) / 60
            max_minutes = max_pre_migrate
            status = "pre-migrate"
        else:
            return {
                "age_minutes": None,
                "threshold": max_pre_migrate,
                "passed": False,
                "enabled": True,
                "note": "No creation timestamp",
            }

    passed = age_min <= max_minutes
    return {
        "age_minutes": age_min,
        "threshold": max_minutes,
        "status": status,
        "passed": passed,
        "enabled": True,
        "note": f"Age: {age_min:.0f}min (max: {max_minutes}min) [{status}]",
    }


def _filter_min_market_cap(token: TokenData, params: dict) -> dict:
    min_mc = params.get("min_mc_usd", 7000)
    mc = token.market_cap

    passed = mc >= min_mc
    return {
        "market_cap": mc,
        "threshold": min_mc,
        "passed": passed,
        "enabled": True,
        "note": f"MC: ${mc:,.0f} (min: ${min_mc:,})",
    }


def _filter_max_market_cap(token: TokenData, params: dict) -> dict:
    max_mc = params.get("max_mc_usd", 200000)
    mc = token.market_cap

    passed = mc <= max_mc or mc <= 0
    return {
        "market_cap": mc,
        "threshold": max_mc,
        "passed": passed,
        "enabled": True,
        "note": f"MC: ${mc:,.0f} (max: ${max_mc:,})",
    }


def _filter_min_total_fee(token: TokenData, params: dict) -> dict:
    min_fee = params.get("min_fee_sol", 0.5)
    fee = token.fee_collected  # dalam SOL

    passed = fee >= min_fee
    return {
        "fee_sol": fee,
        "threshold": min_fee,
        "passed": passed,
        "enabled": True,
        "note": f"Fee: {fee:.2f} SOL (min: {min_fee} SOL)",
    }


def _filter_fee_tier(token: TokenData, params: dict) -> dict:
    fee = token.fee_collected  # dalam SOL
    mc = token.market_cap      # dalam USD

    if mc <= 0:
        return {
            "fee_sol": 0,
            "mc_usd": 0,
            "min_fee_sol": 0,
            "passed": False,
            "enabled": True,
            "note": "No MC data",
        }

    # 3-tier fee system
    if mc < 50000:
        # MC < $50K: proportional 1 SOL per $10K
        min_fee = mc / 10000
        tier = "proportional"
    elif mc <= 100000:
        # $50K <= MC <= $100K: flat 5 SOL
        min_fee = 5.0
        tier = "flat-5"
    else:
        # MC > $100K: flat 10 SOL
        min_fee = 10.0
        tier = "flat-10"

    passed = fee >= min_fee

    return {
        "fee_sol": fee,
        "mc_usd": mc,
        "min_fee_sol": min_fee,
        "tier": tier,
        "passed": passed,
        "enabled": True,
        "note": f"Fee: {fee:.2f} SOL (min: {min_fee:.2f} SOL [{tier}] for ${mc:,.0f} MC)",
    }


def _filter_insider_concentration(token: TokenData, params: dict) -> dict:
    if not params.get("enabled", True):
        return {
            "insider_ratio": token.insider_ratio,
            "threshold": None,
            "passed": True,
            "enabled": False,
            "note": "Insider concentration check disabled",
        }

    max_insider = params.get("max_insider_ratio", 0.50)
    insider = token.insider_ratio

    passed = insider <= max_insider
    return {
        "insider_ratio": insider,
        "threshold": max_insider,
        "passed": passed,
        "enabled": True,
        "note": f"Insider ratio: {insider:.1%} (max: {max_insider:.0%})",
    }


def _filter_min_holders(token: TokenData, params: dict) -> dict:
    min_holders = params.get("min_holders", 100)
    holders = token.holders_count

    passed = holders >= min_holders
    return {
        "holders_count": holders,
        "threshold": min_holders,
        "passed": passed,
        "enabled": True,
        "note": f"Holders: {holders} (min: {min_holders})",
    }


def _filter_funded_wallet_age(token: TokenData, params: dict) -> dict:
    max_new_pct = params.get("max_new_wallet_pct", 30)
    new_pct = token.funded_wallet_new_pct

    passed = new_pct <= max_new_pct
    return {
        "new_wallet_pct": new_pct,
        "threshold": max_new_pct,
        "passed": passed,
        "enabled": True,
        "note": f"{new_pct:.1f}% new wallets (threshold: {max_new_pct}%)",
    }


def _filter_rug_probability(token: TokenData, params: dict) -> dict:
    max_prob = params.get("max_rug_prob", 0.40)
    prob = token.rug_probability

    return {
        "probability": prob,
        "threshold": max_prob,
        "passed": prob <= max_prob,
        "enabled": params.get("enabled", True),
        "note": f"Rug probability: {prob:.0%} (max: {max_prob:.0%})",
    }


def _filter_holder_distribution(token: TokenData, params: dict) -> dict:
    max_top15 = params.get("max_top15_pct", 50)
    top15 = token.top15_hold_pct

    passed = top15 <= max_top15
    return {
        "top15_hold_pct": top15,
        "holders_count": token.holders_count,
        "threshold": max_top15,
        "passed": passed,
        "enabled": True,
        "note": f"Top15 hold: {top15:.1f}% (max: {max_top15}%), {token.holders_count} holders",
    }


def _filter_social_narrative(token: TokenData, params: dict) -> dict:
    score = token.social_narrative_score
    project_type = token.project_type
    influencer_count = len(token.influencer_mentions)
    organic_count = len(getattr(token, "organic_mentions", []))
    has_twitter = bool(token.twitter_username)
    has_website = bool(token.website_url)
    has_telegram = bool(token.telegram_url)
    has_community = bool(getattr(token, "has_community", False))
    catalyst_match = bool(getattr(token, "catalyst_match", False))
    catalyst_description = getattr(token, "catalyst_description", "")

    return {
        "score": score,
        "project_type": project_type,
        "influencer_count": influencer_count,
        "organic_count": organic_count,
        "has_twitter": has_twitter,
        "has_website": has_website,
        "has_telegram": has_telegram,
        "has_community": has_community,
        "catalyst_match": catalyst_match,
        "catalyst_description": catalyst_description,
        "passed": True,  # Always passes (bonus, not hard gate)
        "enabled": True,
        "note": f"Social: {score:.0f}/100 ({project_type}) {influencer_count} inf / {organic_count} org / catalyst={catalyst_match}",
    }


def count_passed_filters(fv: FeatureVector) -> tuple[int, int, list[str]]:
    """Count passed filters and return failures list for hard gate.

    Filters with `enabled: False` are auto-passed (not counted as failures).
    """
    filters = {
        "token_age": fv.token_age,
        "min_market_cap": fv.min_market_cap,
        "max_market_cap": fv.max_market_cap,
        "min_total_fee": fv.min_total_fee,
        "fee_tier": fv.fee_tier,
        "insider_concentration": fv.insider_concentration,
        "min_holders": fv.min_holders,
        "funded_wallet_age": fv.funded_wallet_age,
        "rug_probability": fv.rug_probability,
        "holder_distribution": fv.holder_distribution,
        "ath_drawdown": fv.ath_drawdown,
        "social_narrative": fv.social_narrative,
    }

    passed = 0
    failures = []
    total = 0

    for name, data in filters.items():
        if not data.get("enabled", True):
            continue
        total += 1
        if data.get("passed", False):
            passed += 1
        else:
            failures.append(name)

    return passed, total, failures


def check_hard_gate(fv: FeatureVector) -> tuple[bool, list[str]]:
    """Hard gate: ALL filters must pass. Returns (passed, failures)."""
    _, _, failures = count_passed_filters(fv)
    return len(failures) == 0, failures


SKIP_WEIGHT_SCORING = {"social_narrative", "token_data"}


def calculate_weighted_score(fv: FeatureVector, filter_params: dict) -> tuple[float, dict]:
    """Calculate weighted score from hard gate filters (0.0-1.0).

    social_narrative is excluded — handled separately by LLM #1.
    Returns (score, breakdown) where breakdown shows per-filter details.
    """
    total_weight = 0.0
    weighted_sum = 0.0
    breakdown = {}

    filters = filter_params.get("filters", filter_params)

    for name, data in fv.to_dict().items():
        if name in SKIP_WEIGHT_SCORING:
            continue
        params = filters.get(name, {})
        weight = params.get("weight", 0)
        enabled = params.get("enabled", True)
        if not enabled or weight == 0:
            continue

        passed = data.get("passed", False)
        total_weight += weight
        if passed:
            weighted_sum += weight
        breakdown[name] = {
            "weight": weight,
            "passed": passed,
            "contribution": weight if passed else 0,
        }

    score = weighted_sum / total_weight if total_weight > 0 else 0.0
    return score, breakdown


def _filter_ath_drawdown(token: TokenData, params: dict) -> dict:
    """Filter by max drawdown from ATH (all-time high price)."""
    max_dd = params.get("max_drawdown_pct", -50.0)
    dd = token.drawdown_from_ath_pct
    passed = dd >= max_dd
    return {
        "drawdown_pct": dd,
        "ath_price": token.ath_price,
        "threshold": max_dd,
        "passed": passed,
        "enabled": params.get("enabled", True),
        "note": f"Drawdown from ATH: {dd:.1f}% (max: {max_dd}%)",
    }
