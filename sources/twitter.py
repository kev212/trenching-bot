import logging
from curl_cffi.requests import AsyncSession

logger = logging.getLogger("main")

BASE_URL = "https://api.fxtwitter.com"


class TwitterClient:
    def __init__(self):
        self.host = BASE_URL

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
