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
JUPITER_PRICE_V3 = "https://lite-api.jup.ag/price/v3"          # primary (newer, more reliable)
JUPITER_PRICE = "https://price.jup.ag/v6/price"                  # fallback
GMGN_PRICE = "https://openapi.gmgn.ai"

SOL_MINT = "So11111111111111111111111111111111111111112"
CACHE_TTL = 3.0
SOL_PRICE_CACHE_TTL = 10.0     # was 30.0 — faster recovery from Jupiter hiccups
SOL_PRICE_TIMEOUT_S = 3.0       # per-source timeout for SOL/USD fetch

# Backoff durations (Charon-equivalent)
JUPITER_BACKOFF_429_S = 30
JUPITER_BACKOFF_403_S = 600
JUPITER_BACKOFF_503_S = 1800


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
        # Jupiter backoff (Item #2)
        self._jupiter_backoff_until: float = 0.0
        self._jupiter_backoff_reason: str = ""

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

        Retries 3x with exponential backoff (1s, 2s, 4s) on failure to handle
        cold-start rate limits / network blips.
        """
        if not self._session or self._session.closed:
            kwargs = {"timeout": aiohttp.ClientTimeout(total=self.timeout)}
            if self.proxy:
                kwargs["proxy"] = self.proxy
            self._session = aiohttp.ClientSession(**kwargs)
            logger.info("PriceOracle: session initialized")

        for attempt in range(3):
            try:
                sol_usd = await self.get_sol_price_usd()
                if sol_usd > 0:
                    logger.info(f"PriceOracle: pre-warmed SOL/USD = ${sol_usd:.2f} (attempt {attempt + 1})")
                    return
            except Exception as e:
                logger.warning(f"PriceOracle: pre-warm attempt {attempt + 1} error: {e}")
            await asyncio.sleep(2 ** attempt)  # 1s, 2s, 4s

        logger.warning("PriceOracle: SOL/USD pre-warm failed after 3 attempts (will retry per-call)")

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

        # If Jupiter is backed off, skip Jupiter + DexScreener, fall through to GMGN only
        jupiter_blocked = self._jupiter_backoff_active()

        try:
            if jupiter_blocked:
                # Skip Jupiter sources, only fetch from GMGN (uses different endpoint)
                prices = await asyncio.wait_for(
                    asyncio.gather(
                        self._from_gmgn_usd(token_address),
                        return_exceptions=True,
                    ),
                    timeout=8,
                )
                prices = [0.0, 0.0] + list(prices)  # pad for sources list below
            else:
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
        """Get current SOL price in USD. 10s cache, 3-tier fallback.

        Sources (tried in order):
        1. Jupiter v3 (lite-api) — primary, most reliable
        2. Jupiter v6 (price.jup.ag) — legacy fallback
        3. DexScreener — independent rate-limit pool, last resort

        Returns 0 only on total failure AND no cached value. If backoff is
        active on Jupiter, skips Jupiter sources and tries DexScreener.
        """
        now = time.time()
        cached = self._sol_price_cache.get("SOL")
        if cached and (now - cached["ts"]) < SOL_PRICE_CACHE_TTL:
            return cached["price"]

        # Build source list — skip Jupiter sources if backed off.
        sources = []
        if not self._jupiter_backoff_active():
            sources.extend([
                ("jupiter_v3", self._fetch_sol_v3),
                ("jupiter_v6", self._fetch_sol_v6),
            ])
        # DexScreener uses a different rate-limit pool, always try it
        sources.append(("dexscreener", self._fetch_sol_dexscreener))

        for name, fetch_fn in sources:
            try:
                sol_usd = await asyncio.wait_for(fetch_fn(), timeout=SOL_PRICE_TIMEOUT_S)
                if sol_usd and sol_usd > 0:
                    self._sol_price_cache["SOL"] = {"ts": now, "price": sol_usd}
                    logger.debug(f"[SOL-USD] {name} returned ${sol_usd:.2f}")
                    return sol_usd
            except Exception as e:
                logger.debug(f"[SOL-USD] {name} failed: {e}")
                continue

        # All sources failed
        if cached:
            logger.debug(f"[SOL-USD] all sources failed, returning stale ${cached['price']:.2f}")
            return cached["price"]
        logger.warning("[SOL-USD] all sources failed AND no cache — returning 0 (no SOL/USD rate)")
        return 0.0

    async def _fetch_sol_dexscreener(self) -> float:
        """DexScreener SOL/USD price. Last-resort fallback when Jupiter is down.

        Endpoint: /latest/dex/tokens/SOL_MINT — uses the same endpoint as
        token prices but with SOL as the address. Independent rate-limit
        pool from Jupiter so it survives Jupiter backoffs.
        """
        if not self._session:
            return 0.0
        try:
            url = f"{DEXSCREENER_PRICE}/{SOL_MINT}"
            async with self._session.get(url) as resp:
                if resp.status != 200:
                    return 0.0
                data = await resp.json()
                pairs = data.get("pairs") or []
                if not pairs:
                    return 0.0
                # Pick highest-liquidity pair to get the most reliable price
                pair = max(
                    pairs,
                    key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0),
                )
                price_usd = pair.get("priceUsd")
                if price_usd and float(price_usd) > 0:
                    return float(price_usd)
        except Exception as e:
            logger.debug(f"[SOL-USD] dexscreener failed: {e}")
        return 0.0

    async def _fetch_sol_v3(self) -> float:
        """Jupiter Price API v3 (lite-api). Newer endpoint, more reliable."""
        if not self._session:
            return 0.0
        async with self._session.get(
            JUPITER_PRICE_V3, params={"ids": SOL_MINT}
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                # v3 format: { "So11...": { "usdPrice": 150.0 } }
                entry = data.get(SOL_MINT) or data.get("data", {}).get(SOL_MINT)
                if entry:
                    usd = entry.get("usdPrice") or entry.get("price")
                    if usd and float(usd) > 0:
                        return float(usd)
            elif resp.status in (429, 403, 503):
                headers = dict(resp.headers) if resp.headers else {}
                self._set_jupiter_backoff(resp.status, headers)
        return 0.0

    async def _fetch_sol_v6(self) -> float:
        """Jupiter Price API v6. Fallback when v3 is unavailable."""
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
                    if sol_usd > 0:
                        return sol_usd
            elif resp.status in (429, 403, 503):
                headers = dict(resp.headers) if resp.headers else {}
                self._set_jupiter_backoff(resp.status, headers)
        return 0.0

    def _jupiter_backoff_active(self) -> bool:
        return time.time() < self._jupiter_backoff_until

    def _set_jupiter_backoff(self, status: int, headers: dict = None) -> None:
        """Set Jupiter backoff duration based on HTTP status.

        Charon-equivalent:
        - 429: until x-ratelimit-reset header (ms/unix-s/delta-s) or now+30s
        - 403: now + 10 min
        - 503: now + 30 min (Cloudflare challenge)
        """
        headers = headers or {}
        if status == 429:
            reset = 0
            try:
                reset = int(headers.get("x-ratelimit-reset", 0) or 0)
            except (ValueError, TypeError):
                reset = 0
            now_s = time.time()
            if reset > 1_000_000_000_000:   # already ms (year 2001+)
                until_s = reset / 1000
            elif reset > 1_000_000_000:     # unix seconds (year 2001+)
                until_s = reset
            elif reset > 0:                 # delta seconds from now
                until_s = now_s + reset
            else:                           # no/invalid header
                until_s = now_s + JUPITER_BACKOFF_429_S
            self._jupiter_backoff_until = max(until_s, now_s + JUPITER_BACKOFF_429_S)
            self._jupiter_backoff_reason = f"429 (until {int(self._jupiter_backoff_until - time.time())}s)"
        elif status == 403:
            self._jupiter_backoff_until = time.time() + JUPITER_BACKOFF_403_S
            self._jupiter_backoff_reason = "403 (10 min)"
        elif status == 503:
            self._jupiter_backoff_until = time.time() + JUPITER_BACKOFF_503_S
            self._jupiter_backoff_reason = "503 (30 min)"

        if self._jupiter_backoff_until > time.time():
            logger.warning(
                f"[ORACLE] Jupiter backoff active: {self._jupiter_backoff_reason}"
            )

    async def _from_dexscreener_usd(self, token_address: str) -> float:
        """DexScreener priceUsd (USD per token)."""
        if not self._session:
            return 0.0
        if self._jupiter_backoff_active():
            return 0.0  # shared rate-limit pool with Jupiter
        try:
            url = f"{DEXSCREENER_PRICE}/{token_address}"
            async with self._session.get(url) as resp:
                if resp.status != 200:
                    if resp.status in (429, 403, 503):
                        self._set_jupiter_backoff(resp.status, dict(resp.headers) if resp.headers else {})
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
        if self._jupiter_backoff_active():
            return 0.0
        try:
            async with self._session.get(
                JUPITER_PRICE, params={"ids": token_address}
            ) as resp:
                if resp.status != 200:
                    if resp.status in (429, 403, 503):
                        self._set_jupiter_backoff(resp.status, dict(resp.headers) if resp.headers else {})
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
