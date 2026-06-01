"""Test social scoring bonus calculations against predicted scenarios."""
import sys
sys.path.insert(0, "/Users/khezuma/workspace/trenching")

from analysis.models import TokenData
from llm.social_scoring import calculate_social_signals_bonus, load_social_scoring_config


def make_token(**kwargs) -> TokenData:
    t = TokenData(
        address=kwargs.get("address", "test"),
        name=kwargs.get("name", "Test"),
        symbol=kwargs.get("symbol", "TST"),
    )
    t.influencer_mentions = kwargs.get("influencer_mentions", [])
    t.organic_mentions = kwargs.get("organic_mentions", [])
    t.catalyst_match = kwargs.get("catalyst_match", False)
    t.catalyst_description = kwargs.get("catalyst_description", "")
    return t


def test_no_social():
    token = make_token()
    result = calculate_social_signals_bonus(token)
    assert result["total_bonus"] == 0, f"Expected 0, got {result['total_bonus']}"
    print(f"✓ no_social: bonus=0 (expected 0)")


def test_elon_only():
    """1 Elon tweet → +12 (mega cap)."""
    token = make_token(influencer_mentions=[
        {"handle": "elonmusk", "weight": 30, "tweet_age_min": 30},
    ])
    result = calculate_social_signals_bonus(token)
    assert result["total_bonus"] == 12, f"Expected 12, got {result['total_bonus']}"
    print(f"✓ elon_only: bonus=12 (expected 12)")


def test_toly_only():
    """1 Toly tweet → +12 (mega cap)."""
    token = make_token(influencer_mentions=[
        {"handle": "aeyakovenko", "weight": 20, "tweet_age_min": 30},
    ])
    result = calculate_social_signals_bonus(token)
    assert result["total_bonus"] == 12, f"Expected 12, got {result['total_bonus']}"
    print(f"✓ toly_only: bonus=12 (expected 12)")


def test_vitalik_only():
    """1 Vitalik tweet → +8 (major cap)."""
    token = make_token(influencer_mentions=[
        {"handle": "VitalikButerin", "weight": 12, "tweet_age_min": 30},
    ])
    result = calculate_social_signals_bonus(token)
    assert result["total_bonus"] == 8, f"Expected 8, got {result['total_bonus']}"
    print(f"✓ vitalik_only: bonus=8 (expected 8)")


def test_elon_plus_toly():
    """1 Elon + 1 Toly → base 12 + 12*0.3*1 = 15.6 → 15."""
    token = make_token(influencer_mentions=[
        {"handle": "elonmusk", "weight": 30, "tweet_age_min": 30},
        {"handle": "aeyakovenko", "weight": 20, "tweet_age_min": 30},
    ])
    result = calculate_social_signals_bonus(token)
    assert result["total_bonus"] == 15, f"Expected 15, got {result['total_bonus']}"
    print(f"✓ elon_plus_toly: bonus=15 (expected 15)")


def test_ansem_only():
    """1 Ansem → +6 (mid cap)."""
    token = make_token(influencer_mentions=[
        {"handle": "blknoiz01", "weight": 6, "tweet_age_min": 30},
    ])
    result = calculate_social_signals_bonus(token)
    assert result["total_bonus"] == 6, f"Expected 6, got {result['total_bonus']}"
    print(f"✓ ansem_only: bonus=6 (expected 6)")


def test_seven_organic_basic():
    """7 small organic accounts → +10 (organic_spread)."""
    token = make_token(organic_mentions=[
        {"handle": f"user{i}", "likes": 5, "tweet_age_min": 30} for i in range(7)
    ])
    result = calculate_social_signals_bonus(token)
    assert result["total_bonus"] == 10, f"Expected 10, got {result['total_bonus']}"
    print(f"✓ seven_organic_basic: bonus=10 (expected 10)")


def test_seven_organic_with_engagement():
    """7 organic, 3 with 20+ likes, 1 with 500+ likes → +10 + 5 (tier1) + 5 (tier3) = 20."""
    mentions = []
    for i in range(3):
        mentions.append({"handle": f"user{i}", "likes": 25, "tweet_age_min": 30})
    for i in range(3, 7):
        mentions.append({"handle": f"user{i}", "likes": 5, "tweet_age_min": 30})
    mentions.append({"handle": "viral", "likes": 600, "tweet_age_min": 30})
    token = make_token(organic_mentions=mentions)
    result = calculate_social_signals_bonus(token)
    # organic_spread: 7+ authors = 10
    # tier1: 3 tweets with 20+ likes >= 2 = +5
    # tier3: 1 tweet with 500+ likes = +5
    # total: 20
    assert result["total_bonus"] == 20, f"Expected 20, got {result['total_bonus']}, breakdown={result['breakdown']}"
    print(f"✓ seven_organic_with_engagement: bonus=20 (expected 20)")


def test_elon_plus_seven_organic():
    """1 Elon (cap 12) + 7 organic (+10) + tier1 2+ with 20+ likes (+5) = 27."""
    mentions = [
        {"handle": f"user{i}", "likes": 25, "tweet_age_min": 30} for i in range(2)
    ] + [
        {"handle": f"user{i+2}", "likes": 5, "tweet_age_min": 30} for i in range(5)
    ]
    token = make_token(
        influencer_mentions=[{"handle": "elonmusk", "weight": 30, "tweet_age_min": 30}],
        organic_mentions=mentions,
    )
    result = calculate_social_signals_bonus(token)
    # influencer: 12, organic_spread: 10, engagement: 5
    # total: 27
    assert result["total_bonus"] == 27, f"Expected 27, got {result['total_bonus']}, breakdown={result['breakdown']}"
    print(f"✓ elon_plus_seven_organic: bonus=27 (expected 27)")


def test_elon_plus_catalyst():
    """1 Elon (12) + catalyst (+8) = 20."""
    token = make_token(
        influencer_mentions=[{"handle": "elonmusk", "weight": 30, "tweet_age_min": 30}],
        catalyst_match=True,
        catalyst_description="Trump announcement",
    )
    result = calculate_social_signals_bonus(token)
    assert result["total_bonus"] == 20, f"Expected 20, got {result['total_bonus']}, breakdown={result['breakdown']}"
    print(f"✓ elon_plus_catalyst: bonus=20 (expected 20)")


def test_time_decay_aging():
    """Elon tweet 12h old → 0.5 decay → 12*0.5=6."""
    token = make_token(influencer_mentions=[
        {"handle": "elonmusk", "weight": 30, "tweet_age_min": 720},  # 12h
    ])
    result = calculate_social_signals_bonus(token)
    # 30 * 0.5 = 15 effective, capped at 12 = 12
    assert result["total_bonus"] == 12, f"Expected 12, got {result['total_bonus']}, breakdown={result['breakdown']}"
    print(f"✓ time_decay_aging: bonus=12 (capped, expected 12)")


def test_time_decay_stale():
    """Elon tweet 30h old → 0.25 decay → 30*0.25=7.5, capped at 12 → 7."""
    token = make_token(influencer_mentions=[
        {"handle": "elonmusk", "weight": 30, "tweet_age_min": 1800},  # 30h
    ])
    result = calculate_social_signals_bonus(token)
    # 30 * 0.25 = 7.5 → 7
    assert result["total_bonus"] == 7, f"Expected 7, got {result['total_bonus']}, breakdown={result['breakdown']}"
    print(f"✓ time_decay_stale: bonus=7 (expected 7)")


def test_max_cap():
    """Pathological case: many influencers, organic, catalyst → should cap at 40."""
    token = make_token(
        influencer_mentions=[
            {"handle": f"inf{i}", "weight": 30, "tweet_age_min": 30} for i in range(5)
        ] + [
            {"handle": "ansem", "weight": 6, "tweet_age_min": 30},
        ],
        organic_mentions=[
            {"handle": f"user{i}", "likes": 600, "tweet_age_min": 30} for i in range(15)
        ],
        catalyst_match=True,
    )
    result = calculate_social_signals_bonus(token)
    assert result["total_bonus"] == 40, f"Expected 40 (capped), got {result['total_bonus']}, breakdown={result['breakdown']}"
    print(f"✓ max_cap: bonus=40 (capped, expected 40)")


def test_rabbi_scenario():
    """@RabbiDeploys-style: 254 followers, 39 likes, no influencer mention.
    This is organic only.
    - organic_spread: 1 author < 3 = 0
    - engagement: tier1 needs 2+ tweets with 20+ likes, 1 tweet doesn't qualify = 0
    - catalyst: false = 0
    Total: 0
    """
    token = make_token(organic_mentions=[
        {"handle": "RabbiDeploys", "followers": 254, "likes": 39, "tweet_age_min": 60},
    ])
    result = calculate_social_signals_bonus(token)
    # This case is an organic edge: 1 tweet, 39 likes, 1 author → all 0
    # We'd need to lower tier1 threshold or add solo-engagement bonus
    print(f"⚠️  rabbi_scenario: bonus={result['total_bonus']} (expected 0 currently, but should ideally be +5)")
    print(f"   breakdown={result['breakdown']}")
    # Note: this is the edge case the user wants to fix via tier1_likes=20 lower threshold
    # With tier1_likes=20, 39 likes qualifies, but we still need 2+ tweets


def test_rabbi_with_2_organic():
    """2 organic accounts with 20+ likes → tier1 +5 (but no organic_spread since <3 authors)."""
    token = make_token(organic_mentions=[
        {"handle": "RabbiDeploys", "followers": 254, "likes": 39, "tweet_age_min": 60},
        {"handle": "CryptoKate", "followers": 1200, "likes": 25, "tweet_age_min": 30},
    ])
    result = calculate_social_signals_bonus(token)
    # 2 tweets with 20+ likes → tier1 +5
    # 2 authors < 3 → organic_spread 0
    # total: 5
    assert result["total_bonus"] == 5, f"Expected 5, got {result['total_bonus']}, breakdown={result['breakdown']}"
    print(f"✓ rabbi_with_2_organic: bonus=5 (expected 5)")


if __name__ == "__main__":
    print("Loading config:", load_social_scoring_config())
    print()
    test_no_social()
    test_elon_only()
    test_toly_only()
    test_vitalik_only()
    test_elon_plus_toly()
    test_ansem_only()
    test_seven_organic_basic()
    test_seven_organic_with_engagement()
    test_elon_plus_seven_organic()
    test_elon_plus_catalyst()
    test_time_decay_aging()
    test_time_decay_stale()
    test_max_cap()
    test_rabbi_scenario()
    test_rabbi_with_2_organic()
    print("\n✅ All tests passed!")
