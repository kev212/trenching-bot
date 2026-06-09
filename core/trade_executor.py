"""Trade executor: orchestrates buy/sell flows with Jupiter quotes.

USD-canonical price model (v2):
- All price storage/comparison in USD (entry_price, current_price = USD)
- Wallet debit/credit stays in SOL (we buy/sell with SOL)
- PnL tracked in both USD (canonical) and SOL (display)
- `sol_usd_at_entry` and `sol_usd_at_exit` stored per position for
  accurate PnL conversion (avoids stale-conversion error)

Paper mode (Phase 1): builds positions, records trades, debits/credits
simulated wallet. No real transactions. Uses GMGN price + simulated slippage.

Live mode (Phase 2): also builds, signs, submits via Helius RPC.
"""
import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from analysis.models import TokenData
from core.jupiter_client import JupiterClient, SOL_MINT, DEFAULT_SLIPPAGE_BPS
from core.position_manager import PositionManager
from core.risk_manager import RiskManager
from core.wallet import Wallet
from analysis.models import Position, Trade

logger = logging.getLogger("executor")


# Sanity range for entry_price (USD per token). Meme coins typically fall
# in this range. Outside = unit mismatch (e.g. SOL read as USD) or junk.
MIN_VALID_USD_PRICE = 0.00000001  # 1e-8 USD = very small token
MAX_VALID_USD_PRICE = 100.0       # 100 USD per token = whale-tier cap


class TradeExecutor:
    """Orchestrates buy/sell. Respects paper_mode (no signing/submit in paper).
    All prices in USD; SOL math applied for wallet/amount conversion only.
    """

    def __init__(self, paper: bool, wallet: Wallet, jupiter: JupiterClient,
                 positions: PositionManager, risk: RiskManager, config: dict,
                 gmgn=None, price_oracle=None):
        self.paper = paper
        self.wallet = wallet
        self.jupiter = jupiter
        self.positions = positions
        self.risk = risk
        self.config = config
        self.gmgn = gmgn
        self.price_oracle = price_oracle
        self.max_price_impact_pct = config.get("max_price_impact_pct", 5.0)
        self.slippage_bps = config.get("slippage_bps", DEFAULT_SLIPPAGE_BPS)
        from collections import OrderedDict
        self._paper_price_cache: "OrderedDict[str, dict]" = OrderedDict()
        self._paper_price_cache_ttl = 2.0
        self.MAX_PAPER_CACHE = 500
        self._paper_price_in_flight: dict[str, float] = {}

    def _paper_cache_get(self, key: str):
        entry = self._paper_price_cache.get(key)
        if entry is None:
            return None
        self._paper_price_cache.move_to_end(key)
        return entry

    def _paper_cache_set(self, key: str, value: dict):
        if key in self._paper_price_cache:
            self._paper_price_cache.move_to_end(key)
        self._paper_price_cache[key] = value
        while len(self._paper_price_cache) > self.MAX_PAPER_CACHE:
            n_to_evict = max(1, self.MAX_PAPER_CACHE // 4)
            for _ in range(n_to_evict):
                if not self._paper_price_cache:
                    break
                self._paper_price_cache.popitem(last=False)

    def _paper_cache_drop(self, key: str):
        self._paper_price_cache.pop(key, None)

    async def execute_buy(self, token: TokenData, size_sol: float,
                          filter_params_version: int = 0) -> Optional[Position]:
        """Validate, get quote (or estimate in paper), simulate swap, create Position.

        Paper mode (Phase 1): skip Jupiter, use GMGN USD price + simulated slippage.
        Live mode (Phase 2): require real Jupiter quote, fail if unavailable.

        All price math in USD. Stores:
        - entry_price (USD)
        - entry_amount_sol (SOL spent)
        - entry_amount_token (token count, derived)
        - sol_usd_at_entry (for PnL conversion at exit)
        """
        if size_sol <= 0:
            return None

        token_decimals = self._infer_decimals(token)
        amount_token = 0.0
        entry_price_usd = 0.0
        price_impact = 0.0
        sol_usd_at_entry = 0.0

        if self.paper:
            entry_price_usd = 0.0
            # Check shared paper cache to avoid duplicate GMGN calls via oracle.
            cached = self._paper_cache_get(token.address)
            if cached and (time.time() - cached["ts"]) < self._paper_price_cache_ttl:
                entry_price_usd = cached["price"]
            if entry_price_usd <= 0 and self.price_oracle:
                try:
                    entry_price_usd = await self.price_oracle.get_price_in_usd(token.address)
                except Exception as e:
                    logger.debug(f"[EXEC] oracle buy price error: {e}")
            if entry_price_usd <= 0:
                entry_price_usd = await self._gmgn_price_in_usd(token)
            if entry_price_usd <= 0:
                logger.warning(
                    f"[EXEC] TRADE-SKIPPED {token.symbol} ({token.address[:8]}): "
                    f"no USD price for paper buy"
                )
                if self.positions.db:
                    await self.positions.db.save_risk_event(
                        "USD_PRICE_MISSING", token.address,
                        f"symbol={token.symbol}, size={size_sol:.4f} SOL, paper=True"
                    )
                return None

            # Sanity range check (USD canonical).
            if not (MIN_VALID_USD_PRICE <= entry_price_usd <= MAX_VALID_USD_PRICE):
                logger.warning(
                    f"[EXEC] TRADE-SKIPPED {token.symbol} ({token.address[:8]}): "
                    f"entry_price_usd {entry_price_usd:.10f} outside sanity range "
                    f"[{MIN_VALID_USD_PRICE}, {MAX_VALID_USD_PRICE}]"
                )
                if self.positions.db:
                    await self.positions.db.save_risk_event(
                        "PRICE_OUT_OF_RANGE", token.address,
                        f"symbol={token.symbol}, price={entry_price_usd:.10f} USD, "
                        f"range=[{MIN_VALID_USD_PRICE}, {MAX_VALID_USD_PRICE}]"
                    )
                return None

            # Get SOL/USD for amount_token math.
            if self.price_oracle:
                sol_usd_at_entry = await self.price_oracle.get_sol_price_usd()
            if sol_usd_at_entry <= 0:
                logger.warning(
                    f"[EXEC] TRADE-SKIPPED {token.symbol} ({token.address[:8]}): "
                    f"no SOL/USD rate for paper buy"
                )
                if self.positions.db:
                    await self.positions.db.save_risk_event(
                        "SOL_USD_MISSING", token.address,
                        f"symbol={token.symbol}, size={size_sol:.4f} SOL, paper=True"
                    )
                return None

            simulated_slippage_pct = 1.0
            effective_price_usd = entry_price_usd * (1 + simulated_slippage_pct / 100)
            # amount_token = (size_sol × SOL_USD) / price_per_token_USD
            amount_token = (size_sol * sol_usd_at_entry) / effective_price_usd
            price_impact = simulated_slippage_pct
            raw_gmgn_json = json.dumps(getattr(token, "raw_gmgn", {}) or {}, default=str)
        else:
            amount_lamports = int(size_sol * 1e9)
            quote = await self.jupiter.get_quote(
                SOL_MINT, token.address, amount_lamports, self.slippage_bps
            )
            if not quote:
                logger.warning(
                    f"[EXEC] TRADE-SKIPPED {token.symbol} ({token.address[:8]}): "
                    f"Jupiter quote failed after retries, size={size_sol:.4f} SOL"
                )
                if self.positions.db:
                    await self.positions.db.save_risk_event(
                        "JUPITER_QUOTE_FAILED", token.address,
                        f"symbol={token.symbol}, size={size_sol:.4f} SOL, paper=False"
                    )
                return None

            out_amount = int(quote.get("outAmount", 0))
            if out_amount <= 0:
                logger.warning(f"[EXEC] Zero outAmount for {token.symbol}")
                return None

            price_impact = self.jupiter.price_impact_pct(quote)
            if price_impact > self.max_price_impact_pct:
                logger.warning(
                    f"[EXEC] {token.symbol} price impact {price_impact:.2f}% "
                    f"> {self.max_price_impact_pct}%, skipping"
                )
                if self.positions.db:
                    await self.positions.db.save_risk_event(
                        "PRICE_IMPACT_EXCEEDED", token.address,
                        f"{price_impact:.2f}% > {self.max_price_impact_pct}%"
                    )
                return None

            amount_token = out_amount / (10 ** token_decimals)
            # Live mode: derive USD price from SOL/quote.
            entry_price_sol = size_sol / amount_token if amount_token > 0 else 0.0
            if self.price_oracle:
                sol_usd_at_entry = await self.price_oracle.get_sol_price_usd()
            entry_price_usd = entry_price_sol * sol_usd_at_entry if sol_usd_at_entry > 0 else 0.0
            raw_gmgn_json = ""

        if not await self.wallet.debit(size_sol, f"BUY {token.symbol}"):
            logger.warning(f"[EXEC] Insufficient balance for {token.symbol}")
            return None

        tx_sig = self.wallet.generate_paper_signature() if self.paper else ""
        now = datetime.now(timezone.utc)
        position = Position(
            token_address=token.address,
            token_symbol=token.symbol,
            entry_tx_sig=tx_sig,
            entry_price=entry_price_usd,  # SEMANTIC CHANGE: now USD
            entry_amount_sol=size_sol,
            entry_amount_token=amount_token,
            entry_time=now,
            peak_price=entry_price_usd,  # SEMANTIC CHANGE: now USD
            current_amount_token=amount_token,
            status="OPEN",
            filter_params_version=filter_params_version,
            paper=self.paper,
            raw_gmgn_json=raw_gmgn_json,
            sol_usd_at_entry=sol_usd_at_entry,
        )

        position_id = await self.positions.open_position(position)
        position.id = position_id

        trade = Trade(
            position_id=position_id,
            side="BUY",
            tx_signature=tx_sig,
            amount_in=size_sol,
            amount_out=amount_token,
            price=entry_price_usd,
            slippage_bps=self.slippage_bps,
            status="CONFIRMED" if self.paper else "PENDING",
        )
        await self.positions.record_trade(trade)

        mode_tag = "PAPER" if self.paper else "LIVE"
        logger.info(
            f"[EXEC] {mode_tag} BUY {token.symbol} "
            f"@ ${entry_price_usd:.10f} (size={size_sol:.4f} SOL, "
            f"tokens={amount_token:.2f}, sol_usd={sol_usd_at_entry:.2f}, "
            f"impact={price_impact:.2f}%)"
        )

        # Warm paper price cache with USD price.
        if self.paper and entry_price_usd > 0:
            self._paper_price_cache[token.address] = {
                "ts": time.time(),
                "price": entry_price_usd,
            }
            logger.debug(
                f"[EXEC] warmed USD price cache for {token.symbol} @ ${entry_price_usd:.10f} "
                f"(5s grace before any exit check)"
            )

        return position

    async def _gmgn_price_in_usd(self, token: TokenData) -> float:
        """Get token price in USD from GMGN's info payload.

        Returns USD value of raw_gmgn.price.price field. Returns 0 on failure.
        """
        raw = getattr(token, "raw_gmgn", {}) or {}
        price_obj = raw.get("price", {}) if isinstance(raw.get("price"), dict) else {}
        price_val = price_obj.get("price")
        if price_val and float(price_val) > 0:
            return float(price_val)
        return 0.0

    async def _simulate_paper_price_walk(self, position: dict, reason: str) -> float:
        """Get current token price in USD for paper mode with 2s cache and in-flight dedup.

        Priority: paper cache > in-flight wait > oracle (3-source median USD) > GMGN info.

        In-flight flag prevents concurrent price walks for the same token. Always
        cleared in finally (even on CancelledError) to prevent stuck flags.
        Auto-clears if stale (>30s) as safety net against leaked flags.
        """
        token_address = position.get("token_address", "")
        now = time.time()

        # 1. Check cache first (fast path).
        cached = self._paper_price_cache.get(token_address)
        if cached and (now - cached["ts"]) < self._paper_price_cache_ttl:
            return cached["price"]

        # 2. Check in-flight flag: if another fetch is in progress, wait or use stale.
        in_flight_since = self._paper_price_in_flight.get(token_address, 0.0)
        if in_flight_since > 0.0:
            age = now - in_flight_since
            if age > 30.0:
                # Stuck flag — override and proceed.
                logger.warning(
                    f"[EXEC] paper price in-flight stale for "
                    f"{token_address[:8]} ({age:.1f}s), overriding"
                )
                self._paper_price_in_flight.pop(token_address, None)
            else:
                # Wait briefly for the in-flight fetch to complete, then check.
                await asyncio.sleep(0.05)
                cached = self._paper_price_cache.get(token_address)
                if cached and (now - cached["ts"]) < self._paper_price_cache_ttl:
                    return cached["price"]
                stale = self._paper_price_cache.get(token_address)
                if stale:
                    return stale["price"]
                return 0.0

        # 3. Set in-flight flag.
        self._paper_price_in_flight[token_address] = now
        # Evict any stuck in-flight flags (safety net).
        self._evict_stale_in_flight()

        price = 0.0
        try:
            # 4a. Try oracle (may have fresh price from its own cache).
            if self.price_oracle:
                try:
                    price = await asyncio.wait_for(
                        self.price_oracle.get_price_in_usd(token_address),
                        timeout=4.0,
                    )
                except asyncio.TimeoutError:
                    logger.debug(f"[EXEC] oracle paper price timeout for {token_address[:8]}")
                except asyncio.CancelledError:
                    logger.debug(f"[EXEC] oracle paper price cancelled for {token_address[:8]}")
                    raise
                except Exception as e:
                    logger.debug(f"[EXEC] oracle paper price error: {e}")

            # 4b. Fallback: direct GMGN call.
            if price <= 0 and self.gmgn:
                try:
                    info = await asyncio.wait_for(
                        self.gmgn.get_token_info(token_address),
                        timeout=5.0,
                    )
                    price_obj = info.get("price", {}) if isinstance(info.get("price"), dict) else {}
                    price_val = price_obj.get("price")
                    if price_val and float(price_val) > 0:
                        price = float(price_val)
                except asyncio.TimeoutError:
                    logger.debug(f"[EXEC] paper price gmgn timeout for {token_address[:8]}")
                except asyncio.CancelledError:
                    logger.debug(f"[EXEC] paper price gmgn cancelled for {token_address[:8]}")
                    raise
                except Exception as e:
                    logger.debug(f"[EXEC] paper price fetch error: {e}")

            # 4c. Final fallback: position's raw_gmgn_json (stored at buy time).
            if price <= 0:
                try:
                    raw_json = position.get("raw_gmgn_json", "")
                    if raw_json:
                        raw = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
                        price_obj = raw.get("price", {}) if isinstance(raw.get("price"), dict) else {}
                        price_val = price_obj.get("price")
                        if price_val and float(price_val) > 0:
                            price = float(price_val)
                except Exception as e:
                    logger.debug(f"[EXEC] paper price walk parse error: {e}")
        finally:
            # Always cache result and clear in-flight, even on CancelledError.
            self._paper_price_cache[token_address] = {"ts": time.time(), "price": price}
            self._paper_price_in_flight.pop(token_address, None)

        return price

    def _evict_stale_in_flight(self):
        """Remove in-flight flags older than 30s as a safety net."""
        now = time.time()
        stale = [addr for addr, ts in self._paper_price_in_flight.items()
                 if now - ts > 30.0]
        for addr in stale:
            self._paper_price_in_flight.pop(addr, None)

    async def execute_sell(self, position: dict, sell_pct: float,
                            reason: str,
                            current_price_usd: float = None) -> Optional[Trade]:
        """Sell `sell_pct`% of position. Returns Trade or None.

        All price math in USD. Computes:
        - sol_received = (amount_token × current_price_usd) / sol_usd_now
        - pnl_usd = (current_price_usd - entry_price_usd) × amount_token
        - pnl_sol = pnl_usd / sol_usd_now

        `position` is the dict shape returned by get_open_positions.
        If `current_price_usd` is provided, skip the internal price walk
        (avoids redundant slow fetch that can cascade into monitor timeout).
        Paper mode: simulates price walk from entry (no live price needed).
        Live mode: requires Jupiter price.
        """
        if position.get("status") != "OPEN":
            return None
        if sell_pct <= 0 or sell_pct > 100:
            return None

        if current_price_usd is None:
            if self.paper:
                current_price_usd = await self._simulate_paper_price_walk(position, reason)
            else:
                # Live mode: get SOL price from Jupiter, convert to USD.
                current_price_sol = await self.jupiter.get_token_price_in_sol_with_retry(
                    position["token_address"]
                )
                sol_usd_now = await self.price_oracle.get_sol_price_usd() if self.price_oracle else 0.0
                current_price_usd = current_price_sol * sol_usd_now if sol_usd_now > 0 else 0.0

        if current_price_usd <= 0:
            if reason == "STUCK":
                # STUCK force-close: use entry_price for 0% PnL.
                # Without this, stuck positions never close because price
                # walk keeps timing out and blocking the monitor.
                current_price_usd = position.get("entry_price", 0) or 0
                logger.warning(
                    f"[EXEC] STUCK close for {position['token_symbol']} "
                    f"({position['token_address'][:8]}): "
                    f"price=${current_price_usd:.10f} ($0 PnL)"
                )
            else:
                logger.warning(
                    f"[EXEC] SELL-SKIPPED {position['token_symbol']} "
                    f"({position['token_address'][:8]}): no USD price, "
                    f"sell_pct={sell_pct:.0f}%, reason={reason}, paper={self.paper}"
                )
                if self.positions.db:
                    await self.positions.db.save_risk_event(
                        "PRICE_MISSING", position["token_address"],
                        f"symbol={position['token_symbol']}, reason={reason}, "
                        f"action=SELL_SKIPPED, paper={self.paper}"
                    )
                return None

        # Get current SOL/USD for SOL math.
        sol_usd_now = 0.0
        if self.price_oracle:
            try:
                sol_usd_now = await self.price_oracle.get_sol_price_usd()
            except Exception as e:
                logger.debug(f"[EXEC] get_sol_price_usd error: {e}")
        if sol_usd_now <= 0:
            sol_usd_now = position.get("sol_usd_at_entry", 0.0) or 0.0
        if sol_usd_now <= 0:
            # Last resort: assume a reasonable rate (will be slightly off)
            sol_usd_now = 150.0
            logger.warning(
                f"[EXEC] no SOL/USD available for {position['token_symbol']} sell, "
                f"using fallback ${sol_usd_now:.2f}"
            )

        sell_amount_token = position["current_amount_token"] * (sell_pct / 100)
        # SOL received = (tokens × USD-per-token) / SOL-USD rate
        sol_received = (sell_amount_token * current_price_usd) / sol_usd_now

        # Track total sold in BOTH units.
        prev_total_sold_sol = position.get("total_sold_sol", 0.0) or 0.0
        prev_total_sold_usd = position.get("total_sold_usd", 0.0) or 0.0
        this_sell_usd = sell_amount_token * current_price_usd
        position["total_sold_sol"] = prev_total_sold_sol + sol_received
        position["total_sold_usd"] = prev_total_sold_usd + this_sell_usd

        tx_sig = self.wallet.generate_paper_signature() if self.paper else ""
        trade = Trade(
            position_id=position["id"],
            side="SELL",
            tx_signature=tx_sig,
            amount_in=sell_amount_token,
            amount_out=sol_received,
            price=current_price_usd,
            slippage_bps=self.slippage_bps,
            status="CONFIRMED" if self.paper else "PENDING",
        )
        await self.positions.record_trade(trade)

        await self.wallet.credit(sol_received, f"SELL {position['token_symbol']} ({reason})")

        # Compute PnL on close.
        if sell_pct >= 100 or position["current_amount_token"] - sell_amount_token < 0.001:
            entry_sol = position.get("entry_amount_sol", 0.0) or 0.0
            entry_usd = entry_sol * (position.get("sol_usd_at_entry", 0.0) or 0.0)
            # Use latest total_sold_*, or fallback to accumulated
            total_sold_sol = position["total_sold_sol"]
            total_sold_usd = position["total_sold_usd"]
            pnl_sol = total_sold_sol - entry_sol
            pnl_usd = total_sold_usd - entry_usd
            pnl_pct = ((total_sold_sol / entry_sol) - 1) * 100 if entry_sol > 0 else 0.0
            await self.positions.close_position(
                position, reason, current_price_usd,
                pnl_sol=pnl_sol, pnl_usd=pnl_usd, pnl_pct=pnl_pct,
                sol_usd_at_exit=sol_usd_now,
            )
            self.risk.record_trade_result(pnl_sol)
        else:
            remaining = position["current_amount_token"] - sell_amount_token
            await self.positions.record_partial_sell(position, sell_amount_token, remaining)

        logger.info(
            f"[EXEC] {'PAPER ' if self.paper else ''}SELL {position['token_symbol']} "
            f"@ ${current_price_usd:.10f} ({sell_pct:.0f}%, {reason}), "
            f"sol_out={sol_received:.4f}, sol_usd={sol_usd_now:.2f}"
        )
        return trade

    def _infer_decimals(self, token: TokenData) -> int:
        """Infer token decimals from raw_gmgn payload, fallback to 6 (SPL standard)."""
        try:
            raw = getattr(token, "raw_gmgn", {}) or {}
            decimals = int(raw.get("decimals", 6) or 6)
            if decimals < 0 or decimals > 18:
                return 6
            return decimals
        except Exception:
            return 6
