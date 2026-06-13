import asyncio
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import aiohttp
from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from config import settings

logger = logging.getLogger("main")

_bot_app: Application = None


def _is_authorized(update: Update) -> bool:
    """Single-user auth: only settings.telegram_chat_id may issue commands."""
    if not settings.telegram_chat_id:
        return False  # Refuse everything if no allowlist configured
    chat = update.effective_chat
    return bool(chat and str(chat.id) == str(settings.telegram_chat_id))


def _require_auth(func):
    """Decorator: silently reject commands from non-authorized chats.
    Logs every rejected attempt at warning level for audit trail."""
    import functools

    @functools.wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _is_authorized(update):
            chat_id = update.effective_chat.id if update.effective_chat else "?"
            user_id = update.effective_user.id if update.effective_user else "?"
            logger.warning(
                f"[AUTH] Rejected command from chat_id={chat_id} user_id={user_id}"
            )
            return  # Silent — don't leak that the bot exists or what commands it has
        return await func(update, ctx)

    return wrapper


def _fmt_uptime(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


@_require_auth
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else "?"
    logger.info(f"[START] chat={update.effective_chat.id if update.effective_chat else '?'} user={user_id}")
    await update.message.reply_text(
        "🔥 Trenching Bot ACTIVE\n\n"
        "Commands:\n"
        "/stats - Performance metrics\n"
        "/status - Bot status & queue\n"
        "/active - Tracked alert calls\n"
        "/positions - Open trading positions (paper)\n"
        "/history - Closed positions with PnL\n"
        "/pnl - PnL summary + wallet balance\n"
        "/filter - Current filter params\n"
        "/queue - Queue size\n"
        "/recent - Last 10 calls\n"
        "/best - Best performing tokens\n"
        "/live_status - Live trading status\n"
        "/live_pause - Pause live trading\n"
        "/live_resume - Resume live trading\n"
        "/close_all - Close all live positions\n"
        "/ping - Check bot alive\n"
        "/help - Show this message"
    )


@_require_auth
async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = ctx.bot_data.get("state")
    db = ctx.bot_data.get("db")
    if not state or not db:
        await update.message.reply_text("Bot not ready yet.")
        return

    m = state.metrics
    total_resolved = m.wins + m.losses
    win_rate = (m.wins / total_resolved * 100) if total_resolved > 0 else 0.0

    text = (
        "📊 STATS\n\n"
        f"Total processed: {m.calls_total}\n"
        f"APE: {m.calls_ape} | WATCH: {m.calls_watch} | SKIP: {m.calls_skip}\n"
        f"WIN: {m.wins} | LOSS: {m.losses}\n"
        f"Win rate: {win_rate:.1f}%\n"
        f"Alerts sent: {m.alerts_sent}\n"
        f"Errors: {m.errors}\n"
        f"Uptime: {_fmt_uptime(m.uptime_seconds)}"
    )
    await update.message.reply_text(text)


@_require_auth
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = ctx.bot_data.get("state")
    if not state:
        await update.message.reply_text("Bot not ready yet.")
        return

    q_size = state.queue.qsize() if state.queue else 0
    version = await state.get_filter_version()

    text = (
        "🤖 STATUS\n\n"
        f"Queue size: {q_size}\n"
        f"Filter params version: v{version}\n"
        f"Uptime: {_fmt_uptime(state.metrics.uptime_seconds)}\n"
        f"Total processed: {state.metrics.calls_total}"
    )
    await update.message.reply_text(text)


@_require_auth
async def cmd_active(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = ctx.bot_data.get("state")
    db = ctx.bot_data.get("db")
    if not state or not db:
        await update.message.reply_text("Bot not ready yet.")
        return

    active = await db.get_active_calls()
    if not active:
        await update.message.reply_text("No active calls.")
        return

    lines = ["📈 ACTIVE CALLS\n"]
    for call in active[:10]:
        age = ""
        if call.call_time:
            mins = (datetime.now(timezone.utc) - call.call_time).total_seconds() / 60
            age = f" ({mins:.0f}m ago)"

        gain = call.max_gain
        gain_str = f"+{(gain-1)*100:.1f}%" if gain > 1 else f"{(gain-1)*100:.1f}%"
        lines.append(
            f"• {call.token_symbol} ({call.token_address[:6]}...)\n"
            f"  Score: {call.llm_score} | {call.llm_verdict}{age}\n"
            f"  Max gain: {gain_str}"
        )

    await update.message.reply_text("\n".join(lines))


@_require_auth
async def cmd_positions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show open trading positions (Phase 1 paper mode)."""
    state = ctx.bot_data.get("state")
    db = ctx.bot_data.get("db")
    if not state or not db:
        await update.message.reply_text("Bot not ready yet.")
        return

    try:
        rows = await db.get_open_positions()
    except Exception as e:
        await update.message.reply_text(f"DB error: {e}")
        return

    if not rows:
        await update.message.reply_text("No open positions.")
        return

    lines = [f"💼 OPEN POSITIONS ({len(rows)})\n"]
    for r in rows[:10]:
        entry_price = r["entry_price"] or 0
        entry_tokens = r["entry_amount_token"] or 0
        remaining_tokens = r["current_amount_token"] or 0
        remain_pct = (remaining_tokens / entry_tokens * 100) if entry_tokens > 0 else 100
        # M4 fix: remaining_tokens * entry_price = USD (entry_price is USD/token),
        # not SOL. Convert to actual SOL using sol_usd_at_entry (captured at
        # buy time — the only conversion rate we have on the position row).
        remaining_usd = remaining_tokens * entry_price
        sol_usd = r.get("sol_usd_at_entry") or 0.0
        if sol_usd <= 0:
            sol_usd = 150.0  # last-resort fallback (matches trade_executor)
        remaining_actual_sol = remaining_usd / sol_usd if sol_usd > 0 else 0.0
        peak = r["peak_price"] or 0
        # L9 fix: peak_pct = -100% when peak==0 (newly opened). Default to 0.
        if peak <= 0 or entry_price <= 0:
            peak_pct = 0.0
        else:
            peak_pct = ((peak / entry_price) - 1) * 100
        exit_reason = r["exit_reason"] or ""
        age_sec = 0
        if r["entry_time"]:
            entry_val = r["entry_time"]
            if isinstance(entry_val, str):
                if entry_val.endswith("Z"):
                    entry_val = entry_val[:-1] + "+00:00"
                try:
                    entry_dt = datetime.fromisoformat(entry_val)
                except (ValueError, TypeError) as e:
                    logger.warning(
                        f"[BOT] Unparseable entry_time for position {r.get('id')}: "
                        f"{r['entry_time']!r} — showing age=0 ({e})"
                    )
                    entry_dt = None
            elif isinstance(entry_val, datetime):
                entry_dt = entry_val
            else:
                entry_dt = None
            if entry_dt:
                age_sec = (datetime.now(timezone.utc) - entry_dt).total_seconds()
        paper_tag = " [PAPER]" if r["paper"] else " [LIVE]"
        tp_tag = f" | {exit_reason} taken" if exit_reason else ""
        lines.append(
            f"• {r['token_symbol']} ({r['token_address'][:8]}...){paper_tag}\n"
            f"  Size: {remaining_actual_sol:.4f} SOL (${remaining_usd:.2f} USD, {remain_pct:.0f}% remain) @ {entry_price:.10f}\n"
            f"  Peak: {peak_pct:+.1f}% | Age: {age_sec/60:.1f}m\n"
            f"  ID: {r['id']}{tp_tag}"
        )

    await update.message.reply_text("\n".join(lines))


@_require_auth
async def cmd_pnl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show PnL summary + wallet balance (Phase 1 paper mode)."""
    state = ctx.bot_data.get("state")
    db = ctx.bot_data.get("db")
    if not state or not db:
        await update.message.reply_text("Bot not ready yet.")
        return

    try:
        pnl = await db.get_pnl_summary()
    except Exception as e:
        await update.message.reply_text(f"DB error: {e}")
        return

    wins = pnl["wins"]
    losses = pnl["losses"]
    win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0.0

    lines = [
        "💰 PnL SUMMARY (PAPER)\n",
        f"Closed trades: {pnl['closed_count']} (W:{wins} / L:{losses}, WR={win_rate:.0f}%)",
        f"Realized: {pnl['realized_sol']:+.4f} SOL (avg {pnl['avg_pnl_pct']:+.1f}%)",
        f"Open: {pnl['open_count']} positions, {pnl['deployed_sol']:.4f} SOL deployed",
    ]
    if pnl["wallet_balance"] is not None:
        lines.append(f"Wallet: {pnl['wallet_balance']:.4f} SOL")
    await update.message.reply_text("\n".join(lines))


@_require_auth
async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show closed (past) trading positions with PnL."""
    state = ctx.bot_data.get("state")
    db = ctx.bot_data.get("db")
    if not state or not db:
        await update.message.reply_text("Bot not ready yet.")
        return

    try:
        rows = await db.get_closed_positions(limit=15)
    except Exception as e:
        await update.message.reply_text(f"DB error: {e}")
        return

    if not rows:
        await update.message.reply_text("No closed positions yet.")
        return

    realized_total = sum((r["pnl_sol"] or 0) for r in rows)
    wins = sum(1 for r in rows if (r["pnl_sol"] or 0) > 0)
    losses = sum(1 for r in rows if (r["pnl_sol"] or 0) <= 0)

    lines = [
        f"📜 POSITION HISTORY ({len(rows)})\n",
        f"Total: {realized_total:+.4f} SOL (W:{wins} / L:{losses})\n",
    ]
    for r in rows:
        pnl_sol = r["pnl_sol"] or 0
        pnl_pct = r["pnl_pct"] or 0
        exit_reason = r["exit_reason"] or "?"
        paper_tag = " [PAPER]" if r["paper"] else " [LIVE]"
        pnl_emoji = "🟢" if pnl_sol >= 0 else "🔴"
        entry_sol = r["entry_amount_sol"] or 0
        entry_price = r["entry_price"] or 0
        exit_price = r["exit_price"] or 0

        hold_str = ""
        if r["hold_seconds"]:
            mins = r["hold_seconds"] // 60
            secs = r["hold_seconds"] % 60
            hold_str = f"{mins}m{secs}s" if mins > 0 else f"{secs}s"

        exit_time_short = ""
        if r["exit_time"]:
            exit_val = r["exit_time"]
            if isinstance(exit_val, str):
                if exit_val.endswith("Z"):
                    exit_val = exit_val[:-1] + "+00:00"
                try:
                    dt = datetime.fromisoformat(exit_val)
                except (ValueError, TypeError) as e:
                    logger.warning(
                        f"[BOT] Unparseable exit_time for position {r.get('id')}: "
                        f"{r['exit_time']!r} — skipping ({e})"
                    )
                    dt = None
            elif isinstance(exit_val, datetime):
                dt = exit_val
            else:
                dt = None
            if dt:
                exit_time_short = dt.strftime("%m-%d %H:%M")

        lines.append(
            f"{pnl_emoji} {r['token_symbol']} ({exit_reason}){paper_tag} {exit_time_short}\n"
            f"   {entry_sol:.4f} SOL | {entry_price:.2e} → {exit_price:.2e}\n"
            f"   PnL: {pnl_sol:+.4f} SOL ({pnl_pct:+.1f}%) | Hold: {hold_str}"
        )

    await update.message.reply_text("\n".join(lines))


@_require_auth
async def cmd_filter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = ctx.bot_data.get("state")
    if not state:
        await update.message.reply_text("Bot not ready yet.")
        return

    params = await state.get_filter_params()
    version = await state.get_filter_version()
    lines = [f"🔧 FILTER PARAMS (v{version})\n"]

    key_map = {
        "min_total_fee": ("min_fee", "min_fee_sol", " SOL"),
        "fee_tier": ("fee_tier", None, ""),
        "insider_concentration": ("max_insider", "max_insider_ratio", "%"),
        "min_holders": ("min_holders", "min_holders", ""),
        "funded_wallet_age": ("max_new", "max_new_wallet_pct", "%"),
        "rug_probability": ("max_rug", "max_rug_prob", ""),
        "holder_distribution": ("max_top15", "max_top15_pct", "%"),
    }

    for filter_name, (short, param_key, unit) in key_map.items():
        fp = params.get(filter_name, {})
        if not fp.get("enabled", True):
            continue
        if param_key:
            val = fp.get(param_key)
            if val is None:
                continue
            if unit == "%":
                lines.append(f"  {short}: {val:.0%}")
            elif unit == "$":
                lines.append(f"  {short}: ${val:,.0f}")
            else:
                lines.append(f"  {short}: {val}{unit}")

    await update.message.reply_text("\n".join(lines))


@_require_auth
async def cmd_queue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = ctx.bot_data.get("state")
    if not state:
        await update.message.reply_text("Bot not ready yet.")
        return
    q_size = state.queue.qsize() if state.queue else 0
    text = f"📦 Queue: {q_size} tokens waiting"
    await update.message.reply_text(text)


@_require_auth
async def cmd_recent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db = ctx.bot_data.get("db")
    if not db:
        await update.message.reply_text("Bot not ready yet.")
        return

    now = datetime.now(timezone.utc)
    calls = await db.get_calls_in_range(now - timedelta(hours=24), now)
    if not calls:
        await update.message.reply_text("No calls in the last 24h.")
        return

    lines = ["📋 RECENT CALLS (24h)\n"]
    for call in calls[:10]:
        age = ""
        if call.call_time:
            mins = (now - call.call_time).total_seconds() / 60
            age = f" ({mins:.0f}m ago)"

        gain = call.max_gain
        gain_str = f"+{(gain-1)*100:.1f}%" if gain > 1 else f"{(gain-1)*100:.1f}%"
        status_emoji = {"WIN": "✅", "LOSS": "❌", "PENDING": "⏳"}.get(call.status.value, "?")
        lines.append(
            f"{status_emoji} {call.token_symbol} | Score {call.llm_score} | {call.llm_verdict}{age}\n"
            f"   Max: {gain_str} | {call.token_address[:8]}..."
        )

    await update.message.reply_text("\n".join(lines))


@_require_auth
async def cmd_best(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db = ctx.bot_data.get("db")
    if not db:
        await update.message.reply_text("Bot not ready yet.")
        return

    now = datetime.now(timezone.utc)
    calls = await db.get_calls_in_range(now - timedelta(days=7), now)
    if not calls:
        await update.message.reply_text("No calls in the last 7 days.")
        return

    resolved = [c for c in calls if c.status.value in ("WIN", "LOSS")]
    if not resolved:
        await update.message.reply_text("No resolved calls yet.")
        return

    resolved.sort(key=lambda c: c.max_gain, reverse=True)
    lines = ["🏆 BEST PERFORMERS (7d)\n"]
    for call in resolved[:5]:
        gain = call.max_gain
        gain_str = f"+{(gain-1)*100:.1f}%"
        lines.append(
            f"• {call.token_symbol} {gain_str} | Score {call.llm_score} | {call.llm_verdict}"
        )

    await update.message.reply_text("\n".join(lines))


@_require_auth
async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = ctx.bot_data.get("state")
    uptime = _fmt_uptime(state.metrics.uptime_seconds) if state else "?"
    user_id = update.effective_user.id if update.effective_user else "?"
    logger.info(f"[PING] chat={update.effective_chat.id if update.effective_chat else '?'} user={user_id}")
    await update.message.reply_text(f"🏓 Pong! Uptime: {uptime}")


@_require_auth
async def cmd_live_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show live trading status: mode, balance, positions, PnL, pause state."""
    state = ctx.bot_data.get("state")
    db = ctx.bot_data.get("db")
    if not state:
        await update.message.reply_text("Bot not ready yet.")
        return

    paper_mode = state.paper_mode if hasattr(state, "paper_mode") else True
    live_paused = getattr(state, "_live_paused", False)
    mode = "PAPER" if paper_mode else "LIVE"
    pause_str = " ⏸ PAUSED" if live_paused else ""

    lines = [f"🔴 Live Status: {mode}{pause_str}"]

    # GMGN balance (live mode)
    if not paper_mode and getattr(state, "gmgn_cli", None):
        try:
            bal = await state.gmgn_cli.get_sol_balance()
            lines.append(f"GMGN balance: {bal:.4f} SOL")
        except Exception as e:
            lines.append(f"GMGN balance: error ({e})")

    # Open positions
    try:
        positions = await state.position_manager.get_open_positions()
        live_count = sum(1 for p in positions if not p.get("paper", 1))
        paper_count = len(positions) - live_count
        lines.append(
            f"Open positions: {len(positions)} "
            f"(live: {live_count}, paper: {paper_count})"
        )
    except Exception as e:
        lines.append(f"Positions: error ({e})")

    # Risk state
    if hasattr(state, "risk_manager"):
        rm = state.risk_manager
        lines.append(
            f"Daily PnL: {rm.daily_pnl:+.4f} SOL | "
            f"trades today: {rm.daily_trades} | "
            f"loss streak: {rm.loss_streak}"
        )

    await update.message.reply_text("\n".join(lines))


@_require_auth
async def cmd_live_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Pause live trading (no new buys until /live_resume)."""
    state = ctx.bot_data.get("state")
    if not state:
        await update.message.reply_text("Bot not ready yet.")
        return
    if state.paper_mode:
        await update.message.reply_text("Bot is in PAPER mode (no live trades).")
        return
    state._live_paused = True
    logger.warning("[LIVE] Paused via /live_pause")
    await update.message.reply_text("⏸ Live trading PAUSED. No new buys will execute.\nUse /live_resume to resume.")


@_require_auth
async def cmd_live_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Resume live trading after pause."""
    state = ctx.bot_data.get("state")
    if not state:
        await update.message.reply_text("Bot not ready yet.")
        return
    if state.paper_mode:
        await update.message.reply_text("Bot is in PAPER mode (no live trades).")
        return
    state._live_paused = False
    logger.warning("[LIVE] Resumed via /live_resume")
    await update.message.reply_text("▶️ Live trading RESUMED. New buys will execute on APE signals.")


@_require_auth
async def cmd_close_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Close all live positions by selling tokens at market."""
    state = ctx.bot_data.get("state")
    if not state:
        await update.message.reply_text("Bot not ready yet.")
        return
    if state.paper_mode:
        await update.message.reply_text("Bot is in PAPER mode (use /positions for view).")
        return
    if not getattr(state, "executor", None) or not getattr(state, "gmgn_cli", None):
        await update.message.reply_text("Live trading not initialized.")
        return

    await update.message.reply_text("🔄 Closing all live positions...")

    try:
        positions = await state.position_manager.get_open_positions()
        live_positions = [p for p in positions if not p.get("paper", 1)]
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to fetch positions: {e}")
        return

    if not live_positions:
        await update.message.reply_text("No live positions to close.")
        return

    # Pause live trading to prevent re-entry during close
    state._live_paused = True

    closed = 0
    skipped = 0
    failed = 0
    for pos in live_positions:
        sym = pos.get("token_symbol", "?")
        addr = pos.get("token_address", "")
        current_amount = pos.get("current_amount_token", 0) or 0
        strategy_id = pos.get("strategy_order_id", "")

        # Skip if has active GMGN strategy (let it execute naturally)
        if strategy_id:
            skipped += 1
            logger.info(
                f"[CLOSE-ALL] Skipped {sym}: has active strategy {strategy_id[:16]}"
            )
            continue

        if current_amount <= 0:
            skipped += 1
            continue

        try:
            from core.jupiter_client import SOL_MINT
            token_decimals = 9  # default; could fetch from token info
            sell_lamports = int(current_amount * (10 ** token_decimals))
            result = await state.gmgn_cli.swap(
                chain="sol",
                from_addr=state.gmgn_cli.get_wallet_address_sync()
                if hasattr(state.gmgn_cli, "get_wallet_address_sync")
                else "unknown",
                input_token=addr,
                output_token=SOL_MINT,
                amount=sell_lamports,
                slippage=30,
            )
            if result and result.get("order_id"):
                # Wait for confirmation
                status = await state.gmgn_cli.wait_for_order("sol", result["order_id"])
                if status.get("status") == "confirmed":
                    await state.position_manager.close_position(
                        pos, exit_reason="MANUAL_CLOSE_ALL",
                        exit_price=0.0, pnl_sol=0.0, pnl_pct=0.0, pnl_usd=0.0,
                    )
                    closed += 1
                else:
                    failed += 1
            else:
                failed += 1
        except Exception as e:
            logger.error(f"[CLOSE-ALL] Failed to close {sym}: {e}")
            failed += 1

    msg = (
        f"✅ Closed: {closed} | "
        f"⏭ Skipped (active strategy): {skipped} | "
        f"❌ Failed: {failed}\n"
        f"Live trading PAUSED — use /live_resume to continue."
    )
    await update.message.reply_text(msg)


@_require_auth
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)


async def bot_handler(state, db):
    if not settings.telegram_bot_token:
        logger.warning("No Telegram bot token configured, bot handler disabled")
        await asyncio.sleep(float("inf"))
        return

    global _bot_app

    # A3 fix: guarantee only ONE Application + webhook at a time.
    # If a previous attempt left state behind (crash, hot-reload), clean up
    # first to avoid "Conflict: terminated by other getUpdates" on restart.
    if _bot_app is not None:
        try:
            await _bot_app.shutdown()
        except Exception as e:
            logger.warning(f"[BOT] Previous app shutdown failed (continuing): {e}")
        _bot_app = None

    _bot_app = Application.builder().token(settings.telegram_bot_token).build()
    _bot_app.bot_data["state"] = state
    _bot_app.bot_data["db"] = db

    _bot_app.add_handler(CommandHandler("start", cmd_start))
    _bot_app.add_handler(CommandHandler("help", cmd_help))
    _bot_app.add_handler(CommandHandler("stats", cmd_stats))
    _bot_app.add_handler(CommandHandler("status", cmd_status))
    _bot_app.add_handler(CommandHandler("active", cmd_active))
    _bot_app.add_handler(CommandHandler("positions", cmd_positions))
    _bot_app.add_handler(CommandHandler("history", cmd_history))
    _bot_app.add_handler(CommandHandler("pnl", cmd_pnl))
    _bot_app.add_handler(CommandHandler("filter", cmd_filter))
    _bot_app.add_handler(CommandHandler("queue", cmd_queue))
    _bot_app.add_handler(CommandHandler("recent", cmd_recent))
    _bot_app.add_handler(CommandHandler("best", cmd_best))
    _bot_app.add_handler(CommandHandler("ping", cmd_ping))
    _bot_app.add_handler(CommandHandler("live_status", cmd_live_status))
    _bot_app.add_handler(CommandHandler("live_pause", cmd_live_pause))
    _bot_app.add_handler(CommandHandler("live_resume", cmd_live_resume))
    _bot_app.add_handler(CommandHandler("close_all", cmd_close_all))

    await _bot_app.initialize()

    port = int(os.environ.get("PORT", 8080))

    webhook_url = os.environ.get("TELEGRAM_WEBHOOK_URL", "")
    if not webhook_url:
        railway_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
        if railway_domain:
            webhook_url = f"https://{railway_domain}/telegram/webhook"
        else:
            webhook_url = f"http://127.0.0.1:{port}/telegram/webhook"

    # Detect polling mode: use when no public https URL is reachable.
    # Webhook requires https + public domain (Telegram servers must reach us).
    # Localhost http:// URLs are NOT reachable, so use polling.
    use_polling = (
        webhook_url.startswith("http://")
        or "127.0.0.1" in webhook_url
        or "localhost" in webhook_url
        or os.environ.get("TELEGRAM_POLLING", "").lower() == "true"
    )

    runner = None  # for cleanup in finally
    if use_polling:
        logger.warning(
            f"TELEGRAM POLLING MODE (webhook URL '{webhook_url}' not usable from "
            f"Telegram servers). No port 8080 needed."
        )
        try:
            # In python-telegram-bot v20+ you MUST call start() before
            # start_polling() — otherwise the updater receives updates from
            # Telegram but the dispatcher is not running, so handlers are
            # never invoked (silent dead bot). This was the root cause of
            # "Telegram not responding" on VPS.
            await _bot_app.start()
            await _bot_app.updater.start_polling(
                drop_pending_updates=True,
                allowed_updates=["message"],
            )
            logger.info("Telegram polling bot ready (updater + dispatcher running)")
        except Exception as e:
            logger.error(f"Failed to start Telegram polling: {e}")
            raise
    else:
        logger.warning(f"WEBHOOK URL: {webhook_url}")

        async def handle_webhook(request: web.Request):
            try:
                data = await request.json()
                update = Update.de_json(data, _bot_app.bot)
                await _bot_app.process_update(update)
            except Exception as e:
                logger.error(f"Webhook error: {e}")
            return web.Response(text="ok")

        async def health_check(request: web.Request):
            return web.Response(text="ok")

        app = web.Application()
        app.router.add_post("/telegram/webhook", handle_webhook)
        app.router.add_get("/health", health_check)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()

        logger.info(f"Webhook server started on port {port}")

        for attempt in range(5):
            try:
                result = await _bot_app.bot.set_webhook(
                    url=webhook_url,
                    drop_pending_updates=True,
                    allowed_updates=["message"],
                )
                if result:
                    logger.info(f"Webhook registered: {webhook_url}")
                    break
                else:
                    logger.warning(f"Webhook set failed (attempt {attempt+1}/5)")
            except Exception as e:
                logger.warning(f"Webhook set error (attempt {attempt+1}/5): {e}")
            await asyncio.sleep(2)
        else:
            # D14 fix: if all 5 attempts failed, alert the user via Telegram
            # (best-effort, swallow any error) and raise so the watcher restarts
            # the bot. Previously the failure was silently logged — leaving the
            # user with no idea their commands weren't working.
            try:
                from alerts.dispatcher import dispatcher
                await dispatcher.send_message(
                    f"🚨 BOT STARTUP FAILED\n\n"
                    f"Could not register Telegram webhook at {webhook_url} "
                    f"after 5 attempts. Bot is running but /commands will not work.\n"
                    f"Check RAILWAY_PUBLIC_DOMAIN env var + Telegram bot permissions."
                )
            except Exception:
                pass
            raise RuntimeError(f"Webhook set failed after 5 attempts: {webhook_url}")

        logger.info("Telegram webhook bot ready")

    try:
        from telegram import BotCommand
        commands = [
            BotCommand("start", "Show welcome + commands"),
            BotCommand("help", "Show command list"),
            BotCommand("stats", "Performance metrics"),
            BotCommand("status", "Bot status & queue"),
            BotCommand("active", "Tracked alert calls"),
            BotCommand("positions", "Open trading positions"),
            BotCommand("history", "Closed (past) positions with PnL"),
            BotCommand("pnl", "PnL summary + wallet balance"),
            BotCommand("filter", "Current filter params"),
            BotCommand("queue", "Queue size"),
            BotCommand("recent", "Last 10 calls"),
            BotCommand("best", "Best performing tokens"),
            BotCommand("ping", "Check bot alive"),
        ]
        await _bot_app.bot.set_my_commands(commands)
        logger.info(f"[BOT] Registered {len(commands)} commands (use / to see)")
    except Exception as e:
        logger.warning(f"setMyCommands failed: {e}")

    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        try:
            if _bot_app is not None:
                await _bot_app.shutdown()
        except Exception as e:
            logger.warning(f"[BOT] shutdown error: {e}")
        if runner is not None:
            try:
                await runner.cleanup()
            except Exception as e:
                logger.warning(f"[BOT] runner cleanup error: {e}")
        # A3 fix: clear module-level ref so a future restart (via _run_forever)
        # doesn't see stale state and try to re-use a dead Application.
        _bot_app = None
