"""Local test for GMGN API /v1/trenches endpoint.

Calls get_trenches() directly and prints the raw response.
Run: .venv/bin/python tests/local_test_trenches.py
"""
import asyncio
import json
import os
import sys
from pathlib import Path

# Load .env
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

sys.path.insert(0, str(Path(__file__).parent.parent))

from sources.gmgn import GMGNClient

async def main():
    api_key = os.environ.get("GMGN_API_KEY")
    proxy = os.environ.get("HTTP_PROXY", "")
    print(f"API key: {api_key[:20]}...")
    print(f"Proxy:   {proxy or '(none)'}")
    print()

    client = GMGNClient(api_key, proxy)

    print("=" * 60)
    print("Test 1: get_trenches() with current params (no types/platforms)")
    print("=" * 60)
    tokens = await client.get_trenches(limit=20)
    print(f"Result: {len(tokens)} tokens")
    if tokens:
        print(f"First: {json.dumps(tokens[0], indent=2, default=str)[:800]}")
    print()

    print("=" * 60)
    print("Test 2: get_trending() (works — sanity check)")
    print("=" * 60)
    trending = await client.get_trending(limit=5)
    print(f"Result: {len(trending)} tokens")
    if trending:
        print(f"First addr: {trending[0].get('address', trending[0].get('token_address', 'N/A'))}")
    print()

    print("=" * 60)
    print("Test 3: Direct _post() to /v1/trenches (raw response)")
    print("=" * 60)
    data = await client._post("/v1/trenches", {"chain": "sol"}, {"limit": 20})
    print(f"Type: {type(data).__name__}")
    print(f"Keys: {list(data.keys()) if isinstance(data, dict) else 'N/A'}")
    print(f"Full: {json.dumps(data, default=str)[:1000]}")

asyncio.run(main())
