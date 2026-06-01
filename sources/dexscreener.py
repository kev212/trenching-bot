import asyncio
import logging
import aiohttp

logger = logging.getLogger(__name__)

BASE_URL = "https://api.dexscreener.com"


async def dexscreener_poller(queue: asyncio.Queue, state):
    logger.info("DexScreener Poller starting...")
    seen = set()
    poll_count = 0

    while True:
        try:
            tokens = await fetch_trending_boosts()
            new_count = 0
            for token in tokens:
                addr = token.get("tokenAddress")
                if addr and addr not in seen and not await state.is_duplicate(addr):
                    seen.add(addr)
                    token_data = {
                        "address": addr,
                        "name": token.get("description", "")[:50],
                        "symbol": _extract_symbol(token),
                        "event_type": "boosted",
                        "source": "dexscreener",
                        "market_cap": 0,
                        "volume_24h": 0,
                        "price": 0,
                        "raw": token,
                    }
                    await queue.put((1, token_data))
                    new_count += 1

            poll_count += 1
            if new_count > 0:
                logger.info(f"DexScreener poll #{poll_count}: {new_count} new (queue: {queue.qsize()})")

            if len(seen) > 10000:
                seen.clear()

        except Exception as e:
            logger.error(f"DexScreener poller error: {e}")

        await asyncio.sleep(10)


async def fetch_trending_boosts() -> list:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{BASE_URL}/token-boosts/top/v1",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
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
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{BASE_URL}/tokens/v1/solana/{address}",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
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
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{BASE_URL}/token-profiles/latest/v1",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
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


def _extract_symbol(token: dict) -> str:
    url = token.get("url", "")
    if "/" in url:
        return url.split("/")[-1][:10]
    return token.get("tokenAddress", "")[:6]


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
