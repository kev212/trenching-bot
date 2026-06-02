"""Position state machine: SL / TP1 / TP2 / trailing / time exits.

Runs 4×/sec. Reads open positions, fetches current price via Jupiter,
advances each position toward its exit trigger. Sends Telegram alerts
on every exit.
"""
import asyncio
import logging
from datetime import datetime, timezone

from core.jupiter_client import JupiterClient
from core.position_manager import PositionManager
from core.risk_manager import RiskManager
from core.trade_executor import TradeExecutor
from alerts.dispatcher import dispatcher
from alerts.formatter import format_exit_alert

logger = logging.getLogger("position_monitor")


async def position_monitor(state, db, position_manager: PositionManager,
                            risk: RiskManager, jupiter: JupiterClient,
                            executor: TradeExecutor, config: dict):
    """High-frequency position state machine. Runs 4×/sec.

    Paper mode: re-fetches GMGN price for the token to evaluate triggers.
    Live mode: uses Jupiter (with retry) for real prices.
    """
    logger.info("Position monitor started")
    check_interval = 0.25

    stop_loss_pct = config.get("stop_loss_pct", 30)
    tp1_mult = config.get("tp1_multiplier", 1.30)
    tp1_pct = config.get("tp1_sell_pct", 33)
    tp2_mult = config.get("tp2_multiplier", 1.50)
    trailing_pct = config.get("trailing_stop_pct", 15)
    time_limit = config.get("time_limit_seconds", 1800)
    is_paper = config.get("paper_mode", True)
    min_hold_seconds = config.get("min_hold_seconds", 30)
    extreme_tp_mult = config.get("extreme_tp_mult", 2.0)

    while True:
        await asyncio.sleep(check_interval)
        try:
            open_positions = await position_manager.get_open_positions()
            if not open_positions:
                continue

            for position in open_positions:
                token_address = position["token_address"]

                if is_paper:
                    current_price = await executor._simulate_paper_price_walk(
                        position, "monitor"
                    )
                else:
                    current_price = await jupiter.get_token_price_in_sol_with_retry(
                        token_address
                    )

                if current_price <= 0:
                    continue

                entry = position["entry_price"]
                peak = max(position["peak_price"] or 0, current_price)
                if peak > (position["peak_price"] or 0):
                    position["peak_price"] = peak
                    await position_manager.update_position(position)

                triggered = False
                last_reason = ""
                already_partial = bool(position.get("exit_reason")) and \
                    position.get("exit_reason") in ("TP1", "TP2")

                held_so_far = (
                    (datetime.now(timezone.utc) - position["entry_time"]).total_seconds()
                    if position.get("entry_time") else 0
                )
                in_warmup = held_so_far < min_hold_seconds

                tp1_fire_price = entry * tp1_mult
                tp2_fire_price = entry * tp2_mult
                extreme_tp_price = entry * extreme_tp_mult

                if current_price <= entry * (1 - stop_loss_pct / 100):
                    await executor.execute_sell(position, 100, "SL")
                    triggered = True
                    last_reason = "SL"
                elif not already_partial and current_price >= extreme_tp_price:
                    await executor.execute_sell(position, tp1_pct, "TP1-EXTREME")
                    position["exit_reason"] = "TP1"
                    await position_manager.update_position(position)
                    triggered = True
                    last_reason = "TP1"
                elif not in_warmup and not already_partial and current_price >= tp1_fire_price:
                    await executor.execute_sell(position, tp1_pct, "TP1")
                    position["exit_reason"] = "TP1"
                    await position_manager.update_position(position)
                    triggered = True
                    last_reason = "TP1"
                elif not in_warmup and position.get("exit_reason") == "TP1" and \
                        current_price >= tp2_fire_price:
                    remaining_pct = 100 - tp1_pct
                    await executor.execute_sell(position, remaining_pct, "TP2")
                    position["exit_reason"] = "TP2"
                    await position_manager.update_position(position)
                    triggered = True
                    last_reason = "TP2"
                elif not in_warmup and position.get("exit_reason") in ("TP1", "TP2") and \
                        current_price <= peak * (1 - trailing_pct / 100):
                    await executor.execute_sell(position, 100, "TRAILING")
                    triggered = True
                    last_reason = "TRAILING"
                else:
                    if time_limit > 0 and held_so_far > time_limit:
                        await executor.execute_sell(position, 100, "TIME")
                        triggered = True
                        last_reason = "TIME"

                if triggered:
                    logger.info(
                        f"[POS-MON] {position['token_symbol']} exit: "
                        f"price={current_price:.10f}, entry={entry:.10f}, "
                        f"peak={peak:.10f}, paper={is_paper}, hold={held_so_far:.0f}s"
                    )
                    try:
                        entry_tokens = position.get("entry_amount_token", 0) or 0
                        current_tokens = position.get("current_amount_token", 0) or 0
                        if last_reason == "TP1":
                            pre_sell_tokens = entry_tokens
                            sold_tokens = entry_tokens - current_tokens
                            sold_pct = (sold_tokens / pre_sell_tokens * 100) if pre_sell_tokens > 0 else 0
                            pnl_sol = (current_price - entry) * sold_tokens
                            pnl_pct = ((current_price / entry) - 1) * 100 if entry > 0 else 0.0
                        elif last_reason == "TP2":
                            pre_sell_tokens = current_tokens
                            sold_tokens = pre_sell_tokens - (position.get("current_amount_token", 0) or 0)
                            sold_pct = (sold_tokens / pre_sell_tokens * 100) if pre_sell_tokens > 0 else 0
                            pnl_sol = (current_price - entry) * sold_tokens
                            pnl_pct = ((current_price / entry) - 1) * 100 if entry > 0 else 0.0
                        elif last_reason in ("SL", "TRAILING", "TIME"):
                            total_sold = position.get("total_sold_sol", 0.0) or 0.0
                            entry_sol = position.get("entry_amount_sol", 0.0) or 0.0
                            pnl_sol = total_sold - entry_sol
                            pnl_pct = ((total_sold / entry_sol) - 1) * 100 if entry_sol > 0 else 0.0
                            pre_sell_tokens = entry_tokens
                            sold_tokens = entry_tokens
                            sold_pct = 100.0
                        else:
                            pre_sell_tokens = 0
                            sold_tokens = 0
                            sold_pct = 0.0
                            pnl_sol = 0.0
                            pnl_pct = 0.0
                        exit_msg = format_exit_alert(
                            symbol=position["token_symbol"],
                            address=position["token_address"],
                            entry_price=entry,
                            exit_price=current_price,
                            pnl_sol=pnl_sol,
                            pnl_pct=pnl_pct,
                            reason=last_reason or "TIME",
                            hold_seconds=(datetime.now(timezone.utc) - position["entry_time"]).total_seconds()
                                if position.get("entry_time") else 0,
                            paper=is_paper,
                            position_size_sol=position.get("entry_amount_sol", 0) or 0,
                            total_tokens=entry_tokens,
                            sold_pct=sold_pct,
                            sold_tokens=sold_tokens,
                            remaining_tokens=current_tokens if last_reason in ("TP1", "TP2") else 0,
                        )
                        await dispatcher.send_alert(exit_msg)
                    except Exception as e:
                        logger.error(f"[POS-MON] exit alert send failed: {e}")

        except Exception as e:
            logger.error(f"Position monitor error: {e}")
            await asyncio.sleep(1)
