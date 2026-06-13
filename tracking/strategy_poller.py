"""GMGN strategy (condition order) poller for live positions.

When a buy is submitted via `gmgn-cli swap --condition-orders ...`, GMGN
attaches TP1/TP2/SL orders on-chain. We don't need a 4x/sec price monitor;
we just poll GMGN for the strategy status and update position state.

Flow:
  1. Find all OPEN live positions with `strategy_order_id` set
  2. For each, call `gmgn-cli order get --order-id <strategy_id>`
  3. If TP1/TP2/SL filled: update position flag, sync amount, log exit
  4. When fully exited (TP1+TP2 or SL), close position

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

    # Check each condition order's status
    conditions = matching.get("condition_orders", []) or []
    filled_tp1 = False
    filled_tp2 = False
    filled_sl = False
    for c in conditions:
        ctype = c.get("order_type", "")
        cstatus = c.get("status", "")
        if cstatus in ("filled", "success", "completed", "triggered"):
            if ctype == "profit_stop":
                ps = int(c.get("price_scale", "0"))
                if ps <= 150:
                    filled_tp1 = True
                else:
                    filled_tp2 = True
            elif ctype == "loss_stop":
                filled_sl = True

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

    # If strategy is fully exited, close the position
    fully_exited = (filled_tp1 and filled_tp2) or filled_sl
    strategy_done = strategy_status in ("closed", "cancelled", "finished", "completed")
    if fully_exited or strategy_done or strategy_state in ("finished", "closed"):
        if fully_exited:
            exit_reason = "TP1+TP2" if (filled_tp1 and filled_tp2) else "SL"
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
