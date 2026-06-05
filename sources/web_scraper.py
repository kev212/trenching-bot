import asyncio
import logging
import re
from typing import Optional
from curl_cffi.requests import AsyncSession

logger = logging.getLogger("main")


class WebScraper:
    def __init__(self):
        self._session: Optional[AsyncSession] = None

    async def start(self):
        """Eagerly initialize HTTP session. Call once at bot startup."""
        if self._session is None:
            self._session = AsyncSession(impersonate="chrome")
            logger.info("WebScraper: session initialized")

    async def _get_session(self) -> AsyncSession:
        if self._session is None:
            self._session = AsyncSession(impersonate="chrome")
        return self._session

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None

    async def scrape_text(self, url: str, max_length: int = 2000) -> str:
        try:
            session = await self._get_session()
            resp = await asyncio.wait_for(
                session.get(url, timeout=5, allow_redirects=True),
                timeout=10,
            )
            if resp.status_code != 200:
                logger.warning(f"Website scrape failed: HTTP {resp.status_code}")
                return ""

            html = resp.text
            text = self._extract_text(html)
            return text[:max_length]

        except asyncio.TimeoutError:
            logger.error(f"Website scrape timeout for {url}")
            return ""
        except Exception as e:
            logger.error(f"Website scrape error for {url}: {e}")
            return ""

    def _extract_text(self, html: str) -> str:
        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r"[^\w\s.,;:!?-]", "", text)
        return text.strip()
