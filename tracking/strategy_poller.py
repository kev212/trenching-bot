"""GMGN strategy (condition order) poller for live positions.

When a buy is submitted via `gmgn-cli swap --condition-orders ... --sell-ratio-type hold_amount`,
GMGN attaches 4 on-chain orders: TP1 (75%), TP2 (100% remaining), Trailing (30% drawdown), SL (100%).
We don't need a 4x/sec price monitor; we just poll GMGN for the strategy
status and update position state.

Flow:
  1. Find all OPEN live positions
  2. For each, call `gmgn-cli order strategy list --group-tag STMix`
  3. If any condition filled: update position flag, log exit
  4. When fully exited (TP1+TP2 cascade, trailing, or SL), close position

The poller runs every `POLL_INTERVAL_S` seconds (default 15s) — much
slower than position_monitor's 0.25s because GMGN handles the timing.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from core.gmgn_cli import GMGNCli
from core.position_manager import PositionManager

logger = logging.getLogger("strategy_poller")

POLL_INTERVAL_S = 15.0


async def strategy_poller(db, position_manager: PositionManager, gmgn_cli: GMGNCli):
    """Poll GMGN for live strategy (condition order) status.

    Updates position.tp1_filled/tp2_filled/sl_filled flags and closes
    the position when fully exited.
    """
    if not gmgn_cli or not gmgn_cli.is_ready():
        logger.warning("[STRATEGY-POLLER] gmgn_cli not ready, poller exiting")
        return

    logger.info("[STRATEGY-POLLER] started, poll_interval=%.0fs", POLL_INTERVAL_S)

    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL_S)
            await _tick(db, position_manager, gmgn_cli)
        except asyncio.CancelledError:
            logger.info("[STRATEGY-POLLER] cancelled, exiting")
            raise
        except Exception as e:
            logger.error(f"[STRATEGY-POLLER] tick error: {e}", exc_info=True)
            await asyncio.sleep(5)


async def _tick(db, position_manager: PositionManager, gmgn_cli: GMGNCli) -> None:
    """One poll cycle: check all open live positions.

    For each live position, fetch the GMGN strategy list filtered by
    base_token (the token we bought). Then check the condition_orders[].status
    field of each matching strategy.
    """
    try:
        positions = await position_manager.get_open_positions()
    except Exception as e:
        logger.warning(f"[STRATEGY-POLLER] get_open_positions failed: {e}")
        return

    live_positions = [
        p for p in positions
        if not (p.get("paper", 1))
    ]

    if not live_positions:
        return

    logger.debug(
        f"[STRATEGY-POLLER] checking {len(live_positions)} live positions"
    )

    wallet = await _get_wallet_address(gmgn_cli)
    if not wallet:
        return

    for pos in live_positions:
        await _check_one(pos, position_manager, gmgn_cli, wallet)


async def _get_wallet_address(gmgn_cli: GMGNCli) -> str:
    """Cache the GMGN wallet address once per tick."""
    try:
        return await gmgn_cli.get_wallet_address("sol")
    except Exception as e:
        logger.warning(f"[STRATEGY-POLLER] get_wallet_address failed: {e}")
        return ""


async def _check_one(pos: dict, position_manager: PositionManager, gmgn_cli: GMGNCli,
                      wallet: str) -> None:
    token_address = pos.get("token_address", "")
    if not token_address:
        return

    try:
        result = await gmgn_cli.list_strategies(
            chain="sol", from_addr=wallet, base_token=token_address,
        )
    except Exception as e:
        logger.warning(
            f"[STRATEGY-POLLER] list_strategies failed for {token_address[:8]}: {e}"
        )
        return

    # Find the matching strategy
    strategy_list = result.get("list", [])
    matching = None
    for s in strategy_list:
        if (s.get("base_token") == token_address
                and s.get("status") in ("open", "running")):
            matching = s
            break

    if not matching:
        return

    # Check each condition order's status.
    # Strategy: TP1 (1.5x sell 75%) + TP2 (2.0x sell 100% remaining) +
    # Trailing (1.5x activation, 30% drawdown) + SL (0.5x sell 100%).
    # With --sell-ratio-type hold_amount, TP2 sells remaining 25% after TP1.
    # Detection: smallest-scale profit_stop = TP1, largest = TP2.
    # This is config-agnostic — works regardless of TP1/TP2 values.
    conditions = matching.get("condition_orders", []) or []
    profit_stops: list[tuple[int, dict]] = []
    filled_trailing = False
    filled_sl = False
    for c in conditions:
        ctype = c.get("order_type", "")
        cstatus = c.get("status", "")
        if ctype == "profit_stop_trace":
            if cstatus in ("filled", "success", "completed", "triggered"):
                filled_trailing = True
        elif ctype == "loss_stop":
            if cstatus in ("filled", "success", "completed", "triggered"):
                filled_sl = True
        elif ctype == "profit_stop":
            ps = int(c.get("price_scale", "0"))
            is_filled = cstatus in ("filled", "success", "completed", "triggered")
            profit_stops.append((ps, {"filled": is_filled, "raw": c}))

    # Identify TP1 (smallest price_scale) and TP2 (largest, if different)
    filled_tp1 = False
    filled_tp2 = False
    if profit_stops:
        profit_stops.sort(key=lambda x: x[0])
        smallest_scale, smallest_info = profit_stops[0]
        if smallest_info["filled"]:
            filled_tp1 = True
        if len(profit_stops) > 1:
            largest_scale, largest_info = profit_stops[-1]
            if largest_scale > smallest_scale and largest_info["filled"]:
                filled_tp2 = True

    strategy_status = matching.get("status", "open")
    strategy_state = matching.get("strategy_status", "")

    updated = False
    if filled_tp1 and not pos.get("tp1_filled", 0):
        pos["tp1_filled"] = 1
        updated = True
        logger.info(
            f"[STRATEGY-POLLER] TP1 filled: {pos.get('token_symbol','?')} "
            f"({pos.get('token_address','')[:8]})"
        )
    if filled_tp2 and not pos.get("tp2_filled", 0):
        pos["tp2_filled"] = 1
        updated = True
        logger.info(
            f"[STRATEGY-POLLER] TP2 filled: {pos.get('token_symbol','?')} "
            f"({pos.get('token_address','')[:8]})"
        )
    if filled_trailing and not pos.get("trailing_filled", 0):
        pos["trailing_filled"] = 1
        updated = True
        logger.info(
            f"[STRATEGY-POLLER] Trailing filled: {pos.get('token_symbol','?')} "
            f"({pos.get('token_address','')[:8]})"
        )
    if filled_sl and not pos.get("sl_filled", 0):
        pos["sl_filled"] = 1
        updated = True
        logger.info(
            f"[STRATEGY-POLLER] SL filled: {pos.get('token_symbol','?')} "
            f"({pos.get('token_address','')[:8]})"
        )

    if updated:
        try:
            await position_manager.update_position(pos)
        except Exception as e:
            logger.warning(f"[STRATEGY-POLLER] update_position failed: {e}")

    # If strategy is fully exited, close the position.
    # With hold_amount, TP1 (75%) + TP2 (100% remaining) = 100% exit.
    # Trailing also exits 100% of remaining on drawdown.
    # SL exits 100% of position.
    fully_exited = (filled_tp1 and filled_tp2) or filled_trailing or filled_sl
    strategy_done = strategy_status in ("closed", "cancelled", "finished", "completed")
    if fully_exited or strategy_done or strategy_state in ("finished", "closed"):
        if (filled_tp1 and filled_tp2) or filled_trailing or filled_sl:
            reasons = []
            if filled_tp1: reasons.append("TP1")
            if filled_tp2: reasons.append("TP2")
            if filled_trailing: reasons.append("TRAIL")
            if filled_sl: reasons.append("SL")
            exit_reason = "+".join(reasons)
        else:
            exit_reason = f"GMGN:{strategy_status}"
        try:
            await position_manager.close_position(
                pos,
                exit_reason=exit_reason,
                exit_price=0.0,
                pnl_sol=0.0,
                pnl_pct=0.0,
                pnl_usd=0.0,
            )
            logger.info(
                f"[STRATEGY-POLLER] Position closed: {pos.get('token_symbol','?')} "
                f"reason={exit_reason}"
            )
        except Exception as e:
            logger.error(f"[STRATEGY-POLLER] close_position failed: {e}")
