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
