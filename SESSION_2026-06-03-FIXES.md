# Session 2026-06-03 — Fix Summary

## Commits

```
2e08c42 fix: Playwright browser leak + _run_forever never stops
daa8c6c fix: install Playwright chromium via Dockerfile
c704308 fix: install Playwright chromium via nixpacks.toml
688308c fix: install Playwright chromium during Railway build
ff216c0 fix: community creator fills Twitter field instead of separate field
a0a2631 feat: community creator scraping via Playwright
8fdfcad refactor: split LLM scoring inputs + cleanup redundant filters
3ca294d fix: early permanent skip + compound rule threshold update
5ec6f22 fix: restore filter_params definition removed in early check refactor
dc84341 fix: rate limiter lock contention — sleep di luar asyncio.Lock
```

---

## Fix 1: Rate Limiter Lock Contention

**File:** `utils/helpers.py`

**Problem:** `acquire()` held `asyncio.Lock()` during `asyncio.sleep()`, serializing all workers into a single file. Effective throughput: 3.75 tokens/min.

**Fix:** Sleep outside lock. Lock only held during check-and-decrement (~μs).

```python
# BEFORE: sleep inside lock
async with self._lock:
    while True:
        if self.tokens >= n:
            self.tokens -= n
            return
        await asyncio.sleep(...)  # ← BLOCKS ALL OTHER WORKERS

# AFTER: sleep outside lock
while True:
    async with self._lock:
        if self.tokens >= n:
            self.tokens -= n
            return
    await asyncio.sleep(...)  # ← workers run independently
```

---

## Fix 2: Early Permanent Skip

**File:** `main.py`

**Problem:** Token fee < 0.1 SOL still ran through all 12 filters before being rejected. Wasted processing time and rate limiter budget.

**Fix:** Added 3 early checks after Build TokenData, before running filters:

1. `fee < 0.1 SOL` → DEAD-LETTER
2. `age > 30m + fee < 1.0 SOL` → DEAD-LETTER (compound rule)
3. `age > max (120pre/45post)` → DEAD-LETTER

---

## Fix 3: Compound Rule Independence

**File:** `analysis/filters.py`

**Problem:** Compound rule depended on `min_total_fee` failure. With hard gate min_total_fee at 5.0 SOL, compound rule (fee < 1.0 SOL) would never trigger.

**Fix:** Removed `min_total_fee` dependency. Compound rule now checks age + fee directly:
- `COMPOUND_FEE_MIN_SOL`: 1.0 → kept at 1.0
- `COMPOUND_AGE_MINUTES`: 30 → kept at 30
- Function signature: removed `failures` parameter

---

## Fix 4: Hard Gate min_total_fee Threshold

**File:** `config/filter_params.json`

**Changed:** `min_fee_sol` from 0.2 → **5.0 SOL**

---

## Fix 5: LLM Input Separation

**Files:** `llm/prompts.py`, `main.py`, `analysis/filters.py`, `analysis/models.py`

**Problem:** LLM #1 (social) received on-chain data (age, MC, holders) that should be scored by LLM #2 (data).

**Fix:**
- LLM #1 prompt: removed `age_description`, `market_cap`, `holders_count`
- LLM #2: added `token_age` to FeatureVector (enabled=False, context only)
- `_compute_token_age()` added to filters.py for LLM #2 context
- Hard gate `token_age` removed (handled by early check)

---

## Fix 6: Redundant Filter Cleanup

**Files:** `analysis/filters.py`, `analysis/models.py`, `config/filter_params.json`, `alerts/formatter.py`, `alerts/bot.py`, `main.py`

**Removed from hard gate:**
- `min_market_cap` — already filtered by pre-filter ($7K-$200K)
- `max_market_cap` — already filtered by pre-filter
- `token_age` — already handled by early check

**Hard gate:** 12 filters → **9 filters**

---

## Fix 7: Community Creator Scraping (Playwright)

**Files:** `sources/twitter.py`, `main.py`, `analysis/models.py`, `llm/prompts.py`, `requirements.txt`, `Dockerfile`

**Problem:** Tokens with community URLs (e.g., `i/communities/123`) had no Twitter handle → LLM #1 got "Yes (community/ID)" with no social data.

**Fix:**
- `TwitterClient.get_community_creator()` — Playwright headless browser
- Opens community page → clicks About tab → extracts `Created by @handle`
- Fetches creator profile + tweets via FxTwitter
- Creator handle fills Twitter field in LLM #1 prompt
- Dockerfile installs Playwright chromium + system deps

---

## Fix 8: Playwright Browser Leak

**File:** `sources/twitter.py`

**Problem:** If `page.goto()`, `about.click()`, or `page.inner_text()` threw exception, `browser.close()` was never called → zombie chromium processes.

**Fix:** Browser declared outside try, `finally` block always closes:
```python
browser = None
try:
    async with async_playwright() as p:
        browser = await p.chromium.launch(...)
        # ... operations ...
finally:
    if browser:
        try:
            await browser.close()
        except Exception:
            pass
```

---

## Fix 9: `_run_forever` Never Stops

**File:** `main.py`

**Problem:** After 5 consecutive errors, `_run_forever` exited permanently. Critical tasks (`position_monitor`, `bot_handler`) died → Telegram bot unresponsive, positions unmonitored.

**Fix:**
- Retry counter reset after successful execution
- After 5 errors: sleep 60s, reset counter, restart
- Tasks never die permanently

---

## Production State

**Hard gate:** 9 filters (min_total_fee, fee_tier, min_holders, funded_wallet_age, holder_distribution, ath_drawdown, insider_concentration [disabled], rug_probability [disabled], social_narrative [always pass])

**Early check:** fee < 0.1 SOL, age > 30m + fee < 1.0 SOL, age > max

**LLM #1:** murni social data (Twitter, website, community creator, catalyst)

**LLM #2:** on-chain data (token_age context, fee, holders, distribution, drawdown)

**Dual-LLM fusion:** social × 0.5 + data × 0.5
