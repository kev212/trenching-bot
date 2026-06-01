"""Trade executor: orchestrates buy/sell flows with Jupiter quotes.

Paper mode (Phase 1): builds positions, records trades, debits/credits
simulated wallet. No real transactions.

Live mode (Phase 2): also builds, signs, submits via Helius RPC.
"""
import logging
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
                 positions: PositionManager, risk: RiskManager, config: dict):
        self.paper = paper
        self.wallet = wallet
        self.jupiter = jupiter
        self.positions = positions
        self.risk = risk
        self.config = config
        self.max_price_impact_pct = config.get("max_price_impact_pct", 5.0)
        self.slippage_bps = config.get("slippage_bps", DEFAULT_SLIPPAGE_BPS)

    async def execute_buy(self, token: TokenData, size_sol: float,
                          filter_params_version: int = 0) -> Optional[Position]:
        """Validate, get quote, simulate swap, create Position. Returns Position or None."""
        if size_sol <= 0:
            return None

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
                    f"symbol={token.symbol}, size={size_sol:.4f} SOL, "
                    f"action=BUY_SKIPPED, paper={self.paper}"
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

        if not await self.wallet.debit(size_sol, f"BUY {token.symbol}"):
            logger.warning(f"[EXEC] Insufficient balance for {token.symbol}")
            return None

        token_decimals = self._infer_decimals(token)
        amount_token = out_amount / (10 ** token_decimals)
        entry_price = size_sol / amount_token if amount_token > 0 else 0.0

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

        logger.info(
            f"[EXEC] {'PAPER ' if self.paper else ''}BUY {token.symbol} "
            f"@ {entry_price:.10f} SOL, size={size_sol:.4f} SOL, "
            f"tokens={amount_token:.2f}, impact={price_impact:.2f}%"
        )
        return position

    async def execute_sell(self, position: dict, sell_pct: float,
                            reason: str) -> Optional[Trade]:
        """Sell `sell_pct`% of position. Returns Trade or None.

        `position` is the dict shape returned by get_open_positions.
        """
        if position.get("status") != "OPEN":
            return None
        if sell_pct <= 0 or sell_pct > 100:
            return None

        current_price = await self.jupiter.get_token_price_in_sol_with_retry(
            position["token_address"]
        )
        if current_price <= 0:
            logger.warning(
                f"[EXEC] SELL-SKIPPED {position['token_symbol']} "
                f"({position['token_address'][:8]}): no price after retries, "
                f"sell_pct={sell_pct:.0f}%, reason={reason}"
            )
            if self.positions.db:
                await self.positions.db.save_risk_event(
                    "JUPITER_PRICE_FAILED", position["token_address"],
                    f"symbol={position['token_symbol']}, reason={reason}, "
                    f"action=SELL_SKIPPED, paper={self.paper}"
                )
            return None

        sell_amount_token = position["current_amount_token"] * (sell_pct / 100)
        sol_received = sell_amount_token * current_price

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
            pnl_sol = sol_received - position["entry_amount_sol"]
            pnl_pct = (current_price / position["entry_price"] - 1) * 100 if position["entry_price"] > 0 else 0.0
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
