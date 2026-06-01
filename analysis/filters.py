import logging
from datetime import datetime, timezone
from analysis.models import TokenData, FeatureVector

logger = logging.getLogger(__name__)


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

    passed = prob <= max_prob
    return {
        "probability": prob,
        "threshold": max_prob,
        "passed": passed,
        "enabled": True,
        "note": f"Rug probability: {prob:.0%} (max: {max_prob:.0%})",
    }


def _filter_holder_distribution(token: TokenData, params: dict) -> dict:
    max_top10 = params.get("max_top10_pct", 50)
    top10 = token.top10_hold_pct

    passed = top10 <= max_top10
    return {
        "top10_hold_pct": top10,
        "holders_count": token.holders_count,
        "threshold": max_top10,
        "passed": passed,
        "enabled": True,
        "note": f"Top10 hold: {top10:.1f}% (max: {max_top10}%), {token.holders_count} holders",
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
