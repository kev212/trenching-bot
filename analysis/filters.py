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
    fv.mc_fee_ratio = _filter_mc_fee_ratio(token, filter_params.get("mc_fee_ratio", {}))
    fv.bundle_detection = _filter_bundle_detection(token, filter_params.get("bundle_detection", {}))
    fv.wash_trading = _filter_wash_trading(token, filter_params.get("wash_trading", {}))
    fv.min_holders = _filter_min_holders(token, filter_params.get("min_holders", {}))
    fv.top_holder_balance = _filter_top_holder_balance(token, filter_params.get("top_holder_balance", {}))
    fv.funded_wallet_age = _filter_funded_wallet_age(token, filter_params.get("funded_wallet_age", {}))
    fv.rug_probability = _filter_rug_probability(token, filter_params.get("rug_probability", {}))
    fv.holder_distribution = _filter_holder_distribution(token, filter_params.get("holder_distribution", {}))
    fv.social_narrative = _filter_social_narrative(token, filter_params.get("social_narrative", {}))

    return fv


def _filter_token_age(token: TokenData, params: dict) -> dict:
    max_minutes = params.get("max_token_age_minutes", 30)

    if not token.created_at:
        return {
            "age_minutes": None,
            "threshold": max_minutes,
            "passed": False,
            "note": "No creation timestamp",
        }

    age_minutes = (datetime.now(timezone.utc) - token.created_at).total_seconds() / 60
    passed = age_minutes <= max_minutes

    return {
        "age_minutes": age_minutes,
        "threshold": max_minutes,
        "passed": passed,
        "note": f"Age: {age_minutes:.0f}min (max: {max_minutes}min)",
    }


def _filter_min_market_cap(token: TokenData, params: dict) -> dict:
    min_mc = params.get("min_mc_usd", 7000)
    mc = token.market_cap

    passed = mc >= min_mc
    return {
        "market_cap": mc,
        "threshold": min_mc,
        "passed": passed,
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
        "note": f"Fee: {fee:.2f} SOL (min: {min_fee} SOL)",
    }


def _filter_mc_fee_ratio(token: TokenData, params: dict) -> dict:
    # Rule: min 1 SOL fee per $10K MC
    min_fee_per_10k = params.get("min_fee_sol_per_10k_mc", 1.0)
    fee = token.fee_collected  # dalam SOL
    mc = token.market_cap      # dalam USD

    if mc <= 0:
        return {
            "fee_sol": 0,
            "mc_usd": 0,
            "min_fee_sol": 0,
            "passed": False,
            "note": "No MC data",
        }

    min_fee = (mc / 10000) * min_fee_per_10k
    passed = fee >= min_fee

    return {
        "fee_sol": fee,
        "mc_usd": mc,
        "min_fee_sol": min_fee,
        "passed": passed,
        "note": f"Fee: {fee:.2f} SOL (min: {min_fee:.2f} SOL for ${mc:,.0f} MC)",
    }


def _filter_bundle_detection(token: TokenData, params: dict) -> dict:
    max_insider = params.get("max_insider_ratio", 0.50)
    insider = token.insider_ratio

    passed = insider <= max_insider
    return {
        "insider_ratio": insider,
        "threshold": max_insider,
        "passed": passed,
        "note": f"Insider ratio: {insider:.1%} (max: {max_insider:.0%})",
    }


def _filter_wash_trading(token: TokenData, params: dict) -> dict:
    is_wash = token.is_wash_trading

    passed = not is_wash
    return {
        "is_wash_trading": is_wash,
        "passed": passed,
        "note": "Wash trading detected!" if is_wash else "No wash trading",
    }


def _filter_min_holders(token: TokenData, params: dict) -> dict:
    min_holders = params.get("min_holders", 100)
    holders = token.holders_count

    passed = holders >= min_holders
    return {
        "holders_count": holders,
        "threshold": min_holders,
        "passed": passed,
        "note": f"Holders: {holders} (min: {min_holders})",
    }


def _filter_top_holder_balance(token: TokenData, params: dict) -> dict:
    min_sol = params.get("min_balance_sol", 0.2)
    balance = token.top_holder_balance_sol

    passed = balance >= min_sol
    return {
        "balance_sol": balance,
        "threshold": min_sol,
        "passed": passed,
        "note": f"Top holder balance: {balance:.2f} SOL (min: {min_sol})",
    }


def _filter_funded_wallet_age(token: TokenData, params: dict) -> dict:
    max_new_pct = params.get("max_new_wallet_pct", 30)
    new_pct = token.funded_wallet_new_pct

    passed = new_pct <= max_new_pct
    return {
        "new_wallet_pct": new_pct,
        "threshold": max_new_pct,
        "passed": passed,
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
        "note": f"Top10 hold: {top10:.1f}% (max: {max_top10}%), {token.holders_count} holders",
    }


def _filter_social_narrative(token: TokenData, params: dict) -> dict:
    score = token.social_narrative_score
    project_type = token.project_type
    influencer_count = len(token.influencer_mentions)
    has_twitter = bool(token.twitter_username)
    has_website = bool(token.website_url)

    return {
        "score": score,
        "project_type": project_type,
        "influencer_count": influencer_count,
        "has_twitter": has_twitter,
        "has_website": has_website,
        "passed": True,  # Always passes (bonus, not hard gate)
        "note": f"Social: {score:.0f}/100 ({project_type}) {influencer_count} influencers",
    }


def count_passed_filters(fv: FeatureVector) -> tuple[int, int, list[str]]:
    """Count passed filters and return failures list for hard gate."""
    filters = {
        "token_age": fv.token_age,
        "min_market_cap": fv.min_market_cap,
        "max_market_cap": fv.max_market_cap,
        "min_total_fee": fv.min_total_fee,
        "mc_fee_ratio": fv.mc_fee_ratio,
        "bundle_detection": fv.bundle_detection,
        "wash_trading": fv.wash_trading,
        "min_holders": fv.min_holders,
        "top_holder_balance": fv.top_holder_balance,
        "funded_wallet_age": fv.funded_wallet_age,
        "rug_probability": fv.rug_probability,
        "holder_distribution": fv.holder_distribution,
    }

    passed = 0
    failures = []
    total = len(filters)

    for name, data in filters.items():
        if data.get("passed", False):
            passed += 1
        else:
            failures.append(name)

    return passed, total, failures


def check_hard_gate(fv: FeatureVector) -> tuple[bool, list[str]]:
    """Hard gate: ALL filters must pass. Returns (passed, failures)."""
    _, _, failures = count_passed_filters(fv)
    return len(failures) == 0, failures
