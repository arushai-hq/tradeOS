# Brainstorm Session 002 — Deep Research Findings

**Date:** 2026-03-05
**Status:** Complete
**Objective:** Find all existing tools, libraries, AI systems, and MCP connectors relevant to building TradeOS

---

## CRITICAL DISCOVERY — Zerodha Already Has an Official MCP Server

**Repo:** https://github.com/zerodha/kite-mcp-server
**Hosted endpoint:** `https://mcp.kite.trade/mcp` — no installation needed

This means Claude can ALREADY connect to Zerodha natively via MCP today.
Capabilities: Portfolio Management, Order Placement/Modification/Cancel, Live Market Data, Holdings, Positions, Margins

**Impact on TradeOS:** The Execution Engine integration layer is mostly pre-built.

---

## Category 1: Backtesting Frameworks

| Library | Stars | Verdict for TradeOS |
|---------|-------|---------------------|
| **backtesting.py** | 5k+ | ✅ BEST FOR US — Simple API, TA-Lib compatible, fast, beginner-friendly |
| **backtrader** | 13k | ⚠️ Powerful but complex; slower; large learning curve |
| **NautilusTrader** | 9k+ | 🔬 AI-first, production-grade, Rust core — Phase 3 upgrade path |
| **Zipline** | 13k | ❌ Abandoned by Quantopian; outdated |
| **pysystemtrade** | Active | ⚠️ Futures-focused; overkill for Phase 1 |

**Decision:** Use `backtesting.py` for Phase 1. Migrate to NautilusTrader for Phase 3.

---

## Category 2: Technical Indicators (Python)

| Library | Indicators | Verdict |
|---------|-----------|---------|
| **pandas-ta** | 150+ | ✅ BEST — Pure pandas, easy install, includes EMA/RSI/VWAP |
| **TA-Lib** | 150+ | ✅ C-based, fast — requires compilation (harder to install) |
| **ta** | 80+ | ✅ Simple pandas wrapper |

**Decision:** Use `pandas-ta` for Phase 1 (zero-friction install). TA-Lib as optional accelerator later.

---

## Category 3: Data Sources for NSE India

| Source | Type | Cost | Verdict |
|--------|------|------|---------|
| **KiteConnect Historical API** | REST | ₹0 (free with KiteConnect) | ✅ PRIMARY — native to our broker |
| **nsepython** (PyPI v2.97) | NSE scraper | Free | ✅ Backup/validation source |
| **yfinance** | Yahoo Finance scraper | Free | ⚠️ NSE symbols sometimes inconsistent |

**Decision:** KiteConnect Historical as primary. nsepython as backup.

---

## Category 4: AI / LLM Trading Systems (Research Reference)

| System | Type | Key Insight |
|--------|------|------------|
| **TradingAgents** (arxiv 2024) | Multi-agent LLM | Uses news + fundamentals + technicals; outperforms MACD/SMA |
| **AgenticTrading** (Open-Finance-Lab) | Multi-agent + MCP | DAG-based orchestration; memory-augmented learning |
| **FinRL** (AI4Finance) | Deep RL | Train RL agents on historical data; CPU-intensive |
| **AgentQuant** | LLM + backtester | Auto-generates and validates strategies using Gemini; no-code |
| **LLM-TradeBot** | Multi-LLM | Supports Claude, GPT, DeepSeek; paper trade mode |
| **llm_trader** | Claude+GPT4 | Autonomous paper trading with Alpaca API |

**Key Insight:** The trend is moving from pure technical analysis → LLM-assisted decision making. TradeOS Phase 3 should incorporate LLM signal layer on top of technical rules.

---

## Category 5: Indian Market + Zerodha Specific Repos

| Repo | Description | Relevance |
|------|-------------|-----------|
| `zerodha/pykiteconnect` | Official Python KiteConnect SDK | ✅ Core SDK we will use |
| `zerodha/kite-mcp-server` | Official MCP server for Claude | ✅ CRITICAL — pre-built execution bridge |
| `aeron7/Mastering-AlgoTrading-KiteConnect` | Beginner guide + examples | ✅ Reference patterns |
| `ashishkumar30/Stock_Market_Live_Trading_using_AI` | Zerodha + AI + NSE/BSE | Reference implementation |
| Indian-Algorithmic-Trading-Community | Community hub | Community reference |
| `PKScreener` | NSE breakout screener (1k stars) | Potential watchlist filter |

---

## Category 6: Claude + Zerodha MCP Connectors

| Repo | Language | Capabilities |
|------|----------|-------------|
| `zerodha/kite-mcp-server` | Go | **OFFICIAL** — full trading ops |
| `aptro/zerodha-mcp` | Python | Community — full trading ops |
| `arindhimar/ContextCraft` | Python | Claude + Zerodha + natural language orders |
| `codeglyph/kite-mcp` | TypeScript | Portfolio + orders |
| `sukeesh/zerodha-mcp-go` | Go | Read-only — portfolio + quotes |

**Verdict:** The official `zerodha/kite-mcp-server` is the one to use. Hosted at `mcp.kite.trade`.

---

## Technology Stack — Confirmed Decisions

```
Data Layer:
  Primary:   pykiteconnect (historical + live WebSocket)
  Backup:    nsepython
  Format:    Pandas DataFrame

Indicators:
  Primary:   pandas-ta (EMA, RSI, VWAP, ATR)
  Future:    TA-Lib (for performance at scale)

Backtesting:
  Phase 1:   backtesting.py
  Phase 3:   NautilusTrader

Execution Bridge:
  Phase 1:   pykiteconnect REST API (manual code)
  Phase 2+:  zerodha/kite-mcp-server (MCP bridge for Claude)

AI Layer (Future):
  Phase 3:   LLM signal layer (Claude via MCP) on top of technical rules
  Reference: TradingAgents, AgenticTrading architecture patterns

Risk Manager:
  Custom-built (no existing OSS matches our exact rules)
```

---

## Open Questions for Discussion

1. Should we adopt `backtesting.py` or a minimal custom backtester?
2. Should we wire up `kite-mcp-server` immediately to allow Claude to monitor the paper trades in real time?
3. Should we add `nsepython` as a data validator in the Data Engine?
4. Which LLM trading pattern (TradingAgents vs AgenticTrading) should we reference for Phase 3 design?

---

## Resources to Bookmark

- Official KiteConnect docs: https://kite.trade/docs/connect/v3/
- kite-mcp-server hosted: https://mcp.kite.trade/mcp
- backtesting.py docs: https://kernc.github.io/backtesting.py/
- pandas-ta docs: https://github.com/twopirllc/pandas-ta
- NautilusTrader: https://github.com/nautechsystems/nautilus_trader
- TradingAgents paper: https://arxiv.org/abs/2412.20138
- AgenticTrading: https://github.com/Open-Finance-Lab/AgenticTrading
- NSEPython: https://pypi.org/project/nsepython/
