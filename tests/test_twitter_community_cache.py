"""Tests for TwitterClient.get_community_creator cache (June 2026 audit fix).

Covers:
- Cache hit skips Playwright browser launch entirely
- Cache miss invokes the browser launch path
- Cache entry expires after TTL (24h)

Note: The TwitterClient imports `async_playwright` INSIDE the function
(so missing Playwright doesn't break module load), so we patch
`playwright.async_api.async_playwright` rather than the twitter module.
"""
import asyncio
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, "/Users/khezuma/workspace/trenching")

from sources.twitter import TwitterClient


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_playwright_mock(handle_text="by @derpserk_ai"):
    """Build a mocked playwright chain that returns `handle_text` on inner_text."""
    class _FakeLocator:
        async def click(self):
            return None

    class _FakePage:
        def __init__(self, body_text):
            self._body_text = body_text

        async def goto(self, *args, **kwargs):
            return None

        async def wait_for_timeout(self, *args, **kwargs):
            return None

        async def inner_text(self, *args, **kwargs):
            return self._body_text

        def get_by_text(self, *args, **kwargs):
            return _FakeLocator()

    mock_browser = AsyncMock()
    mock_browser.close = AsyncMock()

    mock_page = _FakePage(f"Some header\n{handle_text}\nMore text")

    mock_ctx = AsyncMock()
    mock_ctx.new_page = AsyncMock(return_value=mock_page)

    # CRITICAL: make browser.new_context() return OUR mock_ctx (not the
    # auto-created AsyncMock return value), so that ctx.new_page() resolves
    # to our mock_page.
    mock_browser.new_context = AsyncMock(return_value=mock_ctx)

    mock_p_instance = MagicMock()
    mock_p_instance.chromium.launch = AsyncMock(return_value=mock_browser)
    mock_p_instance.__aenter__ = AsyncMock(return_value=mock_p_instance)
    mock_p_instance.__aexit__ = AsyncMock(return_value=None)

    def async_playwright_factory(*args, **kwargs):
        return mock_p_instance

    return async_playwright_factory


# --- 1. Cache hit skips browser launch -------------------------------------

def test_cache_hit_skips_browser_launch():
    """When community_id is in cache with valid TTL, get_community_creator
    returns the cached handle WITHOUT calling Playwright.
    """
    async def _run_test():
        client = TwitterClient()
        # Pre-populate cache
        client._community_cache["test_community_123"] = ("cached_handle", time.time())

        # Patch the import location of async_playwright. If reached, the
        # cache hit path was bypassed (BAD).
        mock_pw_module = MagicMock()
        mock_pw_module.async_playwright = MagicMock(
            side_effect=AssertionError("Playwright should NOT be called on cache hit")
        )
        with patch.dict("sys.modules", {"playwright": MagicMock(), "playwright.async_api": mock_pw_module}):
            result = await client.get_community_creator("test_community_123")
            assert result == "cached_handle"

    _run(_run_test())


# --- 2. Cache miss invokes Playwright (mocked) -----------------------------

def test_cache_miss_triggers_playwright():
    """On cache miss, get_community_creator should attempt the Playwright path.
    We mock Playwright so the test doesn't actually launch Chromium.
    """
    async def _run_test():
        client = TwitterClient()
        client._community_cache.clear()
        assert "fresh_community_456" not in client._community_cache

        async_playwright_fn = _make_playwright_mock(handle_text="by @derpserk_ai")

        mock_pw_module = MagicMock()
        mock_pw_module.async_playwright = async_playwright_fn
        with patch.dict("sys.modules", {"playwright": MagicMock(), "playwright.async_api": mock_pw_module}):
            result = await client.get_community_creator("fresh_community_456")

        assert result == "derpserk_ai"
        assert "fresh_community_456" in client._community_cache
        cached_handle, _ = client._community_cache["fresh_community_456"]
        assert cached_handle == "derpserk_ai"

    _run(_run_test())


# --- 3. Cache entry expires after TTL -------------------------------------

def test_cache_expires_after_ttl():
    """When cache entry is older than TTL, get_community_creator should
    bypass the cache and re-scrape.
    """
    async def _run_test():
        client = TwitterClient()
        # Pre-populate cache with an OLD entry (older than 24h)
        old_ts = time.time() - (25 * 3600)  # 25 hours ago
        client._community_cache["stale_community_789"] = ("old_handle", old_ts)

        # Mock Playwright to return a different handle (proves we re-scraped)
        async_playwright_fn = _make_playwright_mock(handle_text="by @new_handle")

        mock_pw_module = MagicMock()
        mock_pw_module.async_playwright = async_playwright_fn
        with patch.dict("sys.modules", {"playwright": MagicMock(), "playwright.async_api": mock_pw_module}):
            result = await client.get_community_creator("stale_community_789")

        # Should have re-scraped (old_handle ignored) and updated cache
        assert result == "new_handle"
        cached_handle, ts = client._community_cache["stale_community_789"]
        assert cached_handle == "new_handle"
        # ts should be recent (within last 5 seconds)
        assert (time.time() - ts) < 5.0

    _run(_run_test())
