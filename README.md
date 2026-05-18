# Indian Equity Long-Term Investor

Agentic AI workflow for disciplined long-term value investing in Indian equities.
Target: 15%+ CAGR over 5+ years with strong margin of safety.

## Project Structure

```
indian-equity-investor/
│
├── skill/                          # Agentic skill definition
│   ├── SKILL.md                    # Master orchestration & 10-step pipeline
│   └── references/
│       ├── data-sources.md         # Source URLs & fetch strategy per data type
│       ├── sector-benchmarks.md    # Sector-specific financial thresholds
│       ├── output-templates.md     # All 7 structured output formats
│       ├── edge-cases.md           # Edge case taxonomy and handling rules
│       └── error-recovery.md       # Error recovery playbook
│
├── analysis/
│   ├── reports/                    # Full BUY / REJECT reports per stock
│   ├── quarterly/                  # Surveillance reports (QxFYxx/)
│   ├── sector-scans/               # Sector-level batch scan outputs
│   └── corrections/                # Correction-mode opportunity logs
│
├── portfolio/
│   ├── holdings.md                 # Current positions, avg cost, allocation %
│   ├── transaction-log.md          # All buy/sell entries (date, price, qty)
│   ├── exit-tracker.md             # Completed exits with thesis & P&L
│   └── tax-tracker.md              # LTCG/STCG status per position
│
├── src/
│   ├── agent/
│   │   ├── pipeline.py             # InvestmentPipeline orchestrator
│   │   ├── batch_scanner.py        # BatchScanner (2-phase: pre-screen + full)
│   │   ├── mode_detector.py        # Nifty market mode detection
│   │   ├── tools.py                # Tool dispatch (web_search, web_fetch)
│   │   └── steps/
│   │       ├── base.py             # BaseStep (_call_claude, _agentic_loop)
│   │       ├── step0_prescreen.py  # Deterministic pre-screen (no LLM)
│   │       ├── step1_governance.py # Governance gate + enrichment loop
│   │       ├── step2_moat.py       # Agentic moat + concall research
│   │       ├── step3_financials.py # Deterministic financial gate
│   │       ├── step4_tailwinds.py  # Single Haiku call
│   │       ├── step5_valuation.py  # Deterministic DCF + Haiku call
│   │       ├── step6_technical.py  # Deterministic technical signals
│   │       ├── step7_peers.py      # Agentic peer benchmarking
│   │       ├── step8_premortem.py  # Single Haiku risk analysis
│   │       └── step9_output.py     # Thesis + structured output
│   ├── api/
│   │   ├── nse.py                  # NSEClient (quote, shareholding, index)
│   │   ├── screener.py             # ScreenerClient (financials, shareholding)
│   │   ├── bse.py                  # BSEClient (shareholding fallback)
│   │   ├── trendlyne.py            # TrendlyneClient (valuation, governance)
│   │   ├── yfinance_client.py      # YFinanceClient (NSE/Trendlyne fallback)
│   │   ├── base.py                 # BaseHTTPClient (httpx + tenacity retry)
│   │   └── cache.py                # DataCache singleton (TTL-based)
│   ├── sector/
│   │   ├── profiles.py             # SectorProfile dataclasses (7 sectors)
│   │   └── classifier.py           # Keyword classifier + is_conglomerate()
│   ├── db/
│   │   ├── schema.py               # SQLite table definitions
│   │   └── repository.py           # Async CRUD (aiosqlite)
│   ├── portfolio/
│   │   └── tracker.py              # PortfolioTracker (trade records only)
│   ├── models.py                   # AnalysisState, GovernanceData, MoatAssessment …
│   ├── config.py                   # Settings (API keys, thresholds, model names)
│   ├── logging_config.py           # structlog JSON logging
│   └── main.py                     # CLI (typer) — analyze, scan, portfolio, db-*
│
├── tests/                          # pytest test suite
├── investor.db                     # SQLite: analyses + raw data snapshots (gitignored)
├── CLAUDE.md                       # Claude Code project instructions
├── .env.example
├── .gitignore
└── README.md
```

## Quick Start

1. Copy `.env.example` → `.env` and set `ANTHROPIC_API_KEY`.
2. Run `uv sync --extra dev` to install dependencies.
3. Analyse a single stock: `uv run investor analyze RELIANCE`
4. Scan an index for candidates: `uv run investor scan --index "NIFTY 50" --top 5`
5. Check watchlist alerts (price vs target): `uv run investor watchlist-alerts`
6. Monitor held positions: `uv run investor surveillance`
7. Query saved results: `uv run investor db-summary`
8. Update `portfolio/holdings.md` after each trade via: `uv run investor portfolio`

**All watchlist and rejection tracking is handled automatically by SQLite** (`investor.db`).
Watchlist tiers, target buy prices, and conviction levels are stored at analysis time and
queried by `watchlist-alerts` and `db-recommendations`.

## Operating Modes

| Mode | Trigger | Posture |
|------|---------|---------|
| Normal | Nifty < 8% below 52W high | Standard MoS; monthly scan |
| Correction | Nifty 8–15% below 52W high | Opportunistic; Tier-1 entry |
| Maximum Opportunity | Nifty > 15% below 52W high | Aggressive; Tier-1 + Tier-2 |

## Sector-Aware Scoring

All threshold checks in Steps 0, 3, and 5 are driven by a `SectorProfile` — a dataclass that holds per-sector overrides for every financial metric. `None` means the check is waived entirely for that sector. The pipeline classifies each company's sector before Step 0 runs, using keyword matching on company name (enriched by moat narrative after Step 2).

| Sector | Key overrides |
|--------|--------------|
| `financial_services` | D/E, ICR, CFO/NP waived; EV/EBITDA skipped |
| `defence_govt` | CFO/NP threshold relaxed (milestone billing); CAGR floors lower |
| `infrastructure_utility` | D/E up to 3×; WACC +0.5% |
| `capital_goods` | CFO/NP threshold relaxed (long WC cycles) |
| `commodities_cyclical` | CAGR floors lower; WACC +1.0% |
| `recently_listed` | All 5Y metrics waived (no listed history) |

The classifier returns a confidence score (1.0 = name-match, 0.7 = narrative-only, 0.5 = default fallback); companies with confidence < 0.7 receive a `[SECTOR AMBIGUOUS]` flag in the final report.

## Pipeline Steps

| Step | Name | Model | Strategy |
|------|------|-------|----------|
| 0 | Pre-screen | — | Deterministic arithmetic (no LLM) |
| 1 | Governance | Haiku | Deterministic score + enrichment loop (≤ 4 iter) |
| 2 | Moat | Sonnet | Agentic loop (≤ 6 iter, web tools) |
| 3 | Financials | — | Deterministic arithmetic (no LLM) |
| 4 | Tailwinds | Haiku | Single call |
| 5 | Valuation | Haiku | Deterministic DCF + 1 Haiku call |
| 6 | Technical | — | Deterministic signals |
| 7 | Peers | Sonnet | Agentic loop (≤ 8 iter, web tools) |
| 8 | Premortem | Haiku | Single call |
| 9 | Output | Haiku | Thesis + structured format |

Hard gates: Steps 0, 1, 3 can terminate the pipeline → `recommendation_type = REJECT`.
Step 5 failure → `recommendation_type = WATCHLIST` (pipeline continues for Steps 6–8).

## Error & Edge-Case Codes

| Code | Description | Enforced |
|------|------------|---------|
| ER-01 | NSE quote fetch failed → YFinance fallback | pipeline._prefetch_data |
| ER-02 | Screener financials failed | pipeline._prefetch_data |
| ER-03 | Trendlyne valuation failed → YFinance fallback | pipeline._prefetch_data |
| ER-04 | All shareholding sources failed | pipeline._prefetch_data |
| ER-05 | ≥ 5 error tags → BUY auto-downgraded to WATCHLIST | step9_output |
| ER-06 | Agentic loop (moat/peer) hit max iterations — research may be partial | step2_moat, step7_peers |
| ER-07 | ≥ 3 of 4 raw data snapshots failed to save to SQLite | pipeline._prefetch_data |
| EC-01 | Pre-profit company — modified valuation criteria | step5_valuation |
| EC-02 | Cyclical sector — mid-cycle normalised EBITDA | step5_valuation + sector profile |
| EC-04 | Conglomerate — SOTP valuation recommended | pipeline + step5_valuation |
| EC-06 | Recently listed — 5Y metrics waived | step0_prescreen + sector profile |
| EC-11 | Low volume/liquidity | step6_technical |

## SQLite Persistence

Every analysis is automatically saved to `investor.db` after the pipeline completes. Raw API data (quote, financials, governance, valuation) is stored as JSON snapshots with the actual data source recorded. Results survive process restarts and can be queried with the `investor db-*` commands.

If 3 or more of the 4 raw data snapshots fail to save (e.g. disk full, bad db_path), the pipeline adds an `ER-07` error tag and a `[ER-07: DB SNAPSHOT FAILURES]` flag — the analysis result is still returned.

```bash
uv run investor db-summary                          # ranked view of all analysed stocks
uv run investor db-history RELIANCE                 # trend over time for one ticker
uv run investor db-recommendations --type BUY       # all stocks that passed the full pipeline
uv run investor db-snapshots RELIANCE               # raw data stored for a ticker
uv run investor watchlist-alerts                    # price-vs-target alerts (Tier 1 watchlist)
uv run investor surveillance                        # quarterly thesis check for held positions
```

## Key Contacts / Resources

- NSE: https://www.nseindia.com
- BSE: https://www.bseindia.com
- Screener.in: https://www.screener.in
- Trendlyne: https://trendlyne.com
- SEBI SCORES: https://scores.sebi.gov.in
