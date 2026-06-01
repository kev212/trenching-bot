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


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔥 Trenching Bot ACTIVE\n\n"
        "Commands:\n"
        "/stats - Performance metrics\n"
        "/status - Bot status & queue\n"
        "/active - Tracked alert calls\n"
        "/positions - Open trading positions (Phase 1 paper)\n"
        "/pnl - PnL summary (realized + unrealized)\n"
        "/filter - Current filter params\n"
        "/queue - Queue size\n"
        "/recent - Last 10 calls\n"
        "/best - Best performing tokens\n"
        "/ping - Check bot alive\n"
        "/help - Show this message"
    )


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


async def cmd_positions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show open trading positions (Phase 1 paper mode)."""
    state = ctx.bot_data.get("state")
    db = ctx.bot_data.get("db")
    if not state or not db:
        await update.message.reply_text("Bot not ready yet.")
        return

    try:
        cursor = await db.db.execute(
            "SELECT * FROM positions WHERE status = 'OPEN' ORDER BY entry_time DESC"
        )
        rows = await cursor.fetchall()
    except Exception as e:
        await update.message.reply_text(f"DB error: {e}")
        return

    if not rows:
        await update.message.reply_text("No open positions.")
        return

    lines = [f"💼 OPEN POSITIONS ({len(rows)})\n"]
    for r in rows[:10]:
        entry_sol = r["entry_amount_sol"] or 0
        entry_price = r["entry_price"] or 0
        peak = r["peak_price"] or 0
        peak_pct = ((peak / entry_price) - 1) * 100 if entry_price > 0 else 0
        age_sec = 0
        if r["entry_time"]:
            try:
                entry_dt = datetime.fromisoformat(r["entry_time"])
                age_sec = (datetime.now(timezone.utc) - entry_dt).total_seconds()
            except (ValueError, TypeError):
                pass
        paper_tag = " [PAPER]" if r["paper"] else " [LIVE]"
        lines.append(
            f"• {r['token_symbol']} ({r['token_address'][:8]}...){paper_tag}\n"
            f"  Size: {entry_sol:.4f} SOL @ {entry_price:.10f}\n"
            f"  Peak: {peak_pct:+.1f}% | Age: {age_sec/60:.1f}m\n"
            f"  ID: {r['id']}"
        )

    await update.message.reply_text("\n".join(lines))


async def cmd_pnl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show PnL summary + wallet balance (Phase 1 paper mode)."""
    state = ctx.bot_data.get("state")
    db = ctx.bot_data.get("db")
    if not state or not db:
        await update.message.reply_text("Bot not ready yet.")
        return

    try:
        cursor = await db.db.execute(
            "SELECT COUNT(*) as n, "
            "COALESCE(SUM(pnl_sol), 0) as realized_sol, "
            "COALESCE(AVG(pnl_pct), 0) as avg_pnl_pct, "
            "COALESCE(SUM(CASE WHEN pnl_sol > 0 THEN 1 ELSE 0 END), 0) as wins, "
            "COALESCE(SUM(CASE WHEN pnl_sol <= 0 THEN 1 ELSE 0 END), 0) as losses "
            "FROM positions WHERE status = 'CLOSED'"
        )
        row = await cursor.fetchone()
        wins = row["wins"] or 0
        losses = row["losses"] or 0
        win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0.0

        cursor = await db.db.execute(
            "SELECT COUNT(*) as n, COALESCE(SUM(entry_amount_sol), 0) as deployed "
            "FROM positions WHERE status = 'OPEN'"
        )
        open_row = await cursor.fetchone()
        open_count = open_row["n"] or 0
        deployed = open_row["deployed"] or 0.0

        cursor = await db.db.execute(
            "SELECT snapshot_time, sol_balance FROM wallet_balances "
            "WHERE paper = 1 ORDER BY snapshot_time DESC LIMIT 1"
        )
        bal_row = await cursor.fetchone()
        wallet_balance = bal_row["sol_balance"] if bal_row else None
    except Exception as e:
        await update.message.reply_text(f"DB error: {e}")
        return

    lines = [
        "💰 PnL SUMMARY (PAPER)\n",
        f"Closed trades: {row['n'] or 0} (W:{wins} / L:{losses}, WR={win_rate:.0f}%)",
        f"Realized: {row['realized_sol'] or 0:+.4f} SOL (avg {row['avg_pnl_pct'] or 0:+.1f}%)",
        f"Open: {open_count} positions, {deployed:.4f} SOL deployed",
    ]
    if wallet_balance is not None:
        lines.append(f"Wallet: {wallet_balance:.4f} SOL")
    await update.message.reply_text("\n".join(lines))


async def cmd_filter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = ctx.bot_data.get("state")
    if not state:
        await update.message.reply_text("Bot not ready yet.")
        return

    params = await state.get_filter_params()
    version = await state.get_filter_version()
    lines = [f"🔧 FILTER PARAMS (v{version})\n"]

    key_map = {
        "token_age": ("max_age", "max_token_age_minutes", "min"),
        "min_market_cap": ("min_mc", "min_mc_usd", "$"),
        "max_market_cap": ("max_mc", "max_mc_usd", "$"),
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


async def cmd_queue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = ctx.bot_data.get("state")
    if not state:
        await update.message.reply_text("Bot not ready yet.")
        return
    q_size = state.queue.qsize() if state.queue else 0
    text = f"📦 Queue: {q_size} tokens waiting"
    await update.message.reply_text(text)


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


async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = ctx.bot_data.get("state")
    uptime = _fmt_uptime(state.metrics.uptime_seconds) if state else "?"
    await update.message.reply_text(f"🏓 Pong! Uptime: {uptime}")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)


async def bot_handler(state, db):
    if not settings.telegram_bot_token:
        logger.warning("No Telegram bot token configured, bot handler disabled")
        await asyncio.sleep(float("inf"))
        return

    global _bot_app

    _bot_app = Application.builder().token(settings.telegram_bot_token).build()
    _bot_app.bot_data["state"] = state
    _bot_app.bot_data["db"] = db

    _bot_app.add_handler(CommandHandler("start", cmd_start))
    _bot_app.add_handler(CommandHandler("help", cmd_help))
    _bot_app.add_handler(CommandHandler("stats", cmd_stats))
    _bot_app.add_handler(CommandHandler("status", cmd_status))
    _bot_app.add_handler(CommandHandler("active", cmd_active))
    _bot_app.add_handler(CommandHandler("positions", cmd_positions))
    _bot_app.add_handler(CommandHandler("pnl", cmd_pnl))
    _bot_app.add_handler(CommandHandler("filter", cmd_filter))
    _bot_app.add_handler(CommandHandler("queue", cmd_queue))
    _bot_app.add_handler(CommandHandler("recent", cmd_recent))
    _bot_app.add_handler(CommandHandler("best", cmd_best))
    _bot_app.add_handler(CommandHandler("ping", cmd_ping))

    await _bot_app.initialize()

    port = int(os.environ.get("PORT", 8080))

    webhook_url = os.environ.get("TELEGRAM_WEBHOOK_URL", "")
    if not webhook_url:
        railway_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
        if railway_domain:
            webhook_url = f"https://{railway_domain}/telegram/webhook"
        else:
            webhook_url = f"http://127.0.0.1:{port}/telegram/webhook"

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

    logger.info("Telegram webhook bot ready")

    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await _bot_app.shutdown()
        await runner.cleanup()
