import logging
import re
from curl_cffi.requests import AsyncSession

logger = logging.getLogger("main")

BASE_URL = "https://api.fxtwitter.com"


class TwitterClient:
    def __init__(self):
        self.host = BASE_URL

    INVALID_PATHS = {"i", "status", "statuses", "search", "home", "notifications", "messages", "explore", "settings", "lists"}

    def parse_twitter_input(self, raw: str) -> dict:
        """Parse raw Twitter input into structured data.
        Returns: {handle, tweet_id, community_id, raw_type}
        """
        if not raw:
            return {"handle": "", "tweet_id": "", "community_id": "", "raw_type": "empty"}

        handle = raw.lstrip("@")

        # Full URL: https://x.com/username/status/123456
        match = re.search(r"(?:twitter\.com|x\.com)/([A-Za-z0-9_]+)/status/(\d+)", handle)
        if match:
            candidate = match.group(1)
            if candidate.lower() not in self.INVALID_PATHS:
                return {"handle": candidate, "tweet_id": match.group(2), "community_id": "", "raw_type": "tweet_url"}
            # x.com/i/status/123456 → no handle, but we have tweet ID
            return {"handle": "", "tweet_id": match.group(2), "community_id": "", "raw_type": "tweet_url"}

        # Full URL: https://x.com/i/communities/123456
        match = re.search(r"(?:twitter\.com|x\.com)/i/communities/(\d+)", handle)
        if match:
            return {"handle": "", "tweet_id": "", "community_id": match.group(1), "raw_type": "community_url"}

        # Full URL: https://x.com/username
        match = re.search(r"(?:twitter\.com|x\.com)/([A-Za-z0-9_]+)(?:/|$)", handle)
        if match:
            candidate = match.group(1)
            if candidate.lower() not in self.INVALID_PATHS:
                return {"handle": candidate, "tweet_id": "", "community_id": "", "raw_type": "profile_url"}
            return {"handle": "", "tweet_id": "", "community_id": "", "raw_type": "invalid"}

        # Partial path: username/status/123456
        match = re.match(r"^([A-Za-z0-9_]+)/status/(\d+)", handle)
        if match:
            candidate = match.group(1)
            if candidate.lower() not in self.INVALID_PATHS:
                return {"handle": candidate, "tweet_id": match.group(2), "community_id": "", "raw_type": "tweet_path"}
            return {"handle": "", "tweet_id": match.group(2), "community_id": "", "raw_type": "tweet_path"}

        # Partial path: i/communities/123456
        match = re.match(r"^i/communities/(\d+)", handle)
        if match:
            return {"handle": "", "tweet_id": "", "community_id": match.group(1), "raw_type": "community_path"}

        # Plain handle
        match = re.match(r"^([A-Za-z0-9_]+)$", handle)
        if match:
            candidate = match.group(1)
            if candidate.lower() not in self.INVALID_PATHS:
                return {"handle": candidate, "tweet_id": "", "community_id": "", "raw_type": "handle"}

        return {"handle": "", "tweet_id": "", "community_id": "", "raw_type": "unknown"}

    async def _get(self, path: str, params: dict = None) -> dict:
        try:
            async with AsyncSession(impersonate="chrome") as session:
                resp = await session.get(
                    f"{self.host}{path}",
                    params=params or {},
                    timeout=10,
                )
                if resp.status_code != 200:
                    logger.warning(f"FxTwitter {path}: HTTP {resp.status_code}")
                    return {}
                data = resp.json()
                if data.get("code") != 200:
                    logger.warning(f"FxTwitter {path}: {data.get('message')}")
                    return {}
                return data
        except Exception as e:
            logger.error(f"FxTwitter {path} error: {e}")
            return {}

    async def get_profile(self, handle: str) -> dict:
        data = await self._get(f"/2/profile/{handle}")
        return data.get("user", {})

    async def get_recent_tweets(self, handle: str, count: int = 3) -> list:
        data = await self._get(
            f"/2/profile/{handle}/statuses",
            {"count": count},
        )
        return data.get("results", [])

    async def get_tweet(self, tweet_id: str) -> dict:
        data = await self._get(f"/2/status/{tweet_id}")
        return data.get("status", {})

    async def search_by_contract(self, address: str, count: int = 10) -> list:
        data = await self._get(
            "/2/search",
            {"q": address, "feed": "latest", "count": count},
        )
        return data.get("results", [])

    async def get_community_creator(self, community_id: str) -> str:
        """Scrape community page via Playwright to extract creator handle.

        Returns handle (e.g. 'derpserk_ai') or empty string on failure.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.warning("Playwright not installed, cannot scrape community creator")
            return ""

        browser = None
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                ctx = await browser.new_context(
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/120.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 720},
                )
                page = await ctx.new_page()
                url = f"https://x.com/i/communities/{community_id}"
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(5000)

                # Click About tab
                about = page.get_by_text("About", exact=True)
                await about.click()
                await page.wait_for_timeout(3000)

                text = await page.inner_text("body")

                # Extract creator from "Created ... by @handle"
                match = re.search(r"by\s+@(\w+)", text)
                if match:
                    handle = match.group(1)
                    logger.info(f"[COMMUNITY] {community_id}: creator=@{handle}")
                    return handle

                logger.warning(f"[COMMUNITY] {community_id}: no creator found in About tab")
                return ""

        except Exception as e:
            logger.error(f"[COMMUNITY] {community_id}: scrape failed: {e}")
            return ""
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
