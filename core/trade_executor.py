"""Trade executor: orchestrates buy/sell flows with Jupiter quotes.

USD-canonical price model (v2):
- All price storage/comparison in USD (entry_price, current_price = USD)
- Wallet debit/credit stays in SOL (we buy/sell with SOL)
- PnL tracked in both USD (canonical) and SOL (display)
- `sol_usd_at_entry` and `sol_usd_at_exit` stored per position for
  accurate PnL conversion (avoids stale-conversion error)

Paper mode (Phase 1): builds positions, records trades, debits/credits
simulated wallet. No real transactions. Uses GMGN price + simulated slippage.

Live mode (Phase 2): executes real swaps via GMGN OpenAPI.
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

MIN_VALID_USD_PRICE = 0.00000001
MAX_VALID_USD_PRICE = 100.0


class TradeExecutor:
    """Orchestrates buy/sell. Respects paper_mode (no signing/submit in paper).
    All prices in USD; SOL math applied for wallet/amount conversion only.
    """

    def __init__(self, paper: bool, wallet: Wallet, jupiter: JupiterClient,
                 positions: PositionManager, risk: RiskManager, config: dict,
                 gmgn=None, price_oracle=None, gmgn_cli=None):
        self.paper = paper
        self.wallet = wallet
        self.jupiter = jupiter
        self.positions = positions
        self.risk = risk
        self.config = config
        self.gmgn = gmgn
        self.price_oracle = price_oracle
        self.gmgn_cli = gmgn_cli
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
        if size_sol <= 0:
            return None

        if not self.paper and self.gmgn_cli and self.gmgn_cli.is_ready():
            return await self._execute_buy_live(token, size_sol, filter_params_version)

        return await self._execute_buy_paper(token, size_sol, filter_params_version)

    def _build_condition_orders_json(self) -> str:
        """Build TP1 + TP2 + Trailing + SL condition orders JSON for GMGN swap.

        Strategy (verified 2026-06 on real GMGN CLI):
        - `--sell-ratio-type hold_amount` (swap-level flag) → each condition sells
          % of CURRENT position, not original buy.
        - TP1: profit_stop 1.50x sell 75% of position
        - TP2: profit_stop 2.00x sell 100% of REMAINING (= 25% of original)
        - Trailing: profit_stop_trace 1.50x activation, drawdown 30%, sell 100%
                    of remaining (tracks after TP1 if no TP2)
        - SL: loss_stop 0.50x (-50%) sell 100% of position

        price_scale (verified 2026-06-14, real GMGN CLI):
          - profit_stop: GAIN % from entry. e.g. "50" = +50% / 1.5x, "100" = +100% / 2.0x
          - loss_stop: DROP % from entry. e.g. "50" = drops 50% / triggers at 0.5x
          - profit_stop_trace: activation gain %, same as profit_stop
          - Formula: gain_pct = (multiplier - 1) × 100
        """
        # profit_stop: gain % = (multiplier - 1) × 100
        # loss_stop: drop % = stop_loss_pct
        tp1_mult = int((self.config.get("tp1_multiplier", 1.5) - 1) * 100)  # 50 for 1.5x
        tp1_pct = int(self.config.get("tp1_sell_pct", 75))
        tp2_mult = int((self.config.get("tp2_multiplier", 2.0) - 1) * 100)  # 100 for 2.0x
        tp2_pct = int(self.config.get("tp2_sell_pct", 100))
        trail_mult = int((self.config.get("trailing_activation_mult", 1.5) - 1) * 100)  # 50
        trail_dd = int(self.config.get("trailing_drawdown_pct", 30))
        sl_scale = int(self.config.get("stop_loss_pct", 50))  # 50 for -50%

        orders = [
            {
                "order_type": "profit_stop",
                "side": "sell",
                "price_scale": str(tp1_mult),
                "sell_ratio": str(tp1_pct),
            },
            {
                "order_type": "profit_stop",
                "side": "sell",
                "price_scale": str(tp2_mult),
                "sell_ratio": str(tp2_pct),
            },
            {
                "order_type": "profit_stop_trace",
                "side": "sell",
                "price_scale": str(trail_mult),
                "sell_ratio": "100",
                "drawdown_rate": str(trail_dd),
            },
            {
                "order_type": "loss_stop",
                "side": "sell",
                "price_scale": str(sl_scale),
                "sell_ratio": "100",
            },
        ]
        return json.dumps(orders)

    async def _execute_buy_live(self, token: TokenData, size_sol: float,
                                 filter_params_version: int = 0) -> Optional[Position]:
        """Execute a real buy via GMGN CLI swap.

        Pre-trade: sync balance from GMGN hosted wallet (Helius can't see it).
        Attach TP/SL condition orders to the swap (Step 3) — GMGN handles exits
        on-chain, no Python polling needed.
        """
        token_decimals = self._infer_decimals(token)
        amount_lamports = int(size_sol * 1e9)

        # Step 1: sync balance from GMGN hosted wallet
        balance = await self.wallet.sync_from_gmgn(self.gmgn_cli)
        if balance < size_sol + self.wallet.RESERVE_SOL:
            logger.warning(
                f"[EXEC-LIVE] Insufficient GMGN balance for {token.symbol}: "
                f"have {balance:.4f} SOL, need {size_sol + self.wallet.RESERVE_SOL:.4f}"
            )
            return None

        # Step 3: build TP/SL condition orders
        condition_orders_json = self._build_condition_orders_json()
        sell_ratio_type = self.config.get("sell_ratio_type", "hold_amount")
        strategy_order_id_hint = ""

        result = await self.gmgn_cli.swap(
            chain="sol",
            from_addr=self.wallet.pubkey,
            input_token=SOL_MINT,
            output_token=token.address,
            amount=amount_lamports,
            slippage=max(1, self.slippage_bps // 100),
            condition_orders=condition_orders_json,
            priority_fee=0.0001,
            tip_fee=0.00001,
            sell_ratio_type=sell_ratio_type,
        )
        if not result or not result.get("order_id"):
            logger.warning(
                f"[EXEC-LIVE] BUY failed for {token.symbol} ({token.address[:8]}): "
                f"swap returned no order_id"
            )
            if self.positions.db:
                await self.positions.db.save_risk_event(
                    "GMGN_SWAP_FAILED", token.address,
                    f"symbol={token.symbol}, size={size_sol:.4f} SOL"
                )
            return None

        order_id = result["order_id"]
        strategy_order_id_hint = result.get("strategy_order_id", "") or ""
        status = await self.gmgn_cli.wait_for_order("sol", order_id)
        # FIX C2: read state from confirmation.state (real GMGN CLI response
        # shape), not top-level "status". Verified by core/gmgn_cli.py:194-202
        # _is_terminal() which checks both paths. Real response:
        #   {"confirmation": {"state": "confirmed", "detail": "success"},
        #    "hash": "...", "report": {...}}
        # Previously status.get("status") returned None for every response,
        # making the buy always return None. The test mock used the wrong
        # format too, masking the bug.
        order_state = (
            status.get("confirmation", {}).get("state")
            or status.get("status", "")
        )
        if order_state != "confirmed":
            logger.warning(
                f"[EXEC-LIVE] BUY {token.symbol}: order {order_id} not confirmed: "
                f"{order_state}"
            )
            return None

        tx_id = status.get("hash", order_id)
        report = status.get("report", {})
        out_amount_raw = int(report.get("output_amount", 0))
        out_decimals = int(report.get("output_token_decimals", token_decimals))
        amount_token = out_amount_raw / (10 ** out_decimals) if out_amount_raw > 0 else 0.0

        if amount_token <= 0:
            logger.warning(
                f"[EXEC-LIVE] BUY {token.symbol}: output_amount=0, order={order_id[:16]}"
            )
            return None

        sol_usd_at_entry = 0.0
        if self.price_oracle:
            sol_usd_at_entry = await self.price_oracle.get_sol_price_usd()
        entry_price_usd = (size_sol * sol_usd_at_entry) / amount_token if amount_token > 0 else 0.0

        # Live mode: skip local debit (gmgn-cli manages balance).
        # Refresh local cache from GMGN so subsequent calls see the new balance.
        await self.wallet.sync_from_gmgn(self.gmgn_cli)

        now = datetime.now(timezone.utc)
        position = Position(
            token_address=token.address,
            token_symbol=token.symbol,
            entry_tx_sig=tx_id,
            entry_price=entry_price_usd,
            entry_amount_sol=size_sol,
            entry_amount_token=amount_token,
            entry_time=now,
            peak_price=entry_price_usd,
            current_amount_token=amount_token,
            status="OPEN",
            filter_params_version=filter_params_version,
            paper=self.paper,
            raw_gmgn_json=json.dumps(getattr(token, "raw_gmgn", {}) or {}, default=str),
            sol_usd_at_entry=sol_usd_at_entry,
            strategy_order_id=strategy_order_id_hint,
        )
        position_id = await self.positions.open_position(position)
        position.id = position_id

        trade = Trade(
            position_id=position_id,
            side="BUY",
            tx_signature=tx_id,
            amount_in=size_sol,
            amount_out=amount_token,
            price=entry_price_usd,
            slippage_bps=self.slippage_bps,
            status="CONFIRMED",
        )
        await self.positions.record_trade(trade)

        if self.risk and hasattr(self.risk, "record_trade_open"):
            self.risk.record_trade_open()

        logger.info(
            f"[EXEC-LIVE] BUY {token.symbol} @ ${entry_price_usd:.10f} "
            f"(size={size_sol:.4f} SOL, tokens={amount_token:.2f}, "
            f"tx={tx_id[:16]}, sol_usd={sol_usd_at_entry:.2f})"
        )
        return position

    async def _execute_buy_paper(self, token: TokenData, size_sol: float,
                                  filter_params_version: int = 0) -> Optional[Position]:
        """Paper buy: simulate using GMGN USD price."""
        token_decimals = self._infer_decimals(token)
        amount_token = 0.0
        entry_price_usd = 0.0
        price_impact = 0.0
        sol_usd_at_entry = 0.0

        entry_price_usd = 0.0
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
        amount_token = (size_sol * sol_usd_at_entry) / effective_price_usd
        price_impact = simulated_slippage_pct
        raw_gmgn_json = json.dumps(getattr(token, "raw_gmgn", {}) or {}, default=str)

        if not await self.wallet.debit(size_sol, f"BUY {token.symbol}"):
            logger.warning(f"[EXEC] Insufficient balance for {token.symbol}")
            return None

        tx_sig = self.wallet.generate_paper_signature()
        now = datetime.now(timezone.utc)
        position = Position(
            token_address=token.address,
            token_symbol=token.symbol,
            entry_tx_sig=tx_sig,
            entry_price=entry_price_usd,
            entry_amount_sol=size_sol,
            entry_amount_token=amount_token,
            entry_time=now,
            peak_price=entry_price_usd,
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
            status="CONFIRMED",
        )
        await self.positions.record_trade(trade)

        logger.info(
            f"[EXEC] PAPER BUY {token.symbol} "
            f"@ ${entry_price_usd:.10f} (size={size_sol:.4f} SOL, "
            f"tokens={amount_token:.2f}, sol_usd={sol_usd_at_entry:.2f}, "
            f"impact={price_impact:.2f}%)"
        )

        if entry_price_usd > 0:
            self._paper_price_cache[token.address] = {
                "ts": time.time(),
                "price": entry_price_usd,
            }

        if self.risk and hasattr(self.risk, "record_trade_open"):
            self.risk.record_trade_open()

        return position

    async def _gmgn_price_in_usd(self, token: TokenData) -> float:
        raw = getattr(token, "raw_gmgn", {}) or {}
        price_obj = raw.get("price", {}) if isinstance(raw.get("price"), dict) else {}
        price_val = price_obj.get("price")
        if price_val and float(price_val) > 0:
            return float(price_val)
        return 0.0

    async def _simulate_paper_price_walk(self, position: dict, reason: str) -> float:
        token_address = position.get("token_address", "")
        now = time.time()

        cached = self._paper_price_cache.get(token_address)
        if cached and (now - cached["ts"]) < self._paper_price_cache_ttl:
            return cached["price"]

        in_flight_since = self._paper_price_in_flight.get(token_address, 0.0)
        if in_flight_since > 0.0:
            age = now - in_flight_since
            if age > 30.0:
                logger.warning(
                    f"[EXEC] paper price in-flight stale for "
                    f"{token_address[:8]} ({age:.1f}s), overriding"
                )
                self._paper_price_in_flight.pop(token_address, None)
            else:
                await asyncio.sleep(0.05)
                cached = self._paper_price_cache.get(token_address)
                if cached and (now - cached["ts"]) < self._paper_price_cache_ttl:
                    return cached["price"]
                stale = self._paper_price_cache.get(token_address)
                if stale:
                    return stale["price"]
                return 0.0

        self._paper_price_in_flight[token_address] = now
        self._evict_stale_in_flight()

        price = 0.0
        try:
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
            self._paper_price_cache[token_address] = {"ts": time.time(), "price": price}
            self._paper_price_in_flight.pop(token_address, None)

        return price

    def _evict_stale_in_flight(self):
        now = time.time()
        stale = [addr for addr, ts in self._paper_price_in_flight.items()
                 if now - ts > 30.0]
        for addr in stale:
            self._paper_price_in_flight.pop(addr, None)

    async def execute_sell(self, position: dict, sell_pct: float,
                            reason: str,
                            current_price_usd: float = None) -> Optional[Trade]:
        if position.get("status") != "OPEN":
            return None
        if sell_pct <= 0 or sell_pct > 100:
            return None

        sell_amount_token = position["current_amount_token"] * (sell_pct / 100)

        if not self.paper and self.gmgn_cli and self.gmgn_cli.is_ready():
            return await self._execute_sell_live(position, sell_amount_token,
                                                  sell_pct, reason)

        return await self._execute_sell_paper(position, sell_amount_token,
                                               sell_pct, reason,
                                               current_price_usd)

    async def _execute_sell_live(self, position: dict, sell_amount_token: float,
                                  sell_pct: float, reason: str) -> Optional[Trade]:
        """Execute a real sell via GMGN CLI swap."""
        token_decimals = self._infer_decimals_from_position(position)
        sell_lamports = int(sell_amount_token * (10 ** token_decimals))
        if sell_lamports <= 0:
            return None

        result = await self.gmgn_cli.swap(
            chain="sol",
            from_addr=self.wallet.pubkey,
            input_token=position["token_address"],
            output_token=SOL_MINT,
            amount=sell_lamports,
            slippage=max(1, self.slippage_bps // 100),
        )
        if not result or not result.get("order_id"):
            logger.warning(
                f"[EXEC-LIVE] SELL failed {position['token_symbol']} "
                f"({position['token_address'][:8]}): swap returned no order_id"
            )
            return None

        order_id = result["order_id"]
        status = await self.gmgn_cli.wait_for_order("sol", order_id)
        # FIX C2: same as buy path — read state from confirmation.state
        # (real GMGN CLI response shape) instead of top-level "status".
        order_state = (
            status.get("confirmation", {}).get("state")
            or status.get("status", "")
        )
        if order_state != "confirmed":
            logger.warning(
                f"[EXEC-LIVE] SELL {position['token_symbol']}: order {order_id} not confirmed ({order_state})"
            )
            return None

        tx_id = status.get("hash", order_id)
        report = status.get("report", {})
        out_amount_raw = int(report.get("output_amount", 0))
        sol_received = out_amount_raw / 1e9

        sol_usd_now = 0.0
        if self.price_oracle:
            try:
                sol_usd_now = await self.price_oracle.get_sol_price_usd()
            except Exception as e:
                logger.debug(f"[EXEC] get_sol_price_usd error: {e}")
        if sol_usd_now <= 0:
            sol_usd_now = position.get("sol_usd_at_entry", 0.0) or 0.0
        if sol_usd_now <= 0:
            sol_usd_now = 150.0

        current_price_usd = sol_received / sell_amount_token if sell_amount_token > 0 else 0.0

        prev_total_sold_sol = position.get("total_sold_sol", 0.0) or 0.0
        prev_total_sold_usd = position.get("total_sold_usd", 0.0) or 0.0
        this_sell_usd = sell_amount_token * current_price_usd
        position["total_sold_sol"] = prev_total_sold_sol + sol_received
        position["total_sold_usd"] = prev_total_sold_usd + this_sell_usd

        trade = Trade(
            position_id=position["id"],
            side="SELL",
            tx_signature=tx_id,
            amount_in=sell_amount_token,
            amount_out=sol_received,
            price=current_price_usd,
            slippage_bps=self.slippage_bps,
            status="CONFIRMED",
        )
        await self.positions.record_trade(trade)
        await self.wallet.credit(sol_received, f"SELL {position['token_symbol']} ({reason})")

        if sell_pct >= 100 or position["current_amount_token"] - sell_amount_token < 0.001:
            entry_sol = position.get("entry_amount_sol", 0.0) or 0.0
            entry_usd = entry_sol * (position.get("sol_usd_at_entry", 0.0) or 0.0)
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
            f"[EXEC-LIVE] SELL {position['token_symbol']} "
            f"@ ${current_price_usd:.10f} ({sell_pct:.0f}%, {reason}), "
            f"sol_out={sol_received:.4f}, tx={tx_id[:16]}"
        )
        return trade

    async def _execute_sell_paper(self, position: dict, sell_amount_token: float,
                                   sell_pct: float, reason: str,
                                   current_price_usd: float = None) -> Optional[Trade]:
        """Paper sell: simulate using price walk."""
        if current_price_usd is None:
            current_price_usd = await self._simulate_paper_price_walk(position, reason)

        if current_price_usd <= 0:
            if reason == "STUCK":
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

        sol_usd_now = 0.0
        if self.price_oracle:
            try:
                sol_usd_now = await self.price_oracle.get_sol_price_usd()
            except Exception as e:
                logger.debug(f"[EXEC] get_sol_price_usd error: {e}")
        if sol_usd_now <= 0:
            sol_usd_now = position.get("sol_usd_at_entry", 0.0) or 0.0
        if sol_usd_now <= 0:
            sol_usd_now = 150.0

        sol_received = (sell_amount_token * current_price_usd) / sol_usd_now

        prev_total_sold_sol = position.get("total_sold_sol", 0.0) or 0.0
        prev_total_sold_usd = position.get("total_sold_usd", 0.0) or 0.0
        this_sell_usd = sell_amount_token * current_price_usd
        position["total_sold_sol"] = prev_total_sold_sol + sol_received
        position["total_sold_usd"] = prev_total_sold_usd + this_sell_usd

        tx_sig = self.wallet.generate_paper_signature()
        trade = Trade(
            position_id=position["id"],
            side="SELL",
            tx_signature=tx_sig,
            amount_in=sell_amount_token,
            amount_out=sol_received,
            price=current_price_usd,
            slippage_bps=self.slippage_bps,
            status="CONFIRMED",
        )
        await self.positions.record_trade(trade)
        await self.wallet.credit(sol_received, f"SELL {position['token_symbol']} ({reason})")

        if sell_pct >= 100 or position["current_amount_token"] - sell_amount_token < 0.001:
            entry_sol = position.get("entry_amount_sol", 0.0) or 0.0
            entry_usd = entry_sol * (position.get("sol_usd_at_entry", 0.0) or 0.0)
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
            f"[EXEC] PAPER SELL {position['token_symbol']} "
            f"@ ${current_price_usd:.10f} ({sell_pct:.0f}%, {reason}), "
            f"sol_out={sol_received:.4f}, sol_usd={sol_usd_now:.2f}"
        )
        return trade

    def _infer_decimals(self, token: TokenData) -> int:
        try:
            raw = getattr(token, "raw_gmgn", {}) or {}
            decimals = int(raw.get("decimals", 6) or 6)
            if decimals < 0 or decimals > 18:
                return 6
            return decimals
        except Exception:
            return 6

    def _infer_decimals_from_position(self, position: dict) -> int:
        """Infer token decimals from position's raw_gmgn_json. Fallback 6."""
        try:
            raw_json = position.get("raw_gmgn_json", "")
            if raw_json:
                raw = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
                decimals = int(raw.get("decimals", 6) or 6)
                if 0 <= decimals <= 18:
                    return decimals
            return 6
        except Exception:
            return 6
