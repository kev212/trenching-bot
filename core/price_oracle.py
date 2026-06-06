"""Multi-source price oracle: DexScreener → Jupiter → GMGN, take median.

All prices returned in USD (canonical unit for the bot). SOL unit is derived
at display/PnL-conversion time only.

Why USD as canonical:
- USD prices are MORE available for fresh meme coins (~95% vs ~70% for SOL)
- Eliminates the entire unit-mismatch bug class (no more "USD-as-SOL" silent
  fallbacks at oracle level)
- Single comparison unit across SL/TP/trailing checks
- Live mode safe (same code path)

Cache: 3s per token. If all 3 sources fail, return 0 (fail-closed).
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
    """Async price aggregator. Caches per token, 3s TTL. Returns USD."""

    CACHE_MAX = 500

    def __init__(self, gmgn=None, jupiter=None, dexscreener=None,
                 proxy: str = "", timeout: int = 10):
        self.gmgn = gmgn
        self.jupiter = jupiter
        self.dexscreener = dexscreener
        self.proxy = proxy
        self.timeout = timeout
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: dict = {}  # token_address -> {"ts": float, "price": float (USD)}
        self._sol_price_cache: dict = {}

    def _evict_cache(self):
        if len(self._cache) > self.CACHE_MAX:
            oldest_keys = sorted(self._cache, key=lambda k: self._cache[k]["ts"])[:len(self._cache) // 4]
            for k in oldest_keys:
                del self._cache[k]

    async def start(self) -> None:
        """Eagerly initialize HTTP session + pre-warm SOL/USD rate.

        Pre-warming sol_usd eliminates the race where the first BUY call
        happens before SOL/USD is cached, forcing a fallback path that
        previously could return USD-as-SOL silently.
        """
        if not self._session or self._session.closed:
            kwargs = {"timeout": aiohttp.ClientTimeout(total=self.timeout)}
            if self.proxy:
                kwargs["proxy"] = self.proxy
            self._session = aiohttp.ClientSession(**kwargs)
            logger.info("PriceOracle: session initialized")

        try:
            sol_usd = await self.get_sol_price_usd()
            if sol_usd > 0:
                logger.info(f"PriceOracle: pre-warmed SOL/USD = ${sol_usd:.2f}")
            else:
                logger.warning("PriceOracle: SOL/USD pre-warm failed (will retry on first call)")
        except Exception as e:
            logger.warning(f"PriceOracle: pre-warm error: {e}")

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def get_price_in_usd(self, token_address: str) -> float:
        """Get token price in USD (CANONICAL unit). Returns 0 on total failure.

        Multi-source aggregation: DexScreener priceUsd, Jupiter Price v6,
        GMGN info.price. Returns median of valid sources. Cache 3s per token.
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
                    self._from_dexscreener_usd(token_address),
                    self._from_jupiter_usd(token_address),
                    self._from_gmgn_usd(token_address),
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
        logger.debug(f"[ORACLE] {token_address[:8]}: median=${median:.10f} ({sources})")
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

    async def _from_dexscreener_usd(self, token_address: str) -> float:
        """DexScreener priceUsd (USD per token)."""
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
                price_usd = pair.get("priceUsd")
                if price_usd and float(price_usd) > 0:
                    return float(price_usd)
        except Exception as e:
            logger.debug(f"[ORACLE] dexscreener {token_address[:8]}: {e}")
        return 0.0

    async def _from_jupiter_usd(self, token_address: str) -> float:
        """Jupiter Price API v6 — returns USD directly."""
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
                    if usd > 0:
                        return usd
        except Exception as e:
            logger.debug(f"[ORACLE] jupiter {token_address[:8]}: {e}")
        return 0.0

    async def _from_gmgn_usd(self, token_address: str) -> float:
        """GMGN info.price (USD per token). Cap 5s for oracle-level timeout."""
        if not self.gmgn:
            return 0.0
        try:
            info = await asyncio.wait_for(
                self.gmgn.get_token_info(token_address), timeout=5.0
            )
            price_obj = info.get("price", {}) if isinstance(info.get("price"), dict) else {}
            raw_price = price_obj.get("price")
            if raw_price and float(raw_price) > 0:
                return float(raw_price)
        except asyncio.TimeoutError:
            logger.debug(f"[ORACLE] gmgn {token_address[:8]}: timeout 5s")
        except Exception as e:
            logger.debug(f"[ORACLE] gmgn {token_address[:8]}: {e}")
        return 0.0
