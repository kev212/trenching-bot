import logging
import re
from curl_cffi.requests import AsyncSession

logger = logging.getLogger("main")

BASE_URL = "https://api.fxtwitter.com"


class TwitterClient:
    def __init__(self):
        self.host = BASE_URL

    INVALID_PATHS = {"i", "status", "statuses", "search", "home", "notifications", "messages", "explore", "settings", "lists", "communities"}

    def _extract_handle(self, raw: str) -> str:
        """Extract clean Twitter handle from URL or raw string."""
        if not raw:
            return ""
        handle = raw.lstrip("@")
        
        # Full URL: https://x.com/username or https://twitter.com/username
        match = re.search(r"(?:twitter\.com|x\.com)/([A-Za-z0-9_]+)(?:/|$)", handle)
        if match:
            candidate = match.group(1)
            # Skip invalid paths: x.com/i/..., x.com/search, etc.
            if candidate.lower() in self.INVALID_PATHS:
                return ""
            return candidate
        
        # Partial path: username/status/... or i/communities/...
        match = re.match(r"^([A-Za-z0-9_]+)(?:/|$)", handle)
        if match:
            candidate = match.group(1)
            if candidate.lower() in self.INVALID_PATHS:
                return ""
            return candidate
        
        return ""

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
        clean = self._extract_handle(handle)
        if not clean:
            return {}
        data = await self._get(f"/2/profile/{clean}")
        return data.get("user", {})

    async def get_recent_tweets(self, handle: str, count: int = 3) -> list:
        clean = self._extract_handle(handle)
        if not clean:
            return []
        data = await self._get(
            f"/2/profile/{clean}/statuses",
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
