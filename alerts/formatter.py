from datetime import datetime, timezone
from analysis.models import CallRecord, LLMDecision, TokenData


def format_alert(token: TokenData, decision: LLMDecision, fv_dict: dict) -> str:
    verdict_emoji = {"APE": "🔥", "WATCH": "👀", "SKIP": "⛔"}.get(decision.verdict.value, "❓")

    score_bar = _score_bar(decision.score)

    filters_text = ""
    filter_names = {
        "gas_fee": "⛽ Gas Fee",
        "funded_wallet_age": "👛 Wallet Age",
        "top_holder_balance": "💰 Top Holder",
        "entry_market_cap": "📊 Entry MC",
        "bundle_detection": "🔍 Bundle",
        "volume_fee_ratio": "📈 Volume",
        "rug_probability": "🛡️ Rug Risk",
        "holder_distribution": "👥 Holders",
    }
    for key, label in filter_names.items():
        f = fv_dict.get(key, {})
        status = "✅" if f.get("passed", False) else "❌"
        note = f.get("note", "")
        filters_text += f"  {status} {label}: {note}\n"

    links = []
    ca_short = f"{token.address[:6]}...{token.address[-4:]}"
    links.append(f"  • GMGN: https://gmgn.ai/sol/token/{token.address}")
    links.append(f"  • DexScreener: https://dexscreener.com/solana/{token.address}")
    links.append(f"  • Solscan: https://solscan.io/token/{token.address}")

    msg = f"""{verdict_emoji} {decision.verdict.value} ALERT — ${token.symbol or token.name}

📊 Score: {decision.score}/100 {score_bar}
🏷️ Contract: {ca_short}
💰 Market Cap: ${token.market_cap:,.0f}
📈 Volume (1h): ${token.volume_1h:,.0f}
💧 Liquidity: ${token.liquidity:,.0f}
👥 Holders: {token.holders_count}

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
