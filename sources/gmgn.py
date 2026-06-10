import asyncio
import logging
import os
import time
import threading
import uuid
from typing import Optional

import aiohttp

logger = logging.getLogger("main")

BASE_URL = "https://openapi.gmgn.ai"

GMGN_TIMEOUT = 15
GMGN_GATHER_TIMEOUT = 45

# Backoff durations (Charon-equivalent)
# NOTE: This backoff logic (lines 138-179) is duplicated in:
#   - core/price_oracle.py  (PriceOracle._jupiter_backoff_*)
#   - core/jupiter_client.py
# Keep in sync across all three locations. If extracting to a shared
# helper, put it in utils/helpers.py and import here, price_oracle.py,
# and jupiter_client.py.
GMGN_BACKOFF_429_S = 60
GMGN_BACKOFF_403_S = 600
GMGN_BACKOFF_CLOUDFLARE_S = 1800


class GMGNClient:
    def __init__(self, api_key: str, proxy: str = "", rate_limiter=None):
        self.api_key = api_key
        self.host = BASE_URL
        self.proxy = proxy or os.environ.get("GMGN_PROXY") or os.environ.get("HTTP_PROXY") or ""
        self.rate_limiter = rate_limiter
        self._session: Optional[aiohttp.ClientSession] = None
        # Locks initialized eagerly — Python 3.11+ supports creating
        # asyncio.Lock outside a running event loop. The race condition
        # from lazy _ensure_lock() / _ensure_pace_lock() is eliminated by
        # creating them once at construction time.
        self._session_lock = asyncio.Lock()
        # Backoff state
        self._backoff_until: float = 0.0
        self._backoff_reason: str = ""
        self._last_retry_after: int = 30
        # Pacing is NOT used here — the rate_limiter handles pacing.
        # (Removed _pace_lock and associated mechanism per Bug #9 fix.)
        # Bug #18: Shared TTL cache for get_token_info to eliminate duplicate
        # GMGN calls from PriceOracle AND TradeExecutor.
        self._info_cache: dict[str, tuple[float, dict]] = {}  # address -> (timestamp, info_dict)
        self._INFO_CACHE_TTL = 3.0  # short TTL; prices change every tick
        if self.proxy:
            logger.warning(f"GMGN proxy: {self.proxy[:50]}...")
        else:
            logger.warning("GMGN: NO proxy")

    async def start(self):
        """Eagerly initialize HTTP session. Call once at bot startup."""
        async with self._session_lock:
            if self._session is None:
                timeout = aiohttp.ClientTimeout(total=GMGN_TIMEOUT)
                if self.proxy:
                    self._session = aiohttp.ClientSession(
                        timeout=timeout,
                        proxy=self.proxy,
                    )
                else:
                    self._session = aiohttp.ClientSession(timeout=timeout)
                logger.info("GMGN: session initialized")

    async def _get_session(self) -> aiohttp.ClientSession:
        async with self._session_lock:
            if self._session is None:
                # Fallback only — start() should have been called
                timeout = aiohttp.ClientTimeout(total=GMGN_TIMEOUT)
                if self.proxy:
                    self._session = aiohttp.ClientSession(
                        timeout=timeout,
                        proxy=self.proxy,
                    )
                else:
                    self._session = aiohttp.ClientSession(timeout=timeout)
            return self._session

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None

    def _auth_params(self) -> dict:
        return {
            "timestamp": str(int(time.time())),
            "client_id": str(uuid.uuid4()),
        }

    def _headers(self) -> dict:
        return {
            "X-APIKEY": self.api_key,
            "Content-Type": "application/json",
        }

    def _backoff_active(self) -> bool:
        return time.time() < self._backoff_until

    def _set_backoff(self, status: int, body: str = "", retry_after: int = None) -> None:
        """Set GMGN backoff duration based on HTTP status / response.

        Charon-equivalent:
        - Cloudflare challenge (body contains 'cf_chl' or 'Just a moment'): 30 min
        - 429: now + retry_after (cap 60s) OR 30s default
        - 403: now + 10 min

        NOTE: This backoff logic is duplicated in core/price_oracle.py and
        core/jupiter_client.py. Keep in sync across all three locations.
        """
        is_cloudflare = (
            "challenge-platform" in body
            or "cf_chl" in body
            or "<title>Just a moment" in body
        )
        if is_cloudflare:
            self._backoff_until = time.time() + GMGN_BACKOFF_CLOUDFLARE_S
            self._backoff_reason = "cloudflare (30 min)"
        elif status == 429:
            delay = min(60, retry_after if retry_after else GMGN_BACKOFF_429_S)
            self._backoff_until = time.time() + delay
            self._backoff_reason = f"429 ({delay}s)"
        elif status == 403:
            self._backoff_until = time.time() + GMGN_BACKOFF_403_S
            self._backoff_reason = "403 (10 min)"

        if self._backoff_until > time.time():
            logger.warning(
                f"[GMGN] backoff until {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self._backoff_until))} "
                f"reason={self._backoff_reason}"
            )

    def _extract_retry_after(self, resp) -> int:
        """Parse Retry-After header (seconds). Default to 30s on missing/invalid."""
        try:
            ra = resp.headers.get("retry-after") or resp.headers.get("Retry-After")
            if ra:
                return int(ra)
        except (ValueError, TypeError, AttributeError):
            pass
        return 30

    async def _get(self, path: str, params: dict = None) -> dict:
        if self._backoff_active():
            logger.debug(f"GMGN GET {path} skipped (backoff: {self._backoff_reason})")
            return {}
        try:
            if self.rate_limiter:
                try:
                    await self.rate_limiter.acquire(1, timeout=15.0)
                except asyncio.TimeoutError:
                    logger.warning(f"GMGN rate limit timeout for {path}, skipping")
                    return {}
            query = {**(params or {}), **self._auth_params()}
            session = await self._get_session()
            async with session.get(
                f"{self.host}{path}",
                params=query,
                headers=self._headers(),
            ) as resp:
                if resp.status != 200:
                    body = (await resp.text() or "")[:500]
                    if resp.status in (429, 403):
                        retry_after = self._extract_retry_after(resp)
                        self._set_backoff(resp.status, body, retry_after)
                    logger.warning(f"GMGN GET {path}: HTTP {resp.status} - {body[:200]}")
                    return {}
                data = await resp.json()
            if data.get("code") != 0:
                logger.warning(f"GMGN {path}: {data.get('error')} - {data.get('message')}")
                return {}
            result = data.get("data", data)
            if isinstance(result, dict) and "data" in result:
                result = result["data"]
            return result
        except asyncio.TimeoutError:
            logger.error(f"GMGN {path} timeout (> {GMGN_TIMEOUT}s)")
            return {}
        except Exception as e:
            logger.error(f"GMGN {path} error: {e}")
            return {}

    async def _post(self, path: str, query: dict = None, body: dict = None) -> dict:
        if self._backoff_active():
            logger.debug(f"GMGN POST {path} skipped (backoff: {self._backoff_reason})")
            return {}
        try:
            if self.rate_limiter:
                try:
                    await self.rate_limiter.acquire(1, timeout=15.0)
                except asyncio.TimeoutError:
                    logger.warning(f"GMGN rate limit timeout for POST {path}, skipping")
                    return {}
            auth = self._auth_params()
            full_query = {**(query or {}), **auth}
            session = await self._get_session()
            async with session.post(
                f"{self.host}{path}",
                params=full_query,
                json=body or {},
                headers=self._headers(),
            ) as resp:
                if resp.status != 200:
                    body_text = (await resp.text() or "")[:500]
                    if resp.status in (429, 403):
                        retry_after = self._extract_retry_after(resp)
                        self._set_backoff(resp.status, body_text, retry_after)
                    logger.warning(f"GMGN POST {path}: HTTP {resp.status} - {body_text[:300]}")
                    return {}
                data = await resp.json()
            if data.get("code") != 0:
                logger.warning(f"GMGN {path}: {data.get('error')} - {data.get('message')}")
                return {}
            return data.get("data", data)
        except asyncio.TimeoutError:
            logger.error(f"GMGN {path} timeout (> {GMGN_TIMEOUT}s)")
            return {}
        except Exception as e:
            logger.error(f"GMGN {path} error: {e}")
            return {}

    async def get_trending(self, limit: int = 20) -> list:
        data = await self._get("/v1/market/rank", {"chain": "sol", "interval": "5m", "limit": limit})
        if isinstance(data, list):
            return data
        return data.get("rank", []) if isinstance(data, dict) else []

    async def get_token_info(self, address: str) -> dict:
        # Bug #18: Cached result avoids duplicate GMGN calls from
        # PriceOracle._from_gmgn_usd and TradeExecutor._simulate_paper_price_walk.
        now = time.time()
        cached = self._info_cache.get(address)
        if cached and (now - cached[0]) < self._INFO_CACHE_TTL:
            return cached[1]
        result = await self._get("/v1/token/info", {"chain": "sol", "address": address})
        # Cache even empty results (prevent retry-spam within TTL window)
        self._info_cache[address] = (time.time(), result)
        return result

    async def get_token_security(self, address: str) -> dict:
        return await self._get("/v1/token/security", {"chain": "sol", "address": address})

    async def get_token_holders(self, address: str) -> dict:
        return await self._get("/v1/market/token_top_holders", {"chain": "sol", "address": address})

    async def get_kol_holders(self, address: str, limit: int = 20) -> list:
        """Fetch KOL/renowned wallet holders for a token.

        Returns list of holder dicts, each with:
          - end_holding_at: None if still holding, timestamp if dumped
          - amount_percentage: % of supply held (decimal, e.g. 0.05 = 5%)
        """
        data = await self._get("/v1/market/token_top_holders", {
            "chain": "sol", "address": address, "tag": "renowned", "limit": limit,
        })
        if isinstance(data, dict):
            return data.get("list", [])
        return []

    async def get_token_ath(self, address: str) -> dict:
        """Fetch 1d candles, return ATH price + timestamp from highest candle."""
        now_ms = int(time.time() * 1000)
        long_ago_ms = now_ms - (1000 * 24 * 60 * 60 * 1000)
        data = await self._get("/v1/market/token_kline", {
            "chain": "sol",
            "address": address,
            "resolution": "1d",
            "from": long_ago_ms,
            "to": now_ms,
        })
        candles = data.get("list", []) if isinstance(data, dict) else []
        if not candles:
            return {}
        ath_candle = max(candles, key=lambda c: float(c.get("high", 0) or 0))
        return {
            "ath_price": float(ath_candle.get("high", 0) or 0),
            "ath_timestamp": int(ath_candle.get("time", 0) or 0) // 1000,
            "candles_checked": len(candles),
        }

    LAUNCHPADS = [
        "Pump.fun", "pump_mayhem", "pump_mayhem_agent", "pump_agent",
        "letsbonk", "bonkers", "bags", "memoo", "liquid", "bankr",
        "zora", "surge", "anoncoin", "moonshot_app", "wendotdev",
        "heaven", "sugar", "token_mill", "believe", "trendsfun",
        "trends_fun", "jup_studio", "Moonshot", "boop",
        "ray_launchpad", "meteora_virtual_curve", "xstocks",
    ]
    QUOTE_TYPES = [4, 5, 3, 1, 13, 0]

    async def get_trenches(
        self,
        limit: int = 20,
        min_smart_degen: int = 0,
        category: str = "near_completion",
    ) -> list:
        """Fetch trenches (v2 API — GMGN account needs trading volume).

        Category options:
          - near_completion  tokens nearing 100% bonding curve (has SM)
          - new_creation     freshly created tokens, very early
          - completed        tokens that already graduated
        """
        body = {
            "version": "v2",
            category: {
                "filters": ["offchain", "onchain"],
                "launchpad_platform": self.LAUNCHPADS,
                "quote_address_type": self.QUOTE_TYPES,
                "launchpad_platform_v2": True,
                "limit": limit,
                "min_smart_degen_count": min_smart_degen,
            },
        }
        data = await self._post("/v1/trenches", {"chain": "sol"}, body)
        if isinstance(data, dict):
            return data.get("pump") or data.get("new_creation") or data.get("completed") or []
        return []
