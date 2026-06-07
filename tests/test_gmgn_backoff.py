"""Tests for GMGN backoff (429/403/Cloudflare)."""
import asyncio
import sys
import time
from unittest.mock import MagicMock, AsyncMock

sys.path.insert(0, "/Users/khezuma/workspace/trenching")

from sources.gmgn import GMGNClient, GMGN_BACKOFF_429_S, GMGN_BACKOFF_403_S, GMGN_BACKOFF_CLOUDFLARE_S


class FakeResp:
    def __init__(self, status_code, text="", headers=None, json_data=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self._json = json_data or {}

    def json(self):
        return self._json


class FakeSession:
    """Mock curl_cffi AsyncSession."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.call_count = 0

    async def get(self, *a, **k):
        self.call_count += 1
        return self._responses.pop(0)

    async def post(self, *a, **k):
        self.call_count += 1
        return self._responses.pop(0)

    async def close(self):
        pass


def make_client(responses):
    c = GMGNClient(api_key="test", proxy="")
    fake = FakeSession(responses)
    c._session = fake
    return c, fake


def test_gmgn_429_sets_backoff_30s():
    """429 → 30s backoff (no Retry-After header)."""
    async def go():
        client, fake = make_client([FakeResp(429, "rate limited", headers={})])
        result = await client._get("/v1/token/info", {"chain": "sol", "address": "X"})
        assert result == {}
        assert client._backoff_active(), "backoff should be active"
        remaining = client._backoff_until - time.time()
        assert 25 < remaining <= 31, f"expected ~30s, got {remaining}"
        assert "429" in client._backoff_reason
    asyncio.run(go())


def test_gmgn_429_with_retry_after_header():
    """429 with Retry-After: 60 → 60s backoff."""
    async def go():
        client, fake = make_client([FakeResp(429, "rate limited", headers={"retry-after": "60"})])
        await client._get("/v1/token/info", {"chain": "sol", "address": "X"})
        assert client._backoff_active()
        remaining = client._backoff_until - time.time()
        assert 55 < remaining <= 61, f"expected ~60s, got {remaining}"
    asyncio.run(go())


def test_gmgn_403_sets_10min_backoff():
    async def go():
        client, fake = make_client([FakeResp(403, "forbidden")])
        await client._get("/v1/token/info", {"chain": "sol", "address": "X"})
        assert client._backoff_active()
        remaining = client._backoff_until - time.time()
        assert 595 < remaining <= 601, f"expected ~600s (10 min), got {remaining}"
        assert "10 min" in client._backoff_reason
    asyncio.run(go())


def test_gmgn_cloudflare_body_sets_30min_backoff():
    async def go():
        body = "<!DOCTYPE html><html><head><title>Just a moment...</title>...cf_chl_opt..."
        client, fake = make_client([FakeResp(403, body)])
        await client._get("/v1/token/info", {"chain": "sol", "address": "X"})
        assert client._backoff_active()
        remaining = client._backoff_until - time.time()
        assert 1795 < remaining <= 1801, f"expected ~1800s (30 min), got {remaining}"
        assert "cloudflare" in client._backoff_reason
    asyncio.run(go())


def test_gmgn_backoff_blocks_subsequent_calls():
    """After backoff set, _get should return {} without making HTTP call."""
    async def go():
        client, fake = make_client([
            FakeResp(429, "rate limited", headers={}),  # 1st: sets backoff
            FakeResp(200, "{}", json_data={"code": 0, "data": {"result": 1}}),  # would succeed
        ])
        # 1st call: 429 → backoff
        r1 = await client._get("/v1/token/info", {"chain": "sol", "address": "X"})
        assert r1 == {}
        assert client._backoff_active()
        # 2nd call: backoff active, should not hit HTTP
        r2 = await client._get("/v1/token/info", {"chain": "sol", "address": "Y"})
        assert r2 == {}
        assert fake.call_count == 1, f"backoff should block 2nd call, got {fake.call_count}"
    asyncio.run(go())


def test_gmgn_500_does_not_set_backoff():
    """500 is server error, not rate limit. Should NOT set backoff."""
    async def go():
        client, fake = make_client([FakeResp(500, "server error")])
        await client._get("/v1/token/info", {"chain": "sol", "address": "X"})
        assert not client._backoff_active(), "500 should not trigger backoff"
    asyncio.run(go())


def test_gmgn_post_also_sets_backoff():
    async def go():
        client, fake = make_client([FakeResp(429, "rate limited", headers={"retry-after": "45"})])
        await client._post("/v1/trenches", {"chain": "sol"}, {"version": "v2"})
        assert client._backoff_active()
        remaining = client._backoff_until - time.time()
        assert 40 < remaining <= 46, f"expected ~45s, got {remaining}"
    asyncio.run(go())
