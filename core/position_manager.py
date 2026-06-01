"""Position lifecycle: open, update, close, persist.

Wraps the positions table. The position_monitor calls these to
advance position state (open → TP1 partial → TP2 partial → trailing close).
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from storage.database import Database

logger = logging.getLogger("position_manager")


class PositionManager:
    """Manages open/closed positions. Persists to DB."""

    def __init__(self, db: Database):
        self.db = db

    async def open_position(self, position) -> int:
        """Save new position, return its DB id."""
        position.id = await self.db.save_position(position)
        logger.info(
            f"[POS] OPEN {position.token_symbol} ({position.token_address[:8]}): "
            f"entry={position.entry_price:.8f} SOL, "
            f"size={position.entry_amount_sol:.4f} SOL, "
            f"tokens={position.entry_amount_token:.2f}, "
            f"{'PAPER' if position.paper else 'LIVE'}"
        )
        return position.id

    async def update_position(self, position) -> None:
        """Persist mutable fields (peak_price, current_amount_token, etc)."""
        await self.db.update_position(position)

    async def record_partial_sell(self, position, sold_amount_token: float,
                                    remaining_amount_token: float) -> None:
        """Update current_amount_token after a partial sell (TP1/TP2)."""
        position.current_amount_token = remaining_amount_token
        await self.db.update_position(position)
        logger.info(
            f"[POS] PARTIAL SELL {position.token_symbol}: "
            f"remaining={remaining_amount_token:.2f} tokens"
        )

    async def close_position(self, position, exit_reason: str,
                              exit_price: float, pnl_sol: float, pnl_pct: float) -> None:
        """Mark position as CLOSED and persist final PnL."""
        now = datetime.now(timezone.utc)
        position.status = "CLOSED"
        position.exit_reason = exit_reason
        position.exit_price = exit_price
        position.exit_time = now
        position.pnl_sol = pnl_sol
        position.pnl_pct = pnl_pct
        position.hold_seconds = int(
            (now - position.entry_time).total_seconds()
        ) if position.entry_time else 0
        await self.db.update_position(position)
        logger.info(
            f"[POS] CLOSE {position.token_symbol} ({position.token_address[:8]}): "
            f"reason={exit_reason}, exit={exit_price:.8f}, "
            f"pnl={pnl_sol:+.4f} SOL ({pnl_pct:+.1f}%), "
            f"hold={position.hold_seconds}s"
        )

    async def get_open_positions(self) -> list[dict]:
        return await self.db.get_open_positions()

    async def get_open_positions_summary(self) -> list[dict]:
        """Return compact view of open positions for logging/Telegram."""
        positions = await self.db.get_open_positions()
        summary = []
        for p in positions:
            entry = p.get("entry_price", 0) or 0
            peak = p.get("peak_price", 0) or 0
            if entry > 0 and peak > 0:
                peak_gain_pct = (peak / entry - 1) * 100
            else:
                peak_gain_pct = 0.0
            summary.append({
                "id": p["id"],
                "symbol": p["token_symbol"],
                "address": p["token_address"][:8] + "...",
                "entry_sol": p.get("entry_amount_sol", 0) or 0,
                "entry_price": entry,
                "peak_gain_pct": peak_gain_pct,
                "tokens": p.get("current_amount_token", 0) or 0,
                "age_sec": (
                    (datetime.now(timezone.utc) - p["entry_time"]).total_seconds()
                    if p.get("entry_time") else 0
                ),
                "status": p.get("status", "?"),
                "paper": bool(p.get("paper", 1)),
            })
        return summary

    async def record_trade(self, trade) -> int:
        return await self.db.save_trade(trade)
