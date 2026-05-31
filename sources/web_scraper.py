import logging
import re
from curl_cffi.requests import AsyncSession

logger = logging.getLogger("main")


class WebScraper:
    async def scrape_text(self, url: str, max_length: int = 2000) -> str:
        try:
            async with AsyncSession(impersonate="chrome") as session:
                resp = await session.get(url, timeout=5, allow_redirects=True)
                if resp.status_code != 200:
                    logger.warning(f"Website scrape failed: HTTP {resp.status_code}")
                    return ""

                html = resp.text
                text = self._extract_text(html)
                return text[:max_length]

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
