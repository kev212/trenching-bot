"""Manual LLM test for willo2 (CgoBzH5qyF68Y8a77MvGdxY33JVt3UHBT1UbNrT6pump).

Reproduces the exact prompt that was sent at 2026-06-02 10:13:35 to verify
why LLM scored it 15 (not because of "scammed" word — that's the user's hypothesis).
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from llm.mimo_client import MiMoClient
from llm.prompts import SOCIAL_ANALYSIS_SYSTEM, SOCIAL_ANALYSIS_USER


async def main():
    # Token context (from bot logs at 10:13:22-10:13:35)
    # social_links=False means: no twitter_username, no website_url
    # has_community=True, community_id=2038714731796574514
    # organic=10 → 10 search results
    # influencers=0 → no influencer mentions
    # catalyst=True → LLM detected one (we don't know what)

    user_prompt = SOCIAL_ANALYSIS_USER.format(
        token_name="willo2",
        token_symbol="willo2",
        market_cap=73500,  # from LLM-FACTORS: "market cap in sweet spot ($73.5K)"
        age_description="unknown",  # unknown from logs (creation_timestamp likely 0)
        holders_count=515,  # from LLM-FACTORS
        twitter_username="none",  # handle='' (empty)
        twitter_followers=0,
        twitter_verified="No",
        twitter_description="No description",
        twitter_community="2038714731796574514",  # has community
        recent_tweets="No tweets from this account yet",
        website_text="No website content",
        # Search results: 10 organic mentions (we don't have actual content, but
        # we know they were collected via search_by_contract)
        search_results=json.dumps(
            [
                {"text": "[sample organic mention 1]", "author": {"screen_name": "user_a", "followers": 200}, "likes": 3},
                {"text": "[sample organic mention 2]", "author": {"screen_name": "user_b", "followers": 150}, "likes": 1},
                {"text": "[sample organic mention 3]", "author": {"screen_name": "user_c", "followers": 80}, "likes": 0},
                "[...7 more search results, no influencers in any...]",
            ],
            indent=2,
        ),
        influencer_mentions="No influencer mentions",
    )

    print("=" * 60)
    print("PROMPT SENT TO LLM:")
    print("=" * 60)
    print(f"SYSTEM:\n{SOCIAL_ANALYSIS_SYSTEM[:300]}...")
    print(f"\nUSER:\n{user_prompt}")
    print("\n" + "=" * 60)
    print("LLM RESPONSE:")
    print("=" * 60)

    client = MiMoClient()
    # Force production model (config.py default is stale)
    client.model = "mimo-v2.5-pro"
    result = await client.analyze_token(SOCIAL_ANALYSIS_SYSTEM, user_prompt)
    print(json.dumps(result, indent=2))

    # Reproduce the scoring math
    score = result.get("score", 0)
    catalyst = result.get("has_catalyst", False)
    signals_bonus = 0
    breakdown = {}
    if catalyst:
        signals_bonus += 8
        breakdown["catalyst"] = 8
    if result.get("influencers_found"):
        signals_bonus += 5
        breakdown["influencer"] = 5
    # organic_spread is from search result count, hard to reproduce exactly
    signals_bonus += 5
    breakdown["organic_spread"] = 5

    multiplier = 1.0 + (signals_bonus / 100)
    final = int(score * multiplier)

    print("\n" + "=" * 60)
    print("SCORING REPRODUCTION:")
    print("=" * 60)
    print(f"LLM score: {score}")
    print(f"Signals bonus: +{signals_bonus}")
    print(f"Multiplier: ×{multiplier:.2f}")
    print(f"Final social: {final}/100")


if __name__ == "__main__":
    asyncio.run(main())
