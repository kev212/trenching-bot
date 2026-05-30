import logging
import os
import time
import uuid
import httpx

logger = logging.getLogger("main")

BASE_URL = "https://openapi.gmgn.ai"


class GMGNClient:
    def __init__(self, api_key: str, proxy: str = ""):
        self.api_key = api_key
        self.host = BASE_URL
        self.proxy = proxy or os.environ.get("GMGN_PROXY") or ""
        if self.proxy:
            logger.warning(f"GMGN proxy: {self.proxy[:40]}...")
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
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        }

    def _client_kwargs(self) -> dict:
        kwargs = {"headers": self._headers(), "timeout": 15.0, "verify": False}
        if self.proxy:
            kwargs["proxy"] = self.proxy
        return kwargs

    async def _get(self, path: str, params: dict = None) -> dict:
        try:
            query = {**(params or {}), **self._auth_params()}
            async with httpx.AsyncClient(**self._client_kwargs()) as client:
                resp = await client.get(f"{self.host}{path}", params=query)
                if resp.status_code != 200:
                    logger.warning(f"GMGN {path}: HTTP {resp.status_code} - {resp.text[:200]}")
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

    async def _post(self, path: str, body: dict = None) -> dict:
        try:
            query = self._auth_params()
            async with httpx.AsyncClient(**self._client_kwargs()) as client:
                resp = await client.post(f"{self.host}{path}", params=query, json=body or {})
                if resp.status_code != 200:
                    logger.warning(f"GMGN {path}: HTTP {resp.status_code} - {resp.text[:200]}")
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
        data = await self._get("/v1/market/rank", {"chain": "sol", "interval": "1h", "limit": limit})
        if isinstance(data, list):
            return data
        return data.get("rank", []) if isinstance(data, dict) else []

    async def get_token_info(self, address: str) -> dict:
        return await self._get("/v1/token/info", {"chain": "sol", "address": address})

    async def get_token_security(self, address: str) -> dict:
        return await self._get("/v1/token/security", {"chain": "sol", "address": address})

    async def get_token_holders(self, address: str) -> dict:
        return await self._get("/v1/market/token_top_holders", {"chain": "sol", "address": address})

    async def get_trenches(self, limit: int = 20) -> list:
        body = {
            "chain": "sol",
            "types": ["new_creation"],
            "platforms": ["pump", "raydium"],
            "limit": limit,
        }
        data = await self._post("/v1/trenches", body)
        if isinstance(data, list):
            return data
        return data.get("items", []) if isinstance(data, dict) else []
