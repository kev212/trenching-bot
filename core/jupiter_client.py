"""Jupiter aggregator client: quotes, prices, and (Phase 2) swap building.

Phase 1: paper mode uses real Jupiter Quote API + Price API. No signing
or transaction submission. All methods are safe to call from paper mode.

Phase 2: add `get_swap_transaction()` for live swap building, plus
Jito bundle support for MEV protection.
"""
import logging
from typing import Optional

import aiohttp

logger = logging.getLogger("jupiter")

QUOTE_API = "https://quote-api.jup.ag/v6"
PRICE_API = "https://price.jup.ag/v6"

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

DEFAULT_SLIPPAGE_BPS = 300
DEFAULT_TIMEOUT = 15


class JupiterClient:
    """Wraps Jupiter Quote + Price APIs. Async-safe via single session."""

    def __init__(self, paper: bool = True, timeout: int = DEFAULT_TIMEOUT,
                 proxy: str = "", rate_limiter=None):
        self.paper = paper
        self.timeout = timeout
        self.proxy = proxy
        self._rate_limiter = rate_limiter
        self._session: Optional[aiohttp.ClientSession] = None

    async def init(self) -> None:
        if not self._session:
            connector = aiohttp.TCPConnector()
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                connector=connector,
            )

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def get_quote(self, input_mint: str, output_mint: str,
                        amount_lamports: int, slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
                        only_direct: bool = False) -> dict:
        """Get a swap quote. Returns Jupiter QuoteResponse or {} on error."""
        if not self._session:
            return {}
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_lamports),
            "slippageBps": str(slippage_bps),
            "onlyDirectRoutes": str(only_direct).lower(),
            "asLegacyTransaction": "false",
        }
        try:
            async with self._session.get(f"{QUOTE_API}/quote", params=params) as resp:
                if resp.status != 200:
                    logger.warning(f"[JUP] quote HTTP {resp.status} for {output_mint[:8]}")
                    return {}
                return await resp.json()
        except Exception as e:
            logger.error(f"[JUP] quote error: {e}")
            return {}

    async def get_token_price_usd(self, token_mint: str) -> float:
        """Get current token price in USD via Jupiter Price API. Returns 0 on miss."""
        if not self._session:
            return 0.0
        try:
            async with self._session.get(f"{PRICE_API}/price",
                                          params={"ids": token_mint}) as resp:
                if resp.status != 200:
                    return 0.0
                data = await resp.json()
                entry = data.get("data", {}).get(token_mint)
                if entry and "price" in entry:
                    return float(entry["price"])
        except Exception as e:
            logger.debug(f"[JUP] price usd error for {token_mint[:8]}: {e}")
        return 0.0

    async def get_sol_price_usd(self) -> float:
        return await self.get_token_price_usd(SOL_MINT)

    async def get_token_price_in_sol(self, token_mint: str) -> float:
        """Get current token price in SOL. Returns 0 on miss.

        Uses 1 unit of token (in lamports, with assumed 6 decimals) →
        quote → derive SOL price. For tokens with different decimals,
        callers should adjust the amount passed to get_quote() directly.
        """
        if not self._session:
            return 0.0
        try:
            one_unit_lamports = 1_000_000
            quote = await self.get_quote(token_mint, SOL_MINT, one_unit_lamports)
            if not quote:
                return 0.0
            out_lamports = int(quote.get("outAmount", 0))
            if out_lamports <= 0:
                return 0.0
            return out_lamports / 1e9
        except Exception as e:
            logger.debug(f"[JUP] price sol error for {token_mint[:8]}: {e}")
            return 0.0

    def price_impact_pct(self, quote: dict) -> float:
        """Extract price impact as percentage (0-100)."""
        try:
            return float(quote.get("priceImpactPct", 0))
        except (TypeError, ValueError):
            return 0.0
