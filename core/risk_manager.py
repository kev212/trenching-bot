"""Risk management: position sizing, daily loss limits, loss-streak halt.

State is in-memory (resets on restart). Persistence of risk events goes
through Database.save_risk_event(). Phase 1 paper mode: limits are
advisory + logged, not enforced against real funds.
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple

logger = logging.getLogger("risk")


class RiskManager:
    """In-memory risk state. Single bot = single RiskManager."""

    def __init__(self, config: dict, db=None):
        self.config = config
        self.db = db
        self.daily_pnl: float = 0.0
        self.loss_streak: int = 0
        self.halted_until: Optional[datetime] = None
        self.last_reset_date: datetime = datetime.now(timezone.utc).date()

    def can_trade(self) -> Tuple[bool, str]:
        """Check all risk gates. Returns (allowed, reason)."""
        self._maybe_reset_daily()
        if self.halted_until and datetime.now(timezone.utc) < self.halted_until:
            remaining = (self.halted_until - datetime.now(timezone.utc)).total_seconds() / 60
            return False, f"Halted for {remaining:.0f}m more (loss streak)"
        if self.daily_pnl <= -self.config.get("daily_loss_limit_sol", 0.5):
            return False, f"Daily loss limit hit: {self.daily_pnl:.3f} SOL"
        return True, "OK"

    def get_position_size(self, balance: float) -> float:
        """Return SOL amount for the next trade (capped, floored)."""
        mode = self.config.get("sizing_mode", "fixed")
        if mode == "fixed":
            size = self.config.get("position_size_sol", 0.05)
        elif mode == "balance_pct":
            pct = self.config.get("position_size_pct", 0.02)
            size = balance * pct
        else:
            size = 0.05
        size = max(self.config.get("min_position_sol", 0.02), size)
        size = min(size, self.config.get("max_position_sol", 0.5))
        size = min(size, balance - 0.1)
        return max(0.0, size)

    def record_trade_result(self, pnl_sol: float) -> None:
        """Update daily PnL, loss streak. Halt if streak threshold hit."""
        self.daily_pnl += pnl_sol
        if pnl_sol < 0:
            self.loss_streak += 1
            if self.loss_streak >= self.config.get("loss_streak_halt", 3):
                hours = self.config.get("loss_streak_halt_hours", 1)
                self.halted_until = datetime.now(timezone.utc) + timedelta(hours=hours)
                logger.warning(
                    f"[RISK] Loss streak {self.loss_streak} hit, halted for {hours}h"
                )
                if self.db:
                    asyncio_create_task_safe(
                        self.db.save_risk_event(
                            "LOSS_STREAK_HALT", "", f"streak={self.loss_streak}, halt_until={self.halted_until.isoformat()}"
                        )
                    )
        else:
            self.loss_streak = 0

    def _maybe_reset_daily(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self.last_reset_date:
            logger.info(f"[RISK] Daily reset, prev PnL={self.daily_pnl:.4f} SOL")
            self.daily_pnl = 0.0
            self.last_reset_date = today


def asyncio_create_task_safe(coro):
    """Schedule a coroutine without awaiting (best-effort, fire-and-forget)."""
    import asyncio
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(coro)
    except RuntimeError:
        pass
