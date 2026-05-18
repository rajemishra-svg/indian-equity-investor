# Indian Equity Long-Term Investor

Agentic AI workflow for disciplined long-term value investing in Indian equities.
Target: 15%+ CAGR over 5+ years with strong margin of safety.

## Project Structure

```
indian-equity-investor/
│
├── skill/                          # Agentic skill definition
│   ├── SKILL.md                    # Master orchestration & 9-step pipeline
│   └── references/
│       ├── data-sources.md         # Source URLs & fetch strategy per data type
│       ├── sector-benchmarks.md    # Sector-specific financial thresholds
│       ├── output-templates.md     # All 7 structured output formats
│       ├── edge-cases.md           # 15 edge case scenarios & handling rules
│       └── error-recovery.md       # 15 error types & recovery playbook
│
├── framework/
│   ├── long_term_investing_context_v2.md   # Core investment philosophy & rules
│   └── CHANGELOG.md                        # Version history & rationale
│
├── analysis/
│   ├── reports/                    # Full BUY / REJECT reports per stock
│   ├── watchlist/
│   │   ├── tier1.md                # Ready to buy (all steps passed)
│   │   ├── tier2.md                # Quality confirmed, valuation pending
│   │   ├── tier3.md                # Universe tracking
│   │   └── rejection-tracker.md   # Rejected stocks with re-eval conditions
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
├── data/
│   ├── raw/                        # Fetched JSON/CSV (gitignored)
│   ├── processed/                  # Normalised data (gitignored)
│   └── macro/                      # RBI, FII/DII, GST, IIP snapshots
│
├── logs/
│   ├── errors/error-log.md         # ER-XX error recovery logs
│   └── qa/qa-log.md                # QA gate pass/fail history
│
├── src/
│   ├── sector/                     # Sector-aware scoring profiles & classifier
│   │   ├── profiles.py             # SectorProfile dataclasses (7 sectors)
│   │   └── classifier.py           # Keyword-based company → sector mapping
│   └── db/
│       ├── schema.py               # SQLite table definitions
│       └── repository.py           # Async CRUD (aiosqlite)
│
├── investor.db                     # SQLite: all analyses + raw data snapshots (gitignored)
├── .gitignore
└── README.md
```

## Quick Start

1. Copy `.env.example` → `.env` and set `ANTHROPIC_API_KEY`.
2. Run `uv sync --extra dev` to install dependencies.
3. Analyse a single stock: `uv run investor analyze RELIANCE`
4. Scan an index for candidates: `uv run investor scan --index "NIFTY 50" --top 5`
5. Review your portfolio: `uv run investor portfolio-review`
6. Query saved results: `uv run investor db-summary` / `investor db-recommendations --type BUY`
7. Update `portfolio/holdings.md` after each trade.

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

## SQLite Persistence

Every analysis is automatically saved to `investor.db` after the pipeline completes. Raw API data (quote, financials, governance, valuation) is stored as JSON snapshots with the actual data source recorded. Results survive process restarts and can be queried with the `investor db-*` commands.

```
investor db-summary                          # ranked view of all analysed stocks
investor db-history RELIANCE                 # trend over time for one ticker
investor db-recommendations --type BUY       # all stocks that passed the full pipeline
investor db-snapshots RELIANCE               # raw data stored for a ticker
```

## Key Contacts / Resources

- NSE: https://www.nseindia.com
- BSE: https://www.bseindia.com
- Screener.in: https://www.screener.in
- Trendlyne: https://trendlyne.com
- SEBI SCORES: https://scores.sebi.gov.in
