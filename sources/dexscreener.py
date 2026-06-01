"""DexScreener public API client: free, no key, lightweight price source.

Used as fallback for paper mode position monitoring when Jupiter is
unreliable. DexScreener returns latest pair data for a token.

API: https://api.dexscreener.com/latest/dex/tokens/{address}
"""
import logging
from typing import Optional
import aiohttp

logger = logging.getLogger("dexscreener")

DEXSCREENER_API = "https://api.dexscreener.com/latest/dex/tokens"


class DexScreenerClient:
    """Free price source. No API key, no auth."""

    def __init__(self, proxy: str = "", timeout: int = 10):
        self.proxy = proxy
        self.timeout = timeout
        self._session: Optional[aiohttp.ClientSession] = None

    async def init(self) -> None:
        if not self._session:
            kwargs = {"timeout": aiohttp.ClientTimeout(total=self.timeout)}
            if self.proxy:
                kwargs["proxy"] = self.proxy
            self._session = aiohttp.ClientSession(**kwargs)

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def get_token_price_usd(self, token_address: str) -> float:
        """Get token price in USD. Returns 0 on miss."""
        if not self._session:
            return 0.0
        try:
            url = f"{DEXSCREENER_API}/{token_address}"
            async with self._session.get(url) as resp:
                if resp.status != 200:
                    logger.debug(f"[DEX] HTTP {resp.status} for {token_address[:8]}")
                    return 0.0
                data = await resp.json()
                pairs = data.get("pairs") or []
                if not pairs:
                    return 0.0
                pair = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
                price_str = pair.get("priceUsd") or pair.get("priceNative")
                if price_str:
                    return float(price_str)
        except Exception as e:
            logger.debug(f"[DEX] price error for {token_address[:8]}: {e}")
        return 0.0
