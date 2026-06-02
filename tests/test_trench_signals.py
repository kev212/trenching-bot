"""Tests for negative signal detection (Stage 3)."""
import sys
sys.path.insert(0, "/Users/khezuma/workspace/trenching")

from analysis.models import TokenData
from core.trench_signals import detect_negative_signals


def make_token(**kwargs) -> TokenData:
    t = TokenData(
        address=kwargs.get("address", "test"),
        name=kwargs.get("name", "Test"),
        symbol=kwargs.get("symbol", "TST"),
    )
    t.organic_mentions = kwargs.get("organic_mentions", [])
    t.recent_tweets = kwargs.get("recent_tweets", [])
    t.website_text = kwargs.get("website_text", "")
    t.twitter_description = kwargs.get("twitter_description", "")
    return t


def test_clean_token_no_penalty():
    """Clean token with normal tweets → 0 penalty."""
    token = make_token(
        name="DogMoon",
        symbol="DMOON",
        organic_mentions=[
            {"handle": "user1", "tweet_text": "just bought $dmoon mooning soon", "tweet_age_min": 30},
            {"handle": "user2", "tweet_text": "$dmoon looking strong", "tweet_age_min": 45},
        ],
    )
    penalty = detect_negative_signals(token)
    assert penalty == 0, f"Expected 0, got {penalty}"
    print(f"✓ clean_token: penalty=0 (expected 0)")


def test_scam_keywords_cooccurrence():
    """2+ scam keywords in same text → +15."""
    token = make_token(
        name="MoonShot",
        symbol="MOON",
        organic_mentions=[
            {"handle": "shill1", "tweet_text": "guaranteed profit, insider info, easy 10x!", "tweet_age_min": 30},
        ],
    )
    penalty = detect_negative_signals(token)
    assert penalty >= 15, f"Expected >=15, got {penalty}"
    print(f"✓ scam_cooccurrence: penalty={penalty} (expected >=15)")


def test_scam_keywords_volume():
    """3+ scam keywords across different texts → +10."""
    token = make_token(
        name="MemeCoin",
        symbol="MEME",
        organic_mentions=[
            {"handle": "user1", "tweet_text": "guaranteed profit", "tweet_age_min": 30},
            {"handle": "user2", "tweet_text": "easy 10x", "tweet_age_min": 30},
            {"handle": "user3", "tweet_text": "wen lambo", "tweet_age_min": 30},
        ],
    )
    penalty = detect_negative_signals(token)
    assert penalty >= 10, f"Expected >=10, got {penalty}"
    print(f"✓ scam_volume: penalty={penalty} (expected >=10)")


def test_suspicious_name():
    """Suspicious name (e.g., 'trumpofficial' suffix) → +10."""
    token = make_token(
        name="TrumpOfficial",
        symbol="TRUMPOFFICIAL",
        organic_mentions=[
            {"handle": "user1", "tweet_text": "buying trumpofficial", "tweet_age_min": 30},
        ],
    )
    penalty = detect_negative_signals(token)
    assert penalty >= 10, f"Expected >=10, got {penalty}"
    print(f"✓ suspicious_name: penalty={penalty} (expected >=10)")


def test_all_caps_name():
    """All caps name no longer flags (suffix patterns handle impersonation)."""
    token = make_token(
        name="ELONMUSK",
        symbol="ELONMUSK",
        organic_mentions=[],
    )
    penalty = detect_negative_signals(token)
    # No suffix pattern (official/real/v2/in/on/ai) → 0 penalty
    assert penalty == 0, f"Expected 0, got {penalty}"
    print(f"✓ all_caps_name: penalty=0 (suffix patterns handle impersonation)")


def test_bot_pattern():
    """Same handle 3+ tweets in <5 min → +5."""
    token = make_token(
        name="BotPromo",
        symbol="BOTS",
        organic_mentions=[
            {"handle": "promobot", "tweet_text": "buy now", "tweet_age_min": 1},
            {"handle": "promobot", "tweet_text": "still buying", "tweet_age_min": 2},
            {"handle": "promobot", "tweet_text": "last chance", "tweet_age_min": 3},
        ],
    )
    penalty = detect_negative_signals(token)
    assert penalty >= 5, f"Expected >=5, got {penalty}"
    print(f"✓ bot_pattern: penalty={penalty} (expected >=5)")


def test_penalty_capped_at_30():
    """Max penalty capped at 30."""
    token = make_token(
        name="ScamInu",
        symbol="SCAMINU",
        organic_mentions=[
            {"handle": "shill", "tweet_text": "guaranteed profit insider info easy 10x wen lambo free money", "tweet_age_min": 1},
        ] + [
            {"handle": "shill", "tweet_text": f"tweet {i}", "tweet_age_min": i}
            for i in range(2, 5)
        ],
    )
    penalty = detect_negative_signals(token)
    assert penalty <= 30, f"Expected <=30, got {penalty}"
    print(f"✓ penalty_capped: penalty={penalty} (expected <=30)")


def test_combined_signals():
    """Multiple signals combine: scam + suspicious_name + bot = max 30."""
    token = make_token(
        name="DogeOfficial",  # matches "official" suffix
        symbol="DOGEOFFICIAL",
        organic_mentions=[
            {"handle": "shill", "tweet_text": "guaranteed profit insider info pump now", "tweet_age_min": 1},
            {"handle": "shill", "tweet_text": "wen lambo", "tweet_age_min": 2},
            {"handle": "shill", "tweet_text": "easy 10x", "tweet_age_min": 3},
        ],
    )
    penalty = detect_negative_signals(token)
    # scam_cooccurrence (15) + official suffix (10) + bot (5) = 30 capped
    assert penalty == 30, f"Expected 30, got {penalty}"
    print(f"✓ combined_signals: penalty={penalty} (expected 30)")


def test_empty_token():
    """Empty token → 0 penalty (no false positives)."""
    token = make_token()
    penalty = detect_negative_signals(token)
    assert penalty == 0, f"Expected 0, got {penalty}"
    print(f"✓ empty_token: penalty=0 (expected 0)")


if __name__ == "__main__":
    test_clean_token_no_penalty()
    test_scam_keywords_cooccurrence()
    test_scam_keywords_volume()
    test_suspicious_name()
    test_all_caps_name()
    test_bot_pattern()
    test_penalty_capped_at_30()
    test_combined_signals()
    test_empty_token()
    print("\n✅ All negative signal tests passed!")
