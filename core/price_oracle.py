"""Multi-source price oracle: DexScreener → Jupiter → GMGN, take median.

Why: GMGN's `info.price.price` is USD-denominated and sometimes inconsistent
on fresh meme coins. DexScreener `priceNative` is always in SOL and reliable.
Jupiter Price API v6 is a public endpoint that aggregates DEX prices.

For paper trading, we use this to:
1. Get a more accurate price at BUY time (so entry is correct)
2. Get a more accurate price at every monitor tick (so SL/TP don't fire
   on phantom data)

Cache: 3s per token. If all 3 sources fail, return last cached price or 0.
"""
import asyncio
import json
import logging
import time
from typing import Optional

import aiohttp

logger = logging.getLogger("price_oracle")

DEXSCREENER_PRICE = "https://api.dexscreener.com/latest/dex/tokens"
JUPITER_PRICE = "https://price.jup.ag/v6/price"
GMGN_PRICE = "https://openapi.gmgn.ai"

SOL_MINT = "So11111111111111111111111111111111111111112"
CACHE_TTL = 3.0
SOL_PRICE_CACHE_TTL = 30.0


class PriceOracle:
    """Async price aggregator. Caches per token, 3s TTL."""

    CACHE_MAX = 500

    def __init__(self, gmgn=None, jupiter=None, dexscreener=None,
                 proxy: str = "", timeout: int = 10):
        self.gmgn = gmgn
        self.jupiter = jupiter
        self.dexscreener = dexscreener
        self.proxy = proxy
        self.timeout = timeout
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: dict = {}
        self._sol_price_cache: dict = {}

    def _evict_cache(self):
        if len(self._cache) > self.CACHE_MAX:
            oldest_keys = sorted(self._cache, key=lambda k: self._cache[k]["ts"])[:len(self._cache) // 4]
            for k in oldest_keys:
                del self._cache[k]

    async def start(self) -> None:
        """Eagerly initialize HTTP session. Call once at bot startup."""
        if not self._session or self._session.closed:
            kwargs = {"timeout": aiohttp.ClientTimeout(total=self.timeout)}
            if self.proxy:
                kwargs["proxy"] = self.proxy
            self._session = aiohttp.ClientSession(**kwargs)
            logger.info("PriceOracle: session initialized")

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def get_price_in_sol(self, token_address: str) -> float:
        """Get token price in SOL, using multi-source aggregation.

        Returns 0 on total failure. Cache 3s per token.
        """
        if not token_address:
            return 0.0
        now = time.time()
        cached = self._cache.get(token_address)
        if cached and (now - cached["ts"]) < CACHE_TTL:
            return cached["price"]

        try:
            prices = await asyncio.wait_for(
                asyncio.gather(
                    self._from_dexscreener(token_address),
                    self._from_jupiter(token_address),
                    self._from_gmgn(token_address),
                    return_exceptions=True,
                ),
                timeout=8,
            )
        except asyncio.TimeoutError:
            logger.debug(f"[ORACLE] timeout for {token_address[:8]}")
            return cached["price"] if cached else 0.0
        valid = [p for p in prices if isinstance(p, (int, float)) and p > 0]
        if not valid:
            if cached:
                logger.debug(
                    f"[ORACLE] all sources failed for {token_address[:8]}, "
                    f"returning cached {cached['price']:.10f}"
                )
                return cached["price"]
            return 0.0

        valid_sorted = sorted(valid)
        median = valid_sorted[len(valid_sorted) // 2]
        sources = ", ".join(
            f"{name}={p:.10f}" for name, p in zip(
                ["dex", "jup", "gmgn"], prices
            ) if isinstance(p, (int, float)) and p > 0
        )
        logger.debug(f"[ORACLE] {token_address[:8]}: median={median:.10f} ({sources})")
        self._cache[token_address] = {"ts": now, "price": median}
        self._evict_cache()
        return median

    async def get_sol_price_usd(self) -> float:
        """Get current SOL price in USD. 30s cache."""
        now = time.time()
        cached = self._sol_price_cache.get("SOL")
        if cached and (now - cached["ts"]) < SOL_PRICE_CACHE_TTL:
            return cached["price"]

        try:
            if not self._session:
                return 0.0
            async with self._session.get(
                JUPITER_PRICE, params={"ids": SOL_MINT}
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    entry = data.get("data", {}).get(SOL_MINT)
                    if entry and "price" in entry:
                        sol_usd = float(entry["price"])
                        self._sol_price_cache["SOL"] = {"ts": now, "price": sol_usd}
                        return sol_usd
        except Exception as e:
            logger.debug(f"[ORACLE] SOL USD price error: {e}")
        if cached:
            return cached["price"]
        return 0.0

    async def _from_dexscreener(self, token_address: str) -> float:
        if not self._session:
            return 0.0
        try:
            url = f"{DEXSCREENER_PRICE}/{token_address}"
            async with self._session.get(url) as resp:
                if resp.status != 200:
                    return 0.0
                data = await resp.json()
                pairs = data.get("pairs") or []
                if not pairs:
                    return 0.0
                pair = max(
                    pairs,
                    key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0),
                )
                native = pair.get("priceNative")
                if native and float(native) > 0:
                    return float(native)
        except Exception as e:
            logger.debug(f"[ORACLE] dexscreener {token_address[:8]}: {e}")
        return 0.0

    async def _from_jupiter(self, token_address: str) -> float:
        if not self._session:
            return 0.0
        try:
            async with self._session.get(
                JUPITER_PRICE, params={"ids": token_address}
            ) as resp:
                if resp.status != 200:
                    return 0.0
                data = await resp.json()
                entry = (data.get("data") or {}).get(token_address)
                if entry and "price" in entry:
                    usd = float(entry["price"])
                    sol_usd = await self.get_sol_price_usd()
                    if sol_usd > 0:
                        return usd / sol_usd
                    return usd
        except Exception as e:
            logger.debug(f"[ORACLE] jupiter {token_address[:8]}: {e}")
        return 0.0

    async def _from_gmgn(self, token_address: str) -> float:
        if not self.gmgn:
            return 0.0
        try:
            info = await self.gmgn.get_token_info(token_address)
            price_obj = info.get("price", {}) if isinstance(info.get("price"), dict) else {}
            native_token = price_obj.get("native_token")
            if isinstance(native_token, dict):
                nt_price = native_token.get("price")
                if nt_price and float(nt_price) > 0:
                    return float(nt_price)
            raw_price = price_obj.get("price")
            if raw_price and float(raw_price) > 0:
                sol_usd = await self.get_sol_price_usd()
                if sol_usd > 0:
                    return float(raw_price) / sol_usd
                return float(raw_price)
        except Exception as e:
            logger.debug(f"[ORACLE] gmgn {token_address[:8]}: {e}")
        return 0.0
