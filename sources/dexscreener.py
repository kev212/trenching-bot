"""DexScreener client. Used by `tracking/price_monitor` for USD price lookups
on legacy alert calls.

A5: The previously-defined `dexscreener_poller` was dead code — it was never
launched from `main.py`. It has been removed. The shared session below is
still used by `fetch_*` helpers (called from `tracking/price_monitor._check_single_call`).
"""
import asyncio
import logging
import aiohttp
from typing import Optional

logger = logging.getLogger(__name__)

BASE_URL = "https://api.dexscreener.com"

_shared_session: Optional[aiohttp.ClientSession] = None
_session_lock = asyncio.Lock()


async def start_shared_session():
    """Eagerly initialize shared HTTP session. Call once at bot startup."""
    global _shared_session
    async with _session_lock:
        if _shared_session is None or _shared_session.closed:
            _shared_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
            )
            logger.info("DexScreener: shared session initialized")


async def close_shared_session():
    global _shared_session
    async with _session_lock:
        if _shared_session and not _shared_session.closed:
            await _shared_session.close()
            _shared_session = None


async def _get_shared_session() -> aiohttp.ClientSession:
    global _shared_session
    async with _session_lock:
        if _shared_session is None or _shared_session.closed:
            _shared_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
            )
    return _shared_session


async def fetch_trending_boosts() -> list:
    try:
        session = await _get_shared_session()
        async with session.get(f"{BASE_URL}/token-boosts/top/v1") as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            sol_tokens = [t for t in data if t.get("chainId") == "solana"]
            return sol_tokens[:20]
    except Exception as e:
        logger.error(f"DexScreener boosts error: {e}")
        return []


async def fetch_pair_data(address: str) -> dict:
    try:
        session = await _get_shared_session()
        async with session.get(f"{BASE_URL}/tokens/v1/solana/{address}") as resp:
            if resp.status != 200:
                return {}
            data = await resp.json()
            if isinstance(data, list) and len(data) > 0:
                return data[0]
            return {}
    except Exception as e:
        logger.error(f"DexScreener pair error for {address[:10]}: {e}")
        return {}


async def fetch_new_pairs() -> list:
    try:
        session = await _get_shared_session()
        async with session.get(f"{BASE_URL}/token-profiles/latest/v1") as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            sol_tokens = [t for t in data if t.get("chainId") == "solana"]
            return sol_tokens[:20]
    except Exception as e:
        logger.error(f"DexScreener new pairs error: {e}")
        return []


def extract_pair_info(pair_data: dict) -> dict:
    if not pair_data:
        return {}

    price_change = pair_data.get("priceChange", {})
    txns = pair_data.get("txns", {})
    volume = pair_data.get("volume", {})
    liquidity = pair_data.get("liquidity", {})

    h1_txns = txns.get("h1", {})
    buys = h1_txns.get("buys", 0)
    sells = h1_txns.get("sells", 0)

    return {
        "address": pair_data.get("baseToken", {}).get("address", ""),
        "name": pair_data.get("baseToken", {}).get("name", ""),
        "symbol": pair_data.get("baseToken", {}).get("symbol", ""),
        "price_usd": float(pair_data.get("priceUsd", 0) or 0),
        "market_cap": pair_data.get("marketCap", 0) or pair_data.get("fdv", 0),
        "volume_1h": volume.get("h1", 0) or 0,
        "volume_6h": volume.get("h6", 0) or 0,
        "volume_24h": volume.get("h24", 0) or 0,
        "liquidity_usd": liquidity.get("usd", 0) or 0,
        "price_change_1h": price_change.get("h1", 0) or 0,
        "price_change_6h": price_change.get("h6", 0) or 0,
        "price_change_24h": price_change.get("h24", 0) or 0,
        "buys_1h": buys,
        "sells_1h": sells,
        "dex_id": pair_data.get("dexId", ""),
        "pair_created_at": pair_data.get("pairCreatedAt"),
        "dex_paid": pair_data.get("info", {}).get("dexPaid", False),
        "boosts": pair_data.get("boosts", {}).get("active", 0),
        "raw": pair_data,
    }


class DexScreenerClient:
    """Free price source. No API key, no auth. (Phase 1 paper fallback)"""

    def __init__(self, proxy: str = "", timeout: int = 10):
        self.proxy = proxy
        self.timeout = timeout
        self._session = None

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
            url = f"{BASE_URL}/tokens/v1/solana/{token_address}"
            async with self._session.get(url) as resp:
                if resp.status != 200:
                    logger.debug(f"[DEX] HTTP {resp.status} for {token_address[:8]}")
                    return 0.0
                data = await resp.json()
                if isinstance(data, list) and len(data) > 0:
                    pair = data[0]
                    price_str = pair.get("priceUsd") or pair.get("priceNative")
                    if price_str:
                        return float(price_str)
        except Exception as e:
            logger.debug(f"[DEX] price error for {token_address[:8]}: {e}")
        return 0.0
