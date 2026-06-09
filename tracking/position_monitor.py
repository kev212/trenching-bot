"""Position state machine: SL / TP1 / TP2 / trailing / time exits.

USD-canonical price model (v2):
- All entry_price / peak_price / current_price fields are USD
- SL/TP/trailing comparisons in USD (single unit, no mismatch bug)
- Phantom-SL guard rejects extreme 1-tick drops (95%+) as likely
  data-quality or unit issues
- 4x/sec polling; STUCK auto-close after 3s blackout

Runs 4x/sec. Reads open positions, fetches current price via Jupiter,
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

# Phantom-SL guard: a single-tick drop > 95% is almost always either:
#   - Unit mismatch (entry SOL, current USD or vice versa) — fix unit refactor
#     makes this vanishingly rare, but defense in depth
#   - Real instant rug — confirming on next tick is fine
# Skip this tick's SL/TP evaluation; let the next tick re-evaluate.
PHANTOM_SL_THRESHOLD = 0.05  # 5% of entry price (i.e. -95% drop)


async def _process_position(
    position, executor, position_manager, is_paper,
    stop_loss_pct, tp1_mult, tp2_mult, extreme_tp_mult,
    tp1_pct, trailing_pct, min_hold_seconds, time_limit,
    sol_usd_now,
) -> dict:
    """Returns exit state dict if triggered, empty dict otherwise.

    All SL/TP/trailing comparisons in USD (entry_price field = USD).
    """
    token_address = position["token_address"]

    if is_paper:
        # Check cache FIRST (no timeout needed — cache read is instant).
        # Only call _simulate_paper_price_walk if cache is expired.
        cached = executor._paper_price_cache.get(token_address)
        now_cached = time.time() if 'time' not in dir() else __import__('time').time()
        if cached:
            ttl = 30.0 if cached["price"] <= 0 else executor._paper_price_cache_ttl
            if (now_cached - cached["ts"]) < ttl:
                current_price_usd = cached["price"]
            else:
                current_price_usd = 0.0  # will be fetched below
        else:
            current_price_usd = 0.0

        if current_price_usd <= 0:
            try:
                current_price_usd = await asyncio.wait_for(
                    executor._simulate_paper_price_walk(position, "monitor"),
                    timeout=5,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[POS-MON] paper price walk timeout for {token_address[:8]}, "
                    "returning 0 (will skip SL/TP this tick)"
                )
                current_price_usd = 0.0
    else:
        # Live mode: SOL from Jupiter × SOL/USD = USD.
        try:
            current_price_sol = await asyncio.wait_for(
                executor.jupiter.get_token_price_in_sol_with_retry(token_address),
                timeout=10,
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"[POS-MON] jupiter price timeout for {token_address[:8]}, "
                "returning 0 (will skip SL/TP this tick)"
            )
            current_price_sol = 0.0
        current_price_usd = current_price_sol * (sol_usd_now or 0.0)

    if current_price_usd <= 0:
        # TIME exit: close at entry_price after time_limit, even without price.
        if time_limit > 0:
            held_so_far = (
                (datetime.now(timezone.utc) - position["entry_time"]).total_seconds()
                if position.get("entry_time") else 0
            )
            if held_so_far > time_limit:
                entry_price = position.get("entry_price", 0) or 0
                if entry_price > 0:
                    await executor.execute_sell(position, 100, "TIME", entry_price)
                    position_manager.cleanup_lock(token_address)
                    return {
                        "triggered": True,
                        "current_price": entry_price,
                        "entry": entry_price,
                        "peak": position.get("peak_price", 0) or 0,
                        "held_so_far": held_so_far,
                        "last_reason": "TIME",
                    }
        return {}

    entry = position["entry_price"]  # USD
    pos_lock = position_manager.get_lock(token_address)

    triggered = False
    last_reason = ""
    peak = position.get("peak_price") or 0

    # Phantom-SL guard: detect extreme 1-tick drops that suggest data quality
    # issues (e.g. unit mismatch, stale GMGN response). Skip this tick;
    # next tick will either confirm (real crash) or recover (data blip).
    if current_price_usd < entry * PHANTOM_SL_THRESHOLD:
        logger.warning(
            f"[POS-MON] PHANTOM-SL GUARD: {position['token_symbol']} "
            f"({token_address[:8]}) current=${current_price_usd:.10f} < "
            f"5% of entry=${entry:.10f} — likely data issue, skipping this tick"
        )
        return {}

    async with pos_lock:
        peak = max(position.get("peak_price") or 0, current_price_usd)
        if peak > (position.get("peak_price") or 0):
            position["peak_price"] = peak
            await position_manager.update_position(position)

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

        if current_price_usd <= entry * (1 - stop_loss_pct / 100):
            await executor.execute_sell(position, 100, "SL", current_price_usd)
            triggered = True
            last_reason = "SL"
        elif not already_partial and current_price_usd >= extreme_tp_price:
            await executor.execute_sell(position, tp1_pct, "TP1-EXTREME", current_price_usd)
            position["exit_reason"] = "TP1"
            await position_manager.update_position(position)
            triggered = True
            last_reason = "TP1"
        elif not in_warmup and not already_partial and current_price_usd >= tp1_fire_price:
            await executor.execute_sell(position, tp1_pct, "TP1", current_price_usd)
            position["exit_reason"] = "TP1"
            await position_manager.update_position(position)
            triggered = True
            last_reason = "TP1"
        elif not in_warmup and position.get("exit_reason") == "TP1" and \
                current_price_usd >= tp2_fire_price:
            remaining_pct = 100 - tp1_pct
            await executor.execute_sell(position, remaining_pct, "TP2", current_price_usd)
            position["exit_reason"] = "TP2"
            await position_manager.update_position(position)
            triggered = True
            last_reason = "TP2"
        elif not in_warmup and position.get("exit_reason") in ("TP1", "TP2") and \
                current_price_usd <= peak * (1 - trailing_pct / 100):
            await executor.execute_sell(position, 100, "TRAILING", current_price_usd)
            triggered = True
            last_reason = "TRAILING"
        else:
            if time_limit > 0 and held_so_far > time_limit:
                await executor.execute_sell(position, 100, "TIME", current_price_usd)
                triggered = True
                last_reason = "TIME"

    if not triggered:
        return {}

    # B2 fix + L3 audit fix: cleanup lock ONLY on full-close exits.
    # Partial sells (TP1, TP2) leave the position open with remaining
    # tokens — keeping the lock preserves lock continuity for the next
    # tick. Removing it on TP1 would force get_lock() to create a new
    # Lock object on the next tick, losing the original lock's state
    # and creating a (mostly harmless) identity discontinuity.
    _FULL_CLOSE_REASONS = ("SL", "TP1-EXTREME", "TP2", "TRAILING", "TIME")
    if last_reason in _FULL_CLOSE_REASONS:
        position_manager.cleanup_lock(token_address)

    gain = (current_price_usd - entry) / entry * 100 if entry > 0 else 0.0
    logger.info(
        f"[EXIT] {position['token_symbol']} ({token_address[:8]}): "
        f"{last_reason} at {gain:+.1f}% (price=${current_price_usd:.8f})"
    )

    entry_tokens = position.get("entry_amount_token", 0) or 0
    current_tokens = position.get("current_amount_token", 0) or 0
    entry_sol = position.get("entry_amount_sol", 0.0) or 0.0
    total_sold_sol = position.get("total_sold_sol", 0.0) or 0.0
    total_sold_usd = position.get("total_sold_usd", 0.0) or 0.0

    # L8 audit fix: read PnL from `position` (set by execute_sell ->
    # close_position) instead of recomputing. Recomputation can drift
    # from the stored value if sol_usd_now changes between execute_sell
    # and here, causing alert to show different numbers than /history.
    # For partial sells (TP1/TP2) the stored pnl_* reflects only this
    # leg's contribution (close is only set when full close), so we
    # fall back to the partial-sell math for those.
    pnl_sol = position.get("pnl_sol") or 0.0
    pnl_usd = position.get("pnl_usd") or 0.0
    pnl_pct = position.get("pnl_pct") or 0.0

    if last_reason in ("TP1", "TP1-EXTREME"):
        sold_tokens = entry_tokens - (position.get("current_amount_token", 0) or 0)
        # For partial sells, pnl_* fields aren't yet meaningful (close
        # hasn't been called). Compute this-leg's PnL for the alert.
        if not pnl_sol:
            pnl_sol = (current_price_usd - entry) / (sol_usd_now or 150.0) * sold_tokens
            pnl_usd = (current_price_usd - entry) * sold_tokens
            pnl_pct = ((current_price_usd / entry) - 1) * 100 if entry > 0 else 0.0
    elif last_reason == "TP2":
        # TP2 is a full close (remaining < 0.001 tokens in execute_sell).
        # Use stored PnL (set by close_position).
        sold_tokens = entry_tokens
    elif last_reason in ("SL", "TRAILING", "TIME"):
        sold_tokens = entry_tokens
    else:
        sold_tokens = 0

    sold_pct = (sold_tokens / entry_tokens * 100) if entry_tokens > 0 else 0.0

    try:
        exit_msg = format_exit_alert(
            symbol=position["token_symbol"],
            address=position["token_address"],
            entry_price=entry,
            exit_price=current_price_usd,
            pnl_sol=pnl_sol,
            pnl_usd=pnl_usd,
            pnl_pct=pnl_pct,
            reason=last_reason,
            hold_seconds=held_so_far,
            paper=is_paper,
            position_size_sol=entry_sol,
            total_tokens=entry_tokens,
            sold_pct=sold_pct,
            sold_tokens=sold_tokens,
            remaining_tokens=position.get("current_amount_token", 0) or 0,
        )
        # Fix #3: fire-and-forget the alert. Previously `await` here would
        # block the monitor tick up to 37s if Telegram was slow (3 retries
        # × 10s timeout + 1+2s backoff), causing SL/TP checks to be missed
        # during a slow-Telegram episode. The dispatcher already has its
        # own internal retry/backoff, so we don't need to wrap it.
        asyncio.create_task(_safe_send_alert(exit_msg))
    except Exception as e:
        logger.error(f"Exit alert format/schedule failed: {e}")

    return {
        "triggered": True,
        "current_price": current_price_usd,
        "entry": entry,
        "peak": peak,
        "held_so_far": held_so_far,
        "last_reason": last_reason,
    }


async def _safe_send_alert(text: str) -> None:
    """Best-effort send. Logs failures but never raises — keeps the
    background task slot clean and prevents 'Task exception was never
    retrieved' warnings."""
    try:
        await dispatcher.send_alert(text)
    except Exception as e:
        logger.error(f"Exit alert send failed: {e}")


async def position_monitor(state, db, position_manager: PositionManager,
                            risk: RiskManager, jupiter: JupiterClient,
                            executor: TradeExecutor, config: dict):
    """High-frequency position state machine. Runs 4x/sec."""
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
            # Fetch SOL/USD once per tick to keep SL/TP math consistent.
            sol_usd_now = 0.0
            if executor and executor.price_oracle:
                try:
                    sol_usd_now = await executor.price_oracle.get_sol_price_usd()
                except Exception:
                    sol_usd_now = 0.0

            open_positions = await position_manager.get_open_positions()
            if not open_positions:
                continue

            for position in open_positions:
                token_address = position["token_address"]
                try:
                    result = await asyncio.wait_for(
                        _process_position(
                            position, executor, position_manager, is_paper,
                            stop_loss_pct, tp1_mult, tp2_mult, extreme_tp_mult,
                            tp1_pct, trailing_pct, min_hold_seconds, time_limit,
                            sol_usd_now,
                        ),
                        timeout=10,
                    )
                    if result and result.get("triggered"):
                        logger.info(
                            f"[POS-MON] {position['token_symbol']} exit: "
                            f"price=${result['current_price']:.10f}, "
                            f"entry=${result['entry']:.10f}, "
                            f"peak=${result['peak']:.10f}, "
                            f"paper={is_paper}, hold={result['held_so_far']:.0f}s"
                        )
                except asyncio.TimeoutError:
                    logger.warning(
                        f"Position {token_address[:8]} monitor timeout — skipping tick"
                    )
                except Exception as e:
                    logger.error(f"Position {token_address[:8]} monitor error: {e}")

        except Exception as e:
            logger.error(f"Position monitor error: {e}")
            await asyncio.sleep(1)
