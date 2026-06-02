import logging
import os
import time
import uuid
from curl_cffi.requests import AsyncSession

logger = logging.getLogger("main")

BASE_URL = "https://openapi.gmgn.ai"


class GMGNClient:
    def __init__(self, api_key: str, proxy: str = ""):
        self.api_key = api_key
        self.host = BASE_URL
        self.proxy = proxy or os.environ.get("GMGN_PROXY") or os.environ.get("HTTP_PROXY") or ""
        if self.proxy:
            logger.warning(f"GMGN proxy: {self.proxy[:50]}...")
        else:
            logger.warning("GMGN: NO proxy")

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
            query = {**(params or {}), **self._auth_params()}
            async with AsyncSession(impersonate="chrome") as session:
                resp = await session.get(
                    f"{self.host}{path}",
                    params=query,
                    headers=self._headers(),
                    proxies=self._proxy_dict(),
                    timeout=15,
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
        except Exception as e:
            logger.error(f"GMGN {path} error: {e}")
            return {}

    async def _post(self, path: str, query: dict = None, body: dict = None) -> dict:
        try:
            auth = self._auth_params()
            full_query = {**(query or {}), **auth}
            async with AsyncSession(impersonate="chrome") as session:
                resp = await session.post(
                    f"{self.host}{path}",
                    params=full_query,
                    json=body or {},
                    headers=self._headers(),
                    proxies=self._proxy_dict(),
                    timeout=15,
                )
                if resp.status_code != 200:
                    logger.warning(f"GMGN POST {path}: HTTP {resp.status_code} - {resp.text[:300]}")
                    return {}
                data = resp.json()
                if data.get("code") != 0:
                    logger.warning(f"GMGN {path}: {data.get('error')} - {data.get('message')}")
                    return {}
                return data.get("data", data)
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

    async def get_trenches(self, limit: int = 20) -> list:
        query = {"chain": "sol"}
        body = {
            "types": ["new_creation"],
            "platforms": ["Pump.fun"],
            "limit": limit,
        }
        data = await self._post("/v1/trenches", query, body)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in body.get("types", []):
                if key in data:
                    return data[key]
        return []
