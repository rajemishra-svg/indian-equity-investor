# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repository Is

A production Python package implementing a **10-step AI-powered investment analysis pipeline** for Indian equities (NSE/BSE). It uses Claude as the reasoning engine, live financial APIs, and a deterministic gate system to evaluate long-term value investments targeting 15%+ CAGR.

---

## Commands

```bash
# Install deps (uses uv)
uv sync --extra dev

# Run all tests
uv run pytest

# Run a single test file
uv run pytest tests/test_agent/test_step1.py -v

# Run a single test by name
uv run pytest -k "test_pledging_above_10_percent_immediate_fail"

# Lint
uv run ruff check src/ tests/

# CLI — analyse a single stock (full 10-step pipeline)
uv run investor analyze RELIANCE

# CLI — scan a full index, surface the best stocks automatically
uv run investor scan                                       # Nifty 500, top 10
uv run investor scan --index "NIFTY 50" --prescreen-only  # cheap scan, no Claude
uv run investor scan --index "NIFTY 100" --top 5 --min-score 7
uv run investor scan --concurrency 3                      # gentler on Screener

# CLI — portfolio and market mode
uv run investor portfolio
uv run investor correction-scan                            # market mode + Tier-1 entry alerts

# CLI — post-buy monitoring
uv run investor watchlist-alerts                           # live CMP vs DCF target price
uv run investor surveillance                               # sweep all BUY+WATCHLIST for drift/staleness
uv run investor surveillance --days-since 14              # flag analyses older than 14 days

# CLI — SQLite database queries
uv run investor db-summary                          # all analyses ranked by recommendation
uv run investor db-history RELIANCE                 # trend for one ticker (last 10 runs)
uv run investor db-history RELIANCE --limit 5
uv run investor db-recommendations --type BUY       # all BUY results across all dates
uv run investor db-recommendations --type WATCHLIST
uv run investor db-snapshots RELIANCE               # raw data snapshots (quote/fin/gov/val)
uv run investor db-snapshots RELIANCE --type financials
```

Environment: copy `.env.example` → `.env` and set `ANTHROPIC_API_KEY`.

---

## Code Architecture

### Data flow

```
CLI (src/main.py)
  ├─ investor analyze TICKER
  │    └─ InvestmentPipeline.analyze(ticker)         src/agent/pipeline.py
  │         ├─ detect_mode()                          src/agent/mode_detector.py
  │         │    └─ NSEClient → YFinanceClient (fallback) → NORMAL + [MODE UNCONFIRMED]
  │         ├─ _prefetch_data()  ← checks DataCache   src/api/cache.py
  │         │    ├─ NSEClient.get_stock_quote()        src/api/nse.py
  │         │    │    └─ YFinanceClient (fallback if NSE 403)  src/api/yfinance_client.py
  │         │    ├─ ScreenerClient.get_financials()    src/api/screener.py
  │         │    ├─ BSEClient.get_shareholding()       src/api/bse.py
  │         │    │    └─ ScreenerClient.get_shareholding() (fallback)
  │         │    └─ TrendlyneClient.get_valuation_data() src/api/trendlyne.py
  │         │         └─ YFinanceClient (fallback if Trendlyne blocked)
  │         │    └─ classify_sector() → state.sector_name  src/sector/classifier.py
  │         │    └─ is_conglomerate() → state.is_conglomerate  src/sector/classifier.py
  │         │    └─ save_snapshot() × 4 (non-fatal)   src/db/repository.py
  │         ├─ Step0…Step9.run(state)                 src/agent/steps/
  │         │    └─ BaseStep._call_claude() / _agentic_loop()
  │         └─ save_analysis() (non-fatal)             src/db/repository.py
  │
  ├─ investor scan
  │    └─ BatchScanner.scan(index, ...)               src/agent/batch_scanner.py
  │         ├─ Phase 1: get_universe()
  │         │    └─ NSEClient → NSE archives CSV → NIFTY50_FALLBACK (3-layer)
  │         ├─ Phase 2: prescreen_universe()           (concurrent, semaphore-limited)
  │         │    └─ _prescreen_one() per ticker
  │         │         ├─ get_fresh_snapshot() ← SQLite warm cache (7-day TTL)
  │         │         ├─ _fetch_prescreen_data()       (quote + financials + shareholding)
  │         │         └─ Step0PreScreen.run()          (deterministic, no Claude)
  │         ├─ Phase 3: InvestmentPipeline.analyze()   (sequential, top N candidates only)
  │         └─ Phase 4: rank_results()                 (BUY→conviction→MoS→governance)
  │
  ├─ investor watchlist-alerts
  │    └─ get_watchlist_with_targets()                src/db/repository.py
  │         └─ YFinanceClient.get_stock_quote() per ticker (live CMP)
  │
  ├─ investor surveillance
  │    └─ get_all_tracked_tickers()                   src/db/repository.py
  │         └─ YFinanceClient.get_stock_quote() per ticker (live CMP)
  │
  └─ investor db-summary / db-history / db-recommendations / db-snapshots
       └─ AnalysisRepository                          src/db/repository.py
            └─ aiosqlite → investor.db (SQLite)
```

`AnalysisState` (`src/models.py`) is the single mutable object passed through every step. It accumulates raw data, step results, flags, errors, and the final recommendation. When `state.is_terminated` is true, all steps except Step9 are skipped.

### Step model-routing and loop strategy

| Step | Implementation | Model | Why |
|------|---------------|-------|-----|
| 0 — Pre-screen | Deterministic (no LLM) | — | Pure arithmetic |
| 1 — Governance | Deterministic + 1 Haiku call (capital allocation score) | `model_light` | Tiny JSON `{"score":N}` |
| 2 — Moat | Agentic loop (≤6 iterations, web tools) | `model_heavy` | Qualitative research + concall analysis |
| 3 — Financials | Deterministic (no LLM) | — | Pure arithmetic |
| 4 — Tailwinds | Single Haiku call (no tools) | `model_light` | Context from Step 2 state is sufficient |
| 5 — Valuation | Deterministic + 1 Haiku DCF call | `model_light` | Structured JSON, no research needed |
| 6 — Technical | Deterministic (no LLM) | — | Arithmetic from quote |
| 7 — Peers | Agentic loop (≤8 iterations, web tools) | `model_heavy` | Requires live peer data fetches |
| 8 — Premortem | Single Haiku call (no tools) | `model_light` | All risk context already in state |
| 9 — Output | 1 Haiku call (thesis) + deterministic format | `model_light` | Narrative from existing state |

`model_heavy = claude-sonnet-4-6`, `model_light = claude-haiku-4-5-20251001`. Both defined in `src/config.py`.

### BaseStep (`src/agent/steps/base.py`)

All steps inherit from `BaseStep`. Two helpers:

- `_call_claude(system, messages, model, max_tokens)` — single-turn call with ephemeral prompt caching on the system prompt.
- `_agentic_loop(system, initial_message, tools, model, max_tokens, max_iterations)` — iterates until `stop_reason == "end_turn"` or tool calls exhaust `max_iterations`. Dispatches tools via `execute_tool()` in `src/agent/tools.py`.

Every `_call_claude` / `_agentic_loop` call logs `input_tokens`, `output_tokens`, `elapsed_seconds`.

### Hard gates and termination

Three steps terminate the pipeline on failure:
- **Step 0**: Pre-screen score < 5/9
- **Step 1**: Governance score < 9/15 OR any immediate trigger (pledging > 10%, SEBI fraud, RPT > 20%, going concern, mid-year auditor resign)
- **Step 3**: Any hard financial trigger (CFO/NP < 50%, D/E > 3, ICR < 3) OR score < 5/7

On termination: `state.terminated_at_step` and `state.termination_reason` are set, `state.recommendation_type = "REJECT"`. Step 9 always runs to generate the REJECTION_LOG output. Step 5 failure sets `recommendation_type = "WATCHLIST"` (not a hard gate — pipeline continues).

### Edge Case Handlers (baked into pipeline logic)

| Code | Condition | Enforcement |
|------|-----------|-------------|
| EC-01 | Pre-profit (EBITDA margin < 0 or both PAT CAGRs < –20%) | Step 5 skips DCF/PE/PEG/EV-EBITDA; Step 9 caps suggested allocation ≤ 4% |
| EC-02 | Cyclical sector (`commodities_cyclical`) | Step 5 DCF uses `ebitda_margin_5y_avg` instead of latest year |
| EC-04 | Conglomerate detected by `is_conglomerate()` | Pipeline sets `state.is_conglomerate`; Step 5 adds SOTP advisory flag |
| EC-06 | MNC subsidiary (`GovernanceData.is_mnc = True`) | Step 0 waives the promoter holding ≥ 40% check |
| EC-11 | Low liquidity (`avg_daily_value_cr < ₹5 Cr`) | Step 0 adds a non-scoring `[EC-11: LOW LIQUIDITY]` flag |
| ER-05 | ≥ 5 error tags accumulated by Step 9 | Auto-downgrades BUY → WATCHLIST Tier 1 with `[ER-05: AUTO-DOWNGRADE]` flag |

### Error Tags vs Data Flags

**Error tags** (`state.error_tags`) count API source failures. Accumulating ≥ 5 triggers ER-05 auto-downgrade.

| Code | Trigger |
|------|---------|
| ER-01 | NSE quote failed (before YFinance fallback) |
| ER-02 | Screener financials failed |
| ER-03 | Trendlyne valuation failed (before YFinance fallback) |
| ER-04 | All shareholding sources failed (NSE + BSE + Screener) |
| ER-05 | ≥ 5 accumulated errors → BUY auto-downgraded to WATCHLIST |
| ER-08 | Both NSE and YFinance Nifty50 failed → mode defaults to NORMAL |

**Data flags** (`state.all_data_flags`) are quality annotations — `[DATA UNVERIFIED]`, `[ESTIMATE]`, `[SECTOR OVERRIDE]`, `[EC-XX]`, `[ER-XX]`, `[POSITIVE]` etc. — that appear verbatim in the final report.

### Sector-Aware Scoring (`src/sector/`)

Every analysis is assigned a `sector_name` before Step 0 runs. Sector-specific thresholds replace the single default threshold for every metric that has a `None` or different value in the profile. `None` means "waive this check entirely".

**Classifier** (`src/sector/classifier.py`): keyword-set matching against company name, with moat narrative enrichment after Step 2 for ambiguous cases. Priority order: `financial_services` → `defence_govt` → `infrastructure_utility` → `capital_goods` → `commodities_cyclical` → `recently_listed` → `default`.

Also exposes `is_conglomerate(company_name, ticker)` — checks against `_CONGLOMERATE_NAMES` (Tata, Birla, Bajaj Holdings, Mahindra, Reliance, ITC, L&T, Vedanta, JSW Holdings, generic "holding company" patterns).

**Profiles** (`src/sector/profiles.py`): seven `SectorProfile` dataclasses, each overriding threshold fields:

| Profile | Key overrides |
|---------|--------------|
| `default` | Standard thresholds throughout |
| `financial_services` | D/E, ICR, CFO/NP all `None` (waived); EV/EBITDA skipped; ROE min 12% |
| `defence_govt` | CFO/NP min 40% (hurdle), 25% (hard trigger); revenue/PAT CAGR min 8/10% |
| `infrastructure_utility` | D/E max 3.0, hard trigger 5.0; ICR min 3.0; WACC +0.5% |
| `capital_goods` | CFO/NP min 55% (hurdle), 35% (hard trigger) |
| `commodities_cyclical` | Revenue/PAT CAGR min 8/10%; WACC +1.0%; `use_normalized_ebitda=True` |
| `recently_listed` | 5Y revenue/PAT/ROE/ROCE all `None` (waived — insufficient history) |

**Pipeline integration**: `classify_sector()` and `is_conglomerate()` are called in `_prefetch_data()`. All sector overrides are logged with `[SECTOR OVERRIDE: ...]`. Conglomerate detection logs `[EC-04: CONGLOMERATE detected — SOTP recommended]`.

**Step integration**:
- **Step 0**: all 9 pre-screen metrics use `profile.*` thresholds; `None` = auto-pass; EC-06 waives promoter holding for MNCs; EC-11 flags low liquidity
- **Step 1**: `profile.capital_allocation_note` injected into capital allocation prompt; P3-2 insider activity signal enriched and flagged
- **Step 3**: all 7 hurdles and 3 hard triggers use `profile.*` thresholds; P1-1 bank KPIs, P1-3 WC deterioration, P1-4 earnings quality, P2-3 trend direction all evaluated as soft flags
- **Step 5**: `wacc += profile.wacc_adjustment`; EV/EBITDA skipped when `not profile.ev_ebitda_applicable`; EC-01 skips earnings-based methods; EC-02 normalises EBITDA; EC-04 adds SOTP note

### Enrichment Features (P1–P3)

| Feature | Where | What it does |
|---------|-------|-------------|
| P1-1 Bank KPIs | `screener.py` + Step 3 | GNPA, NNPA, NIM, ROA, CAR extracted and soft-flagged |
| P1-2 PE percentile | `yfinance_client.py` | `_compute_pe_percentile()` builds 5Y trailing-PE series; used as Step 5 Method 1 |
| P1-3 Working capital | `screener.py` + Step 3 | Debtor/inventory days computed; >30% deterioration flagged |
| P1-4 Earnings quality | `screener.py` + Step 3 | Other income > 15% of revenue flagged |
| P2-1 Surveillance | `main.py` + `repository.py` | `investor surveillance` sweeps all BUY+WATCHLIST for staleness/price drift |
| P2-2 Watchlist alerts | `main.py` + `repository.py` | `investor watchlist-alerts` compares live CMP vs stored DCF target price |
| P2-3 ROCE/ROE trends | `screener.py` + Step 3 | Recent 2Y vs prior 3Y delta; deteriorating/improving flagged |
| P3-1 Concall quality | Step 2 system prompt | Claude searches concall transcripts; `management_guidance_reliability` stored on `MoatAssessment` |
| P3-2 Insider activity | Step 1 enrichment loop | Claude searches BSE bulk/block deals; `insider_net_buying_3m` stored on `GovernanceData` |
| P3-3 Conglomerate | `classifier.py` + pipeline + Step 5 | `is_conglomerate()` sets `state.is_conglomerate`; EC-04 SOTP flag added |
| P3-4 Volume analysis | `yfinance_client.py` + Step 6 | `_compute_volume_trend_sync()` on 30-day history; wired to Technical Signal 4 |

### Valuation methods (Step 5)

Five methods, each scored as in-buy-zone or not:
1. **PE percentile** (10Y historical from yfinance): EXCELLENT (<30th), FAIR (30–60th)
2. **PEG ratio**: EXCELLENT (<1.0), FAIR (1.0–1.3)
3. **DCF**: weighted average of base/bull/bear scenarios; MoS must meet `state.required_mos_pct`; EC-02 uses 5Y avg EBITDA margin for cyclical sectors
4. **FCF yield**: FAIR (3–5%), ATTRACTIVE (>5%)
5. **EV/EBITDA**: EXCELLENT (<12x), FAIR (12–20x); skipped for `financial_services`

Gate: ≥2 methods in buy zone AND DCF MoS met → PASS_GREEN; ≥1 → PASS_CONDITIONAL; 0 → FAIL (Watchlist Tier 2).

WACC is risk-adjusted: 13% large-cap stable, 15% mid-cap, 16.5% small-cap, +1% for cyclical sectors.

`target_buy_price` is computed at WATCHLIST save time as `dcf_intrinsic_weighted × (1 – required_mos_pct/100)` and stored in SQLite. `watchlist-alerts` uses this to fire entry alerts without re-running the pipeline.

### BatchScanner (`src/agent/batch_scanner.py`)

The scanner's two-phase design exists to control Claude API cost:

- **Phase 2 (pre-screen)** runs Step 0 only — purely deterministic, zero LLM calls. All 500 tickers in an index can be pre-screened for the cost of HTTP requests alone. Concurrency is capped by `asyncio.Semaphore(concurrency)` (default 8). **SQLite warm cache**: `get_fresh_snapshot()` is checked before any HTTP call; data fresher than 7 days skips Screener entirely, making repeated weekly scans near-free.
- **Phase 3 (full pipeline)** runs sequentially — one `InvestmentPipeline.analyze()` call per candidate, each managing its own HTTP client sessions. The `DataCache` singleton means any data already fetched in Phase 2 is reused here at no extra HTTP cost.
- **Fallback universe**: NSE JSON API → NSE archives CSV → `NIFTY50_FALLBACK` (hardcoded 50 tickers).
- **Ranking** (`rank_results()`): BUY > WATCHLIST > PEER_SWITCH > REJECT, then by conviction HIGH > MEDIUM > LOW, then MoS% descending, then governance score descending.

### API clients (`src/api/`)

All clients except `YFinanceClient` extend `BaseHTTPClient` which provides `httpx.AsyncClient` with tenacity retry (3 attempts, exponential backoff) on `TimeoutException` and `ConnectError`.

**NSE quirk**: must visit the homepage first to establish session cookies — `_establish_session()` is called automatically on first API request. NSE aggressively blocks bots with 403s in non-browser environments.

**YFinanceClient** (`src/api/yfinance_client.py`): wraps the synchronous `yfinance` library in `run_in_executor()` calls. Provides `get_stock_quote()` (with `avg_daily_value_cr` and `volume_trend_down_days`), `get_valuation_data()` (with `pe_10y_percentile` via `_compute_pe_percentile()`), and `get_nifty50()`. NSE tickers map to Yahoo Finance by appending `.NS`. Data is ~15–20 min delayed — marked `is_stale=True`. Has a no-op async context manager.

**DataCache** (`src/api/cache.py`): module-level singleton with TTL-based invalidation. TTLs: quote/valuation = 1 hour, financials/shareholding = 24 hours. `None` is never cached.

### SQLite Datastore (`src/db/`)

Every completed analysis is persisted to `investor.db`. Writes are always wrapped in `try/except` — DB failures never break pipeline output.

**Schema** (`src/db/schema.py`): two tables:
- `analyses` — one row per `(ticker, analysis_date)`. `UNIQUE(ticker, analysis_date)` + `INSERT OR REPLACE`. Includes `target_buy_price` (DCF-derived entry price stored at WATCHLIST creation).
- `data_snapshots` — one row per `(ticker, snapshot_date, data_type)`. Raw API payload as JSON. `source` column records actual provider used.

**Repository** (`src/db/repository.py`): all async functions using `aiosqlite`:
- `init_db` / `save_analysis` / `save_snapshot` — write operations
- `get_fresh_snapshot(db_path, ticker, data_type, max_age_hours)` — warm cache read for batch scanner
- `get_latest_analysis` / `get_analysis_history` / `list_recommendations` / `get_summary` — read operations
- `get_watchlist_with_targets()` — Tier-1 WATCHLIST entries with stored DCF target prices (used by `watchlist-alerts`)
- `get_all_tracked_tickers()` — all BUY + WATCHLIST entries (used by `surveillance`)

**Pipeline integration** (`src/agent/pipeline.py`):
- After `_prefetch_data`: saves 4 raw snapshots with actual source names
- After `pipeline_complete` log: calls `save_analysis()` — this is the single authoritative persistence point; no separate markdown files are written

**Portfolio tracking** (`src/portfolio/tracker.py`): reads/writes `portfolio/` markdown files for actual trade records (holdings, transactions, tax). Watchlist and rejection tracking is now fully in SQLite.

**CLI commands** (`src/main.py`):
- `investor db-summary` — ranked table of every analysed ticker
- `investor db-history TICKER [--limit N]` — per-ticker trend over time
- `investor db-recommendations --type BUY|WATCHLIST|PEER_SWITCH|REJECT` — filter by outcome
- `investor db-snapshots TICKER [--type quote|financials|governance|valuation]` — inspect raw data
- `investor watchlist-alerts` — live CMP vs stored target buy price for every WATCHLIST ticker
- `investor surveillance [--days-since N]` — staleness + price drift sweep for all tracked tickers

### per-step `max_tokens` budget (src/config.py)

```python
max_tokens       = 4096   # agentic loops
max_tokens_mini  = 256    # tiny JSON (capital allocation score)
max_tokens_short = 600    # DCF / tailwind / premortem JSON
max_tokens_thesis= 512    # narrative thesis
```

---

## Investment Domain Rules

These rules are baked into the step prompts and scoring logic — do not weaken them in refactors.

**Governance hierarchy**: Governance > Business Quality > Financials > Valuation. Any single immediate trigger in Step 1 = REJECT regardless of other scores.

**Data integrity**: Never fabricate metrics. Label estimates `[ESTIMATE]`, unavailable data `[NOT AVAILABLE]`, unverified data `[DATA UNVERIFIED]`. Source priority: NSE/BSE filings > Screener.in > Trendlyne > broker reports.

**Margin of Safety** (required for DCF gate in Step 5):

| Cap | Normal Mode | Correction Mode |
|-----|-------------|-----------------|
| Large Cap (Nifty 100) | 20–30% | 15–25% |
| Mid Cap | 30–40% | 25–35% |
| Small Cap | 40–50% | 35–45% |

**Market modes** (set by `detect_mode()` from Nifty 52W high):
- Normal: Nifty < 8% below peak
- Correction: 8–15% below
- Maximum Opportunity: > 15% below

If Nifty data unavailable, defaults to Normal + adds `[MODE UNCONFIRMED]` flag.

**Watchlist tiers**: Tier 1 = all steps passed + valuation in buy zone (max 15). Tier 2 = Steps 1–5 passed, valuation not attractive (max 30). Tier 3 = Steps 1–3 passed, research pending. All tiers persisted to `investor.db` — no markdown files.

**Tranche plan** (always in BUY output): T1 40% @ CMP, T2 35% @ CMP×0.92, T3 25% @ CMP×0.85.

**Tax**: LTCG 12.5% on gains > ₹1.25L after 1 year; STCG 20% under 1 year. After any trade, update all four files in `portfolio/`.

---

## Reference Files (`skill/references/`)

Read situationally — these are AI context documents, not application code:

| File | When |
|------|------|
| `data-sources.md` | Start of every analysis — exact source URLs per data type |
| `sector-benchmarks.md` | Step 3 — qualitative sector benchmarks (NIM, GNPA, ARPOB etc. not in pipeline code) |
| `output-templates.md` | Step 9 — exact structured output formats |
| `edge-cases.md` | When any edge-case flag fires — full taxonomy including EC-07 through EC-15 not yet in code |
| `error-recovery.md` | When a data fetch fails or sources conflict |

Note: EC-01, EC-02, EC-04, EC-06, EC-11, and ER-01 through ER-05/ER-08 are **fully implemented in code** and fire automatically. The `skill/references/edge-cases.md` file covers all 15 EC codes including those not yet automated (EC-07 turnaround, EC-08 correction entry, EC-09 peer dominance, EC-10 PSU, EC-12 export-oriented, EC-13 post-acquisition, EC-14 regulatory overhang, EC-15 holding company).
