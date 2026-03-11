# HAWK — AI Market Intelligence Engine

**Status:** Design complete. Implementation pending.
**Script:** tools/hawk.py (standalone — no integration with S1 trading engine)
**Branch:** feature/hawk (merge to main only when fully tested)
**Telegram:** HAWK-Picks channel (separate from TradeOS-Trading)

---

## 1. Purpose

Replace TradeOS's hardcoded 20-stock watchlist with an AI-generated daily watchlist that adapts to market conditions, sector rotation, global cues, and technical setups. HAWK runs independently from the trading engine as a shadow-testing tool until proven valuable.

---

## 2. Daily Schedule

| Run | Time (IST) | Trigger | Input |
|-----|-----------|---------|-------|
| Evening | ~4:30 PM (after bhavcopy published) | Manual or cron | Full day's NSE data |
| Morning | ~8:00 AM (before market open) | Manual or cron | Evening picks + overnight global data |

---

## 3. Data Sources

### Tier 1 — Day 1 Build

| Data Point | Primary Source | Fallback Source | Library |
|------------|---------------|-----------------|---------|
| Bhavcopy (OHLCV + delivery %) | nsepython bhav_copy() | KiteConnect historical_data() | nsepython / pykiteconnect |
| FII/DII flows | nsepython nse_fii() | NSE website direct | nsepython |
| Nifty/BankNifty levels | KiteConnect | nsetools get_index_quote() | pykiteconnect / nsetools |
| India VIX | KiteConnect | nsepython | pykiteconnect |
| Sector indices | nsetools get_index_quote() | nsepython | nsetools |
| Top gainers/losers | Derived from bhavcopy | nsepython nse_preopen_movers() | Calculated |
| Unusual delivery % | Derived from bhavcopy (>1.5x 20-day avg) | — | Calculated |

### Tier 2 — Future (Global Data)

| Data Point | Source | Library | Cost |
|------------|--------|---------|------|
| US indices (S&P 500, NASDAQ) | Yahoo Finance | yfinance | Free |
| European indices (DAX, FTSE) | Yahoo Finance | yfinance | Free |
| Asian indices (Nikkei, Hang Seng) | Yahoo Finance | yfinance | Free |
| Crude oil (Brent/WTI) | Yahoo Finance | yfinance | Free |
| USD/INR | KiteConnect or Yahoo | Either | Free |
| US bond yields | FRED API | fredapi | Free (API key) |

### Tier 3 — Future (Advanced)

| Data Point | Source | Method | Cost |
|------------|--------|--------|------|
| News headlines | NewsAPI.org | REST API | Free: 100 req/day |
| Earnings calendar | NSE website | nsepython or scrape | Free |
| Options chain + PCR | nsepython option_chain() | Built-in | Free |
| Delivery % historical | Accumulated from daily bhavcopy | Self-calculated | Free |

---

## 4. LLM Configuration

| Parameter | Value |
|-----------|-------|
| Model | Claude Sonnet (claude-sonnet-4-20250514) |
| Upgrade path | Switch to Opus if quality insufficient |
| Est. tokens/run | ~2K input + ~1K output |
| Est. cost/day | ~$0.02 (2 runs) |
| Est. cost/month | ~$0.44 (22 trading days) |
| API | Anthropic API |

---

## 5. Prompt Architecture

### Evening Run

System prompt: "You are HAWK, an expert Indian equity market analyst. Analyze daily NSE data to identify high-probability intraday momentum opportunities for the next trading day."

User input includes: bhavcopy summary (NIFTY 50), FII/DII flows, Nifty/BankNifty/VIX, sector performance, top gainers/losers, unusual delivery %, TradeOS regime.

Instructions: 10-15 stocks ranked by conviction (HIGH/MEDIUM/LOW), each with symbol, direction, entry zone, support, resistance, reasoning, risk flags. JSON output only.

### Morning Update

Input: Evening picks + overnight global data (US close, Asia early, crude, USD/INR, news).

Instructions: Review, remove invalidated, adjust conviction, add new opportunities, flag regime contradictions. Updated JSON output.

---

## 6. Output Format

### JSON — logs/hawk/YYYY-MM-DD_evening.json

```json
{
  "date": "2026-03-12",
  "run": "evening",
  "regime": "bear_trend",
  "market_context": {
    "nifty_close": 24051,
    "nifty_change_pct": -1.2,
    "banknifty_close": 51200,
    "vix": 23.1,
    "fii_net_cr": -2340.5,
    "dii_net_cr": 1890.2
  },
  "watchlist": [
    {
      "rank": 1,
      "symbol": "HCLTECH",
      "direction": "SHORT",
      "conviction": "HIGH",
      "entry_zone": [1380, 1395],
      "support": 1345,
      "resistance": 1410,
      "reasoning": "Broke below 20DMA with 2.1x delivery. IT sector weakest. FII selling IT.",
      "risk_flag": null
    }
  ],
  "metadata": {
    "model": "claude-sonnet-4-20250514",
    "tokens_input": 1850,
    "tokens_output": 920,
    "cost_usd": 0.012,
    "data_sources": ["nsepython", "kiteconnect"],
    "fallbacks_used": []
  }
}
```

### TimescaleDB — table: hawk_picks

Columns: date, run, rank, symbol, direction, conviction, entry_zone_low, entry_zone_high, support, resistance, reasoning, risk_flag, actual_open, actual_close, would_have_pnl

### Telegram (HAWK-Picks channel)

```
🦅 HAWK Evening — 2026-03-11
Regime: bear_trend | VIX: 23.1 | FII: -₹2,340Cr

TOP PICKS:
1. 🔴 HCLTECH SHORT [HIGH] Entry: 1380-1395
   Broke 20DMA, 2.1x delivery, FII selling IT
2. 🔴 AXISBANK SHORT [HIGH] Entry: 1285-1295
   Below VWAP, banking sector weak
3. 🟢 SUNPHARMA LONG [MED] Entry: 1800-1815
   Pharma relative strength, defensive play
```

---

## 7. Telegram Channel Structure

| Channel | Purpose | Content |
|---------|---------|---------|
| TradeOS-Trading | S1 signals, fills, exits, P&L, heartbeat, system alerts | All trading engine notifications |
| HAWK-Picks | AI watchlist picks, morning updates, evaluation scores | HAWK-only output |

Each channel uses a separate bot token or chat_id configured in config/secrets.yaml. Config structure:

```yaml
telegram:
  trading:
    bot_token: "xxx"
    chat_id: "yyy"
  hawk:
    bot_token: "xxx"
    chat_id: "zzz"
```

Rule: Every new module/engine that sends Telegram notifications MUST use its own channel. Never mix notification streams.

---

## 8. Evaluation Framework

Shadow-test HAWK picks against actual market outcomes daily.

| Metric | How Measured |
|--------|-------------|
| Hit rate | % of picks where direction correct (next day close vs open) |
| Conviction accuracy | HIGH picks outperform MEDIUM, MEDIUM > LOW |
| vs S1 actual | Compare HAWK suggestions with S1 trades |
| Sector call accuracy | Did sector direction calls prove correct? |

Minimum 20 trading days before drawing conclusions.

---

## 9. Implementation Plan

| Step | What | Branch | Dependency |
|------|------|--------|-----------|
| 1 | Data collection module (Tier 1) | feature/hawk | nsepython, nsetools in requirements.txt |
| 2 | LLM prompt engine (evening + morning) | feature/hawk | Anthropic API key |
| 3 | Output writers (JSON + DB + Telegram) | feature/hawk | Existing Telegram infra, TimescaleDB |
| 4 | CLI interface (tools/hawk.py) | feature/hawk | None |
| 5 | Evaluation scorer | feature/hawk | Bhavcopy data |
| 6 | Merge to main when all tests pass | main | All steps complete + tested |

New dependencies: nsepython, nsetools (add to requirements.txt on feature/hawk branch)
