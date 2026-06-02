"""Negative signal detection for token scoring.

Code-based check for scam/rug patterns that the LLM might miss or weight
too lightly. Returns a penalty 0-30 that lowers the social multiplier.

Design:
- Pattern-based: cheap, predictable, no API calls
- Conservative: only flags when 2+ signals co-occur (false-positive guard)
- Token-level + tweet-level: catches both name patterns and discussion
"""
import logging
import re
from typing import Optional

logger = logging.getLogger("trench_signals")


# Scam / rug keywords (case-insensitive substring match)
SCAM_KEYWORDS = [
    "guaranteed profit", "guaranteed return", "100x guaranteed",
    "insider info", "insider tip", "rug pull incoming",
    "honeypot", "is a honeypot", "is a scam",
    "send sol to", "airdrop claim", "claim airdrop at",
    "wallet drainer", "drainer site", "drainer link",
    "pump signal", "pump group", "pump now",
    "wen lambo", "easy 10x", "free money",
    "click here to claim", "verify wallet to claim",
    "trust me bro", "not a rug", "rug",
]

# Wash-trading markers
WASH_PATTERNS = [
    "buy and sell repeatedly", "circular trading",
    "self-trade", "self trade", "wash trade",
]

# Suspicious naming patterns (regex)
# These are high-specificity markers of impersonation / clone attempts.
# Avoid broad suffixes like "in/on/ai" — too many false positives (e.g., "moon" ends in "on").
SUSPICIOUS_NAME_PATTERNS = [
    re.compile(r".*[oO]fficial$", re.IGNORECASE),  # "trumpofficial"
    re.compile(r".*[rR]eal$", re.IGNORECASE),  # "elonreal"
    re.compile(r".*[vV]2$|.*[vV]3$|.*[vV]4$", re.IGNORECASE),  # "dogeV2"
]


def _tweet_texts(token) -> list[str]:
    """Collect all tweet texts (organic + recent + website)."""
    texts = []
    for m in (getattr(token, "organic_mentions", None) or []):
        text = m.get("tweet_text", "")
        if text:
            texts.append(text.lower())
    for t in (getattr(token, "recent_tweets", None) or []):
        if isinstance(t, dict):
            text = t.get("text", "")
            if text:
                texts.append(text.lower())
        elif isinstance(t, str):
            texts.append(t.lower())
    if getattr(token, "website_text", None):
        texts.append(token.website_text.lower()[:1000])
    if getattr(token, "twitter_description", None):
        texts.append(token.twitter_description.lower()[:500])
    return texts


def _has_keyword_match(texts: list[str], keywords: list[str], min_count: int = 2) -> bool:
    """Check if min_count of keywords appear in any single text (co-occurrence guard)."""
    for text in texts:
        hits = sum(1 for kw in keywords if kw in text)
        if hits >= min_count:
            return True
    return False


def _has_keyword_anywhere(texts: list[str], keywords: list[str], min_total: int = 3) -> bool:
    """Check if min_total keyword hits across all texts (volume-based)."""
    total = sum(1 for text in texts for kw in keywords if kw in text)
    return total >= min_total


def _suspicious_name(name: str, symbol: str) -> bool:
    """Check if token name/symbol looks like an impersonation attempt."""
    for pattern in SUSPICIOUS_NAME_PATTERNS:
        if pattern.search(name or "") or pattern.search(symbol or ""):
            return True
    return False


def _bot_pattern(token) -> bool:
    """Detect bot patterns: same handle posting 3+ tweets in <5 min."""
    mentions = getattr(token, "organic_mentions", None) or []
    if len(mentions) < 3:
        return False
    handle_ages: dict[str, list[float]] = {}
    for m in mentions:
        handle = m.get("handle", "")
        age = m.get("tweet_age_min", 0)
        if handle:
            handle_ages.setdefault(handle, []).append(age)
    for ages in handle_ages.values():
        if len(ages) >= 3 and (max(ages) - min(ages)) < 5:
            return True
    return False


def detect_negative_signals(token) -> int:
    """Compute a penalty 0-30 from negative signals in token data.

    Conservative: requires 2+ co-occurring keyword hits in same text,
    OR 3+ keyword hits across all texts.

    Args:
        token: TokenData with social fields populated.

    Returns:
        int 0-30. 0 = no negative signals. 30 = max penalty.
    """
    penalty = 0
    signals_fired: list[str] = []

    texts = _tweet_texts(token)

    if not texts and not (token.name or token.symbol):
        return 0

    name = getattr(token, "name", "") or ""
    symbol = getattr(token, "symbol", "") or ""

    # Signal 1: Scam keywords (co-occurrence in same text, conservative)
    if texts and _has_keyword_match(texts, SCAM_KEYWORDS, min_count=2):
        penalty += 15
        signals_fired.append("scam_keywords_cooccurrence")
    elif texts and _has_keyword_anywhere(texts, SCAM_KEYWORDS, min_total=3):
        penalty += 10
        signals_fired.append("scam_keywords_volume")

    # Signal 2: Wash-trading markers
    if texts and _has_keyword_match(texts, WASH_PATTERNS, min_count=1):
        penalty += 5
        signals_fired.append("wash_trading")

    # Signal 3: Suspicious name (impersonation attempt)
    if _suspicious_name(name, symbol):
        penalty += 10
        signals_fired.append("suspicious_name")

    # Signal 4: Bot pattern (same handle 3+ tweets in <5 min)
    if _bot_pattern(token):
        penalty += 5
        signals_fired.append("bot_pattern")

    # Cap at 30
    penalty = min(30, penalty)

    if penalty > 0:
        logger.info(
            f"[TRENCH-SIGNALS] {symbol} ({name[:20]}): "
            f"penalty={penalty}, fired={signals_fired}"
        )

    return penalty
