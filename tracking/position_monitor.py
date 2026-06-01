"""Position state machine: SL / TP1 / TP2 / trailing / time exits.

Runs 4×/sec. Reads open positions, fetches current price via Jupiter,
advances each position toward its exit trigger.
"""
import asyncio
import logging
from datetime import datetime, timezone

from core.jupiter_client import JupiterClient
from core.position_manager import PositionManager
from core.risk_manager import RiskManager
from core.trade_executor import TradeExecutor

logger = logging.getLogger("position_monitor")


async def position_monitor(state, db, position_manager: PositionManager,
                            risk: RiskManager, jupiter: JupiterClient,
                            executor: TradeExecutor, config: dict):
    """High-frequency position state machine. Runs 4×/sec."""
    logger.info("Position monitor started")
    check_interval = 0.25

    stop_loss_pct = config.get("stop_loss_pct", 30)
    tp1_mult = config.get("tp1_multiplier", 1.30)
    tp1_pct = config.get("tp1_sell_pct", 33)
    tp2_mult = config.get("tp2_multiplier", 1.50)
    trailing_pct = config.get("trailing_stop_pct", 15)
    time_limit = config.get("time_limit_seconds", 1800)

    while True:
        await asyncio.sleep(check_interval)
        try:
            open_positions = await position_manager.get_open_positions()
            if not open_positions:
                continue

            for position in open_positions:
                token_address = position["token_address"]
                current_price = await jupiter.get_token_price_in_sol(token_address)
                if current_price <= 0:
                    continue

                entry = position["entry_price"]
                peak = max(position["peak_price"] or 0, current_price)
                if peak > (position["peak_price"] or 0):
                    position["peak_price"] = peak
                    await position_manager.update_position(position)

                triggered = False
                if current_price <= entry * (1 - stop_loss_pct / 100):
                    await executor.execute_sell(position, 100, "SL")
                    triggered = True
                elif position.get("exit_reason") in (None, "", "TP1") and \
                        current_price >= entry * tp1_mult:
                    await executor.execute_sell(position, tp1_pct, "TP1")
                    triggered = True
                elif position.get("exit_reason") == "TP1" and \
                        current_price >= entry * tp2_mult:
                    remaining_pct = 100 - tp1_pct
                    await executor.execute_sell(position, remaining_pct, "TP2")
                    triggered = True
                elif current_price <= peak * (1 - trailing_pct / 100):
                    await executor.execute_sell(position, 100, "TRAILING")
                    triggered = True
                else:
                    held = (datetime.now(timezone.utc) - position["entry_time"]).total_seconds()
                    if held > time_limit:
                        await executor.execute_sell(position, 100, "TIME")
                        triggered = True

                if triggered:
                    logger.info(
                        f"[POS-MON] {position['token_symbol']} exit: "
                        f"price={current_price:.10f}, entry={entry:.10f}, "
                        f"peak={peak:.10f}"
                    )

        except Exception as e:
            logger.error(f"Position monitor error: {e}")
            await asyncio.sleep(1)
