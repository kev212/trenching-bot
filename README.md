# Trenching Bot

Self-improving Solana meme coin trading bot with dual-LLM scoring architecture.

## Architecture

```
Data Sources → Queue → Worker → Filters → Social + Data Scoring → Alerts + Paper Trading
```

### Data Sources
- **GMGN Trending**: Top 20 tokens by 5-min momentum (60s poll)
- **Trenches**: New Pump.fun launches (30s poll)

### Dual-LLM Scoring
```
final_score = (social_score × 0.5) + (data_score × 0.5)

LLM #1: Social Narrative (0-100)
  - Twitter presence, community, tweets, website, catalyst

LLM #2: Token Data (0-100)
  - MC, fee, holders, distribution, age, ATH (weighted scoring)

Verdict:
  ≥70 → APE 🔥 (auto-buy if confidence ≥ 75%)
  50-69 → WATCH 👀 (alert only)
  <50 → SKIP ⛔
```

### Hard Gate Filters (must all pass)
| Filter | Threshold | Weight |
|---|---|---|
| Token Age | ≤120min pre-migrate, ≤45min post | 0.15 |
| Market Cap | $7K-$200K | 0.00 (gate only) |
| Total Fee | ≥5.0 SOL | 0.20 |
| Holders | ≥100 | 0.20 |
| Holder Distribution | Top 15 ≤65% | 0.20 |
| ATH Drawdown | ≥-50% | 0.15 |
| Funded Wallet Age | ≤30% fresh | 0.10 |

### Position Management
```
SL:      -50% from entry
TP1:     +50% → sell 33%
TP2:     +100% → sell 67% of remaining
Trailing: -20% from peak (after TP1/TP2)
Moon bag: ~22% stays until trailing/time
```

### Price Oracle
Multi-source aggregation (3s cache):
1. DexScreener (`priceNative` — SOL-denominated)
2. Jupiter Price API v6 (USD → SOL)
3. GMGN fallback

## Setup

### Prerequisites
- Python 3.9+
- Telegram bot token
- GMGN API key
- Helius RPC key
- MiMo API key (for LLM)

### Install
```bash
git clone https://github.com/kev212/trenching-bot.git
cd trenching-bot
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Configuration
```bash
cp .env.example .env
# Edit .env with your keys
```

### Run
```bash
python main.py
```

## Telegram Commands

| Command | Description |
|---|---|
| `/positions` | Open positions with remaining SOL + TP status |
| `/pnl` | PnL summary + wallet balance |
| `/history` | Closed positions with PnL |

## Configuration Files

| File | Purpose |
|---|---|
| `config/trading.json` | SL/TP, position size, trailing |
| `config/filter_params.json` | Hard gate thresholds + weights |
| `config/social_scoring.json` | Influencer caps, engagement tiers |
| `config/influencers.json` | Tracked influencer handles + weights |
| `config/risk_rules.json` | Max positions, daily loss limits |

## Tech Stack

- **Language**: Python 3.9
- **LLM**: MiMo V2.5 Pro (api.xiaomimimo.com)
- **Data**: GMGN API + DexScreener + Jupiter
- **Social**: FxTwitter API
- **Bot**: python-telegram-bot (webhook)
- **DB**: SQLite (aiosqlite)
- **Deploy**: Railway

## License

Private — kev212
