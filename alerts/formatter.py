from datetime import datetime, timezone
from analysis.models import CallRecord, LLMDecision, TokenData


def format_alert(token: TokenData, decision: LLMDecision, fv_dict: dict,
                  social_score: float = 0.0) -> str:
    verdict_emoji = {"APE": "🔥", "WATCH": "👀", "SKIP": "⛔"}.get(decision.verdict.value, "❓")

    score_bar = _score_bar(int(decision.confidence * 100))

    filters_text = ""
    filter_names = {
        "token_age": "⏰ Age",
        "min_market_cap": "📊 Min MC",
        "max_market_cap": "📊 Max MC",
        "min_total_fee": "⛽ Min Fee",
        "fee_tier": "💰 Fee Tier",
        "insider_concentration": "🔍 Insider",
        "min_holders": "👥 Min Holders",
        "funded_wallet_age": "👛 Wallet Age",
        "rug_probability": "🛡️ Rug Risk",
        "holder_distribution": "👥 Holders",
        "ath_drawdown": "📉 ATH Drawdown",
    }
    for key, label in filter_names.items():
        f = fv_dict.get(key, {})
        if not f.get("enabled", True):
            status = "ℹ️"
        elif f.get("passed", False):
            status = "✅"
        else:
            status = "❌"
        note = f.get("note", "")
        filters_text += f"  {status} {label}: {note}\n"

    links = []
    ca_short = f"{token.address[:6]}...{token.address[-4:]}"
    links.append(f"  • GMGN: https://gmgn.ai/sol/token/{token.address}")
    links.append(f"  • DexScreener: https://dexscreener.com/solana/{token.address}")
    links.append(f"  • Solscan: https://solscan.io/token/{token.address}")

    social_text = ""
    if token.twitter_username or token.website_url or token.influencer_mentions or token.has_community:
        social_text = "\n🐦 Social Analysis:\n"
        if token.twitter_username:
            verified = "✓" if token.twitter_verified else ""
            social_text += f"  • Twitter: @{token.twitter_username} ({token.twitter_followers:,} followers {verified})\n"
        if token.website_url:
            social_text += f"  • Website: {token.website_url}\n"
        if token.has_community:
            social_text += f"  • Community: Yes\n"
        if token.influencer_mentions:
            for inf in token.influencer_mentions[:3]:
                social_text += f"  • 🔥 @{inf['handle']} tweeted ({inf['likes']:,} likes)\n"
        if token.project_type:
            social_text += f"  • Project: {token.project_type}\n"

    data_score = decision.score
    final_score = (social_score * 0.5) + (data_score * 0.5)

    scoring_text = f"""📊 SCORE BREAKDOWN:
  Social (LLM #1):  {social_score:.0f}/100 × 50% = {social_score*0.5:.1f}
  Data (LLM #2):    {data_score}/100 × 50% = {data_score*0.5:.1f}
  ─────────────────────────────
  Final Score:       {final_score:.1f}/100
  Verdict:           {decision.verdict.value} {verdict_emoji}"""

    msg = f"""{verdict_emoji} {decision.verdict.value} ALERT — ${token.symbol or token.name}

{scoring_text}
🏷️ Contract: {ca_short}
💰 Market Cap: ${token.market_cap:,.0f}
📈 Volume (1h): ${token.volume_1h:,.0f}
💧 Liquidity: ${token.liquidity:,.0f}
👥 Holders: {token.holders_count}
{social_text}
🤖 MiMo Analysis:
"{decision.reasoning}"

🔑 Key Factors: {', '.join(decision.key_factors) if decision.key_factors else 'N/A'}

📋 Filters:
{filters_text}
🔗 Links:
{chr(10).join(links)}

⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}
🎯 Confidence: {decision.confidence:.0%}"""

    return msg


def format_recap(recap: dict) -> str:
    if not recap:
        return "📊 No calls in the last hour."

    lines = [
        f"📊 HOURLY RECAP — {recap['period_start'][:16]} to {recap['period_end'][:16]} UTC",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
    ]

    for call in recap.get("calls", []):
        status = call.status.value
        emoji = {"WIN": "🔥", "LOSS": "❌", "PENDING": "⏳"}.get(status, "❓")
        gain_str = f"{call.max_gain:.2f}x"
        gain_pct = f"+{(call.max_gain - 1) * 100:.0f}%" if call.max_gain >= 1.0 else f"{(call.max_gain - 1) * 100:.0f}%"

        lines.append(f"{emoji} {status} — ${call.token_symbol} (Score: {call.llm_score})")
        lines.append(f"  Entry: ${call.entry_price:.6f} | Max: {gain_str} ({gain_pct})")

        reason = recap.get("loss_reasons", {}).get(call.token_symbol, "")
        if status == "LOSS" and reason:
            lines.append(f"  💡 MiMo: \"{reason}\"")

        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append("📈 SUMMARY")
    lines.append(f"  Calls: {recap['total']} | Wins: {recap['wins']} | Losses: {recap['losses']} | Pending: {recap['pending']}")
    lines.append(f"  Win Rate: {recap['win_rate']:.1f}%")
    lines.append(f"  Avg Gain: {recap['avg_gain']:.2f}x")
    lines.append(f"  Best: ${recap['best_token']} ({recap['best_gain']:.2f}x)")

    return "\n".join(lines)


def _score_bar(score: int) -> str:
    filled = score // 10
    empty = 10 - filled
    return "█" * filled + "░" * empty


def format_exit_alert(symbol: str, address: str, entry_price: float,
                       exit_price: float, pnl_sol: float, pnl_pct: float,
                       reason: str, hold_seconds: float, paper: bool = True,
                       position_size_sol: float = 0.0, total_tokens: float = 0.0,
                       sold_pct: float = 100.0, sold_tokens: float = 0.0,
                       remaining_tokens: float = 0.0) -> str:
    """Format a position exit alert (SL / TP1 / TP2 / TRAILING / TIME)."""
    reason_emojis = {
        "SL": "🛑",
        "TP1": "🎯",
        "TP2": "🎯🎯",
        "TRAILING": "📉",
        "TIME": "⏰",
    }
    pnl_emoji = "📈" if pnl_sol >= 0 else "📉"
    paper_tag = " [PAPER]" if paper else " [LIVE]"
    minutes = int(hold_seconds // 60)
    seconds = int(hold_seconds % 60)
    hold_str = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"

    reason_emoji = reason_emojis.get(reason, "🔔")

    lines = [
        f"{reason_emoji} EXIT: {reason} {symbol}{paper_tag}",
        "",
    ]

    if position_size_sol > 0 or total_tokens > 0:
        size_str = f"{position_size_sol:.4f} SOL" if position_size_sol > 0 else "?"
        if reason in ("TP1", "TP2") and sold_pct < 100:
            size_str += f" (sold {sold_pct:.0f}% = {sold_tokens:,.0f} tokens, {remaining_tokens:,.0f} remain)"
        else:
            size_str += f" (closed 100% = {total_tokens:,.0f} tokens)"
        lines.append(f"  Size:   {size_str}")

    lines.extend([
        f"  Entry:  {entry_price:.10f}",
        f"  Exit:   {exit_price:.10f}",
        f"  PnL: {pnl_emoji} {pnl_sol:+.4f} SOL ({pnl_pct:+.1f}%)",
        f"  Hold:   {hold_str}",
        f"  Token:  {address[:8]}...",
    ])

    return "\n".join(lines)


def format_trade_alert(position, side: str) -> str:
    """Format a trade alert (BUY/SELL) for Telegram."""
    emoji = "🟢" if side == "BUY" else "🔴"
    pnl_sol = getattr(position, "pnl_sol", 0.0) or 0.0
    pnl_pct = getattr(position, "pnl_pct", 0.0) or 0.0
    paper_tag = " [PAPER]" if getattr(position, "paper", True) else " [LIVE]"

    lines = [
        f"{emoji} TRADE: {side} {position.token_symbol}{paper_tag}",
        "",
        f"  Token: {position.token_symbol} ({position.token_address[:8]}...)",
        f"  Size: {position.entry_amount_sol:.4f} SOL",
        f"  Tokens: {position.entry_amount_token:.2f}",
        f"  Entry: {position.entry_price:.10f} SOL",
    ]

    if side == "BUY":
        lines.append(f"  TX: `{position.entry_tx_sig[:16]}...`")
    else:
        exit_reason = getattr(position, "exit_reason", "") or ""
        lines.append(f"  Exit: {position.exit_price:.10f} SOL ({exit_reason})")
        pnl_emoji = "📈" if pnl_sol >= 0 else "📉"
        lines.append(f"  PnL: {pnl_emoji} {pnl_sol:+.4f} SOL ({pnl_pct:+.1f}%)")

    return "\n".join(lines)
