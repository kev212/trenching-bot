"""Manual test — calls real GMGN /v1/trenches with v2 body.

Run: source .env && .venv/bin/python tests/local_test_trenches.py
"""
import asyncio, json
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(".env").absolute())

import sys; sys.path.insert(0, str(Path(__file__).parent.parent))
from sources.gmgn import GMGNClient


async def test():
    import os
    key = os.environ.get("GMGN_API_KEY", "")
    client = GMGNClient(api_key=key)

    print("=== Trenches: near_completion (SM>=1) ===")
    items = await client.get_trenches(limit=5, min_smart_degen=1, category="near_completion")
    print(f"  count={len(items)}")
    for t in items[:5]:
        print(f"  {t.get('symbol','?'):15} mc=${t.get('market_cap',0):.0f} sm={t.get('smart_degen_count',0)} progress={t.get('progress',0)*100:.1f}% platform={t.get('launchpad_platform','')}")

    print("\n=== Trenches: new_creation ===")
    items = await client.get_trenches(limit=5, min_smart_degen=0, category="new_creation")
    print(f"  count={len(items)}")
    for t in items[:5]:
        print(f"  {t.get('symbol','?'):15} mc=${t.get('market_cap',0):.0f} sm={t.get('smart_degen_count',0)}")


if __name__ == "__main__":
    asyncio.run(test())
