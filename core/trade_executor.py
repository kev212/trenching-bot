"""Trade executor: orchestrates buy/sell flows with Jupiter quotes.

Paper mode (Phase 1): builds positions, records trades, debits/credits
simulated wallet. No real transactions. Uses GMGN price + simulated slippage.

Live mode (Phase 2): also builds, signs, submits via Helius RPC.
"""
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


class TradeExecutor:
    """Orchestrates buy/sell. Respects paper_mode (no signing/submit in paper)."""

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
        self._paper_price_cache: dict = {}
        self._paper_price_cache_ttl = 5.0

    async def execute_buy(self, token: TokenData, size_sol: float,
                          filter_params_version: int = 0) -> Optional[Position]:
        """Validate, get quote (or estimate in paper), simulate swap, create Position.

        Paper mode (Phase 1): skip Jupiter, use GMGN price + simulated slippage.
        Live mode (Phase 2): require real Jupiter quote, fail if unavailable.
        Returns Position or None.
        """
        if size_sol <= 0:
            return None

        token_decimals = self._infer_decimals(token)
        amount_token = 0.0
        entry_price = 0.0
        price_impact = 0.0

        if self.paper:
            entry_price = 0.0
            if self.price_oracle:
                try:
                    entry_price = await self.price_oracle.get_price_in_sol(token.address)
                except Exception as e:
                    logger.debug(f"[EXEC] oracle buy price error: {e}")
            if entry_price <= 0:
                entry_price = await self._gmgn_price_in_sol(token)
            if entry_price <= 0:
                logger.warning(
                    f"[EXEC] TRADE-SKIPPED {token.symbol} ({token.address[:8]}): "
                    f"no GMGN price for paper buy"
                )
                if self.positions.db:
                    await self.positions.db.save_risk_event(
                        "GMGN_PRICE_MISSING", token.address,
                        f"symbol={token.symbol}, size={size_sol:.4f} SOL, paper=True"
                    )
                return None
            simulated_slippage_pct = 1.0
            effective_price = entry_price * (1 + simulated_slippage_pct / 100)
            amount_token = size_sol / effective_price
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
            entry_price = size_sol / amount_token if amount_token > 0 else 0.0
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
            entry_price=entry_price,
            entry_amount_sol=size_sol,
            entry_amount_token=amount_token,
            entry_time=now,
            peak_price=entry_price,
            current_amount_token=amount_token,
            status="OPEN",
            filter_params_version=filter_params_version,
            paper=self.paper,
            raw_gmgn_json=raw_gmgn_json,
        )

        position_id = await self.positions.open_position(position)
        position.id = position_id

        trade = Trade(
            position_id=position_id,
            side="BUY",
            tx_signature=tx_sig,
            amount_in=size_sol,
            amount_out=amount_token,
            price=entry_price,
            slippage_bps=self.slippage_bps,
            status="CONFIRMED" if self.paper else "PENDING",
        )
        await self.positions.record_trade(trade)

        mode_tag = "PAPER" if self.paper else "LIVE"
        logger.info(
            f"[EXEC] {mode_tag} BUY {token.symbol} "
            f"@ {entry_price:.10f} SOL, size={size_sol:.4f} SOL, "
            f"tokens={amount_token:.2f}, impact={price_impact:.2f}%"
        )

        if self.paper and entry_price > 0:
            self._paper_price_cache[token.address] = {
                "ts": time.time(),
                "price": entry_price,
            }
            logger.debug(
                f"[EXEC] warmed price cache for {token.symbol} @ {entry_price:.10f} "
                f"(5s grace before any exit check)"
            )

        return position

    async def _gmgn_price_in_sol(self, token: TokenData) -> float:
        """Get token price in SOL from GMGN's info payload (paper mode only).

        Priority:
        1. raw_gmgn.price.native_token.price (SOL — preferred)
        2. raw_gmgn.price.price (USD) ÷ SOL/USD rate
        3. 0.0 (caller treats as failure)
        """
        raw = getattr(token, "raw_gmgn", {}) or {}
        price_obj = raw.get("price", {}) if isinstance(raw.get("price"), dict) else {}

        native_token = price_obj.get("native_token")
        if isinstance(native_token, dict):
            nt_price = native_token.get("price")
            if nt_price and float(nt_price) > 0:
                return float(nt_price)

        price_val = price_obj.get("price")
        if price_val and float(price_val) > 0:
            sol_usd = 0.0
            if self.price_oracle:
                try:
                    sol_usd = await self.price_oracle.get_sol_price_usd()
                except Exception:
                    pass
            if sol_usd > 0:
                return float(price_val) / sol_usd
            return float(price_val)

        return 0.0

    async def _simulate_paper_price_walk(self, position: dict, reason: str) -> float:
        """Get current token price for paper mode with 5s cache.

        Priority: oracle (3-source median) > GMGN info > raw_gmgn_json > entry_price.
        Caches price per token_address to avoid hammering sources at 4×/sec.
        """
        import time
        token_address = position.get("token_address", "")
        now = time.time()
        cached = self._paper_price_cache.get(token_address)
        if cached and (now - cached["ts"]) < self._paper_price_cache_ttl:
            return cached["price"]

        price = 0.0

        if self.price_oracle:
            try:
                price = await self.price_oracle.get_price_in_sol(token_address)
            except Exception as e:
                logger.debug(f"[EXEC] oracle paper price error: {e}")

        if price <= 0 and self.gmgn:
            try:
                info = await self.gmgn.get_token_info(token_address)
                price_obj = info.get("price", {}) if isinstance(info.get("price"), dict) else {}
                native_token = price_obj.get("native_token")
                if isinstance(native_token, dict):
                    nt_price = native_token.get("price")
                    if nt_price and float(nt_price) > 0:
                        price = float(nt_price)
                if price <= 0:
                    price_val = price_obj.get("price")
                    if price_val and float(price_val) > 0:
                        sol_usd = 0.0
                        if self.price_oracle:
                            try:
                                sol_usd = await self.price_oracle.get_sol_price_usd()
                            except Exception:
                                pass
                        if sol_usd > 0:
                            price = float(price_val) / sol_usd
                        else:
                            price = float(price_val)
            except Exception as e:
                logger.debug(f"[EXEC] paper price fetch error: {e}")

        if price <= 0:
            try:
                raw_json = position.get("raw_gmgn_json", "")
                if raw_json:
                    raw = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
                    price_obj = raw.get("price", {}) if isinstance(raw.get("price"), dict) else {}
                    native_token = price_obj.get("native_token")
                    if isinstance(native_token, dict):
                        nt_price = native_token.get("price")
                        if nt_price and float(nt_price) > 0:
                            price = float(nt_price)
                    if price <= 0:
                        price_val = price_obj.get("price")
                        if price_val and float(price_val) > 0:
                            sol_usd = 0.0
                            if self.price_oracle:
                                try:
                                    sol_usd = await self.price_oracle.get_sol_price_usd()
                                except Exception:
                                    pass
                            if sol_usd > 0:
                                price = float(price_val) / sol_usd
                            else:
                                price = float(price_val)
            except Exception as e:
                logger.debug(f"[EXEC] paper price walk parse error: {e}")

        if price <= 0:
            price = position.get("entry_price", 0.0) or 0.0

        if price > 0:
            self._paper_price_cache[token_address] = {"ts": now, "price": price}

        return price

    async def execute_sell(self, position: dict, sell_pct: float,
                            reason: str) -> Optional[Trade]:
        """Sell `sell_pct`% of position. Returns Trade or None.

        `position` is the dict shape returned by get_open_positions.
        Paper mode: simulates price walk from entry (no live price needed).
        Live mode: requires Jupiter price.
        """
        if position.get("status") != "OPEN":
            return None
        if sell_pct <= 0 or sell_pct > 100:
            return None

        if self.paper:
            current_price = await self._simulate_paper_price_walk(position, reason)
        else:
            current_price = await self.jupiter.get_token_price_in_sol_with_retry(
                position["token_address"]
            )

        if current_price <= 0:
            logger.warning(
                f"[EXEC] SELL-SKIPPED {position['token_symbol']} "
                f"({position['token_address'][:8]}): no price, "
                f"sell_pct={sell_pct:.0f}%, reason={reason}, paper={self.paper}"
            )
            if self.positions.db:
                await self.positions.db.save_risk_event(
                    "PRICE_MISSING", position["token_address"],
                    f"symbol={position['token_symbol']}, reason={reason}, "
                    f"action=SELL_SKIPPED, paper={self.paper}"
                )
            return None

        sell_amount_token = position["current_amount_token"] * (sell_pct / 100)
        sol_received = sell_amount_token * current_price
        position["total_sold_sol"] = position.get("total_sold_sol", 0.0) + sol_received

        tx_sig = self.wallet.generate_paper_signature() if self.paper else ""
        trade = Trade(
            position_id=position["id"],
            side="SELL",
            tx_signature=tx_sig,
            amount_in=sell_amount_token,
            amount_out=sol_received,
            price=current_price,
            slippage_bps=self.slippage_bps,
            status="CONFIRMED" if self.paper else "PENDING",
        )
        await self.positions.record_trade(trade)

        await self.wallet.credit(sol_received, f"SELL {position['token_symbol']} ({reason})")

        if sell_pct >= 100 or position["current_amount_token"] - sell_amount_token < 0.001:
            entry_sol = position.get("entry_amount_sol", 0.0) or 0.0
            total_sold = position.get("total_sold_sol", 0.0) or 0.0
            pnl_sol = total_sold - entry_sol
            pnl_pct = ((total_sold / entry_sol) - 1) * 100 if entry_sol > 0 else 0.0
            await self.positions.close_position(position, reason, current_price, pnl_sol, pnl_pct)
            self.risk.record_trade_result(pnl_sol)
        else:
            remaining = position["current_amount_token"] - sell_amount_token
            await self.positions.record_partial_sell(position, sell_amount_token, remaining)

        logger.info(
            f"[EXEC] {'PAPER ' if self.paper else ''}SELL {position['token_symbol']} "
            f"@ {current_price:.10f} SOL ({sell_pct:.0f}%, {reason}), "
            f"sol_out={sol_received:.4f}"
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
