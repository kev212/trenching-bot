import asyncio
import logging
import os
import time
import threading
import uuid
from typing import Optional

from curl_cffi.requests import AsyncSession

logger = logging.getLogger("main")

BASE_URL = "https://openapi.gmgn.ai"

GMGN_TIMEOUT = 15
GMGN_GATHER_TIMEOUT = 45


class GMGNClient:
    def __init__(self, api_key: str, proxy: str = "", rate_limiter=None):
        self.api_key = api_key
        self.host = BASE_URL
        self.proxy = proxy or os.environ.get("GMGN_PROXY") or os.environ.get("HTTP_PROXY") or ""
        self.rate_limiter = rate_limiter
        self._session: Optional[AsyncSession] = None
        # C1 fix: protect session init against concurrent first-call.
        # Lazy init pattern (B7-style) — `asyncio.Lock()` requires a
        # running event loop on Python 3.9/3.10, and tests construct
        # GMGNClient outside the loop. Created on first .start() call.
        self._session_lock: Optional[asyncio.Lock] = None
        if self.proxy:
            logger.warning(f"GMGN proxy: {self.proxy[:50]}...")
        else:
            logger.warning("GMGN: NO proxy")

    def _ensure_lock(self) -> asyncio.Lock:
        if self._session_lock is None:
            self._session_lock = asyncio.Lock()
        return self._session_lock

    async def start(self):
        """Eagerly initialize HTTP session. Call once at bot startup."""
        async with self._ensure_lock():
            if self._session is None:
                self._session = AsyncSession(impersonate="chrome")
                logger.info("GMGN: session initialized")

    async def _get_session(self) -> AsyncSession:
        # C1 fix: lock around the fallback init path.
        async with self._ensure_lock():
            if self._session is None:
                # Fallback only — start() should have been called
                self._session = AsyncSession(impersonate="chrome")
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

    def _proxy_dict(self) -> dict:
        if self.proxy:
            return {"https": self.proxy, "http": self.proxy}
        return {}

    async def _get(self, path: str, params: dict = None) -> dict:
        try:
            if self.rate_limiter:
                await self.rate_limiter.acquire(1)
            query = {**(params or {}), **self._auth_params()}
            session = await self._get_session()
            resp = await asyncio.wait_for(
                session.get(
                    f"{self.host}{path}",
                    params=query,
                    headers=self._headers(),
                    proxies=self._proxy_dict(),
                    timeout=GMGN_TIMEOUT,
                ),
                timeout=GMGN_TIMEOUT + 5,
            )
            if resp.status_code != 200:
                logger.warning(f"GMGN GET {path}: HTTP {resp.status_code} - {resp.text[:200]}")
                return {}
            data = resp.json()
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
        try:
            if self.rate_limiter:
                await self.rate_limiter.acquire(1)
            auth = self._auth_params()
            full_query = {**(query or {}), **auth}
            session = await self._get_session()
            resp = await asyncio.wait_for(
                session.post(
                    f"{self.host}{path}",
                    params=full_query,
                    json=body or {},
                    headers=self._headers(),
                    proxies=self._proxy_dict(),
                    timeout=GMGN_TIMEOUT,
                ),
                timeout=GMGN_TIMEOUT + 5,
            )
            if resp.status_code != 200:
                logger.warning(f"GMGN POST {path}: HTTP {resp.status_code} - {resp.text[:300]}")
                return {}
            data = resp.json()
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
        return await self._get("/v1/token/info", {"chain": "sol", "address": address})

    async def get_token_security(self, address: str) -> dict:
        return await self._get("/v1/token/security", {"chain": "sol", "address": address})

    async def get_token_holders(self, address: str) -> dict:
        return await self._get("/v1/market/token_top_holders", {"chain": "sol", "address": address})

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
