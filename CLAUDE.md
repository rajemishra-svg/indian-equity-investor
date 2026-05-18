# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repository Is

A production Python package implementing a **9-step AI-powered investment analysis pipeline** for Indian equities (NSE/BSE). It uses Claude as the reasoning engine, live financial APIs, and a deterministic gate system to evaluate long-term value investments targeting 15%+ CAGR.

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

# CLI — analyse a single stock (full 9-step pipeline)
uv run investor analyze RELIANCE

# CLI — scan a full index, surface the best stocks automatically
uv run investor scan                                       # Nifty 500, top 10
uv run investor scan --index "NIFTY 50" --prescreen-only  # cheap scan, no Claude
uv run investor scan --index "NIFTY 100" --top 5 --min-score 7
uv run investor scan --concurrency 3                      # gentler on Screener

# CLI — show portfolio, add to watchlist, check market mode
uv run investor portfolio
uv run investor watchlist HDFC 2 --reason "Valuation too high"
uv run investor correction-scan

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
  │         ├─ _prefetch_data()  ← checks DataCache   src/api/cache.py
  │         │    ├─ NSEClient.get_stock_quote()        src/api/nse.py
  │         │    │    └─ YFinanceClient (fallback if NSE 403)  src/api/yfinance_client.py
  │         │    ├─ ScreenerClient.get_financials()    src/api/screener.py
  │         │    ├─ BSEClient.get_shareholding()       src/api/bse.py
  │         │    └─ TrendlyneClient.get_valuation_data() src/api/trendlyne.py
  │         │         └─ YFinanceClient (fallback if Trendlyne blocked) src/api/yfinance_client.py
  │         │    └─ classify_sector()  → state.sector_name  src/sector/classifier.py
  │         │    └─ save_snapshot() × 4 (non-fatal)   src/db/repository.py
  │         ├─ Step0…Step9.run(state)                 src/agent/steps/
  │         │    └─ BaseStep._call_claude() / _agentic_loop()
  │         └─ save_analysis() (non-fatal)             src/db/repository.py
  │
  ├─ investor scan
  │    └─ BatchScanner.scan(index, ...)               src/agent/batch_scanner.py
  │         ├─ Phase 1: get_universe()
  │         │    └─ NSEClient.get_index_constituents() (falls back to NIFTY50_FALLBACK)
  │         ├─ Phase 2: prescreen_universe()           (concurrent, semaphore-limited)
  │         │    └─ _prescreen_one() per ticker
  │         │         ├─ _fetch_prescreen_data()       (quote + financials + shareholding)
  │         │         └─ Step0PreScreen.run()          (deterministic, no Claude)
  │         ├─ Phase 3: InvestmentPipeline.analyze()   (sequential, top N candidates only)
  │         └─ Phase 4: rank_results()                 (BUY→conviction→MoS→governance)
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
| 2 — Moat | Agentic loop (≤6 iterations, web tools) | `model_heavy` | Qualitative research |
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

### Sector-Aware Scoring (`src/sector/`)

Every analysis is assigned a `sector_name` before Step 0 runs. Sector-specific thresholds replace the single default threshold for every metric that has a `None` or different value in the profile. `None` means "waive this check entirely".

**Classifier** (`src/sector/classifier.py`): keyword-set matching against company name, with moat narrative enrichment after Step 2 for ambiguous cases. Priority order: `financial_services` → `defence_govt` → `infrastructure_utility` → `capital_goods` → `commodities_cyclical` → `recently_listed` → `default`.

**Profiles** (`src/sector/profiles.py`): seven `SectorProfile` dataclasses, each overriding threshold fields on `SectorProfile`:

| Profile | Key overrides |
|---------|--------------|
| `default` | Standard thresholds throughout |
| `financial_services` | D/E, ICR, CFO/NP all `None` (waived); EV/EBITDA skipped; ROE min 12% |
| `defence_govt` | CFO/NP min 40% (hurdle), 25% (hard trigger); revenue/PAT CAGR min 8/10% |
| `infrastructure_utility` | D/E max 3.0, hard trigger 5.0; ICR min 3.0; WACC +0.5% |
| `capital_goods` | CFO/NP min 55% (hurdle), 35% (hard trigger) |
| `commodities_cyclical` | Revenue/PAT CAGR min 8/10%; WACC +1.0% |
| `recently_listed` | 5Y revenue/PAT/ROE/ROCE all `None` (waived — insufficient history) |

**Pipeline integration**: `classify_sector()` is called in `_prefetch_data()` right after data fetch and sets `state.sector_name`. Step 0 re-classifies if not already set (for standalone step use). All sector overrides are logged to `state.all_data_flags` with `[SECTOR OVERRIDE: ...]` prefix.

**Step integration**:
- **Step 0**: all 9 pre-screen metrics use `profile.*` thresholds; `None` = auto-pass
- **Step 1**: `profile.capital_allocation_note` injected into the Claude capital allocation prompt
- **Step 3**: all 7 hurdles and 3 hard triggers use `profile.*` thresholds; `None` = waived
- **Step 5**: `wacc += profile.wacc_adjustment`; EV/EBITDA method skipped when `not profile.ev_ebitda_applicable`

### Valuation methods (Step 5)

Five methods, each scored as in-buy-zone or not:
1. PE percentile (10Y historical): EXCELLENT (<30th), FAIR (30–60th)
2. PEG ratio: EXCELLENT (<1.0), FAIR (1.0–1.3)
3. DCF: weighted average of base/bull/bear scenarios; MoS must meet `state.required_mos_pct`
4. FCF yield: FAIR (3–5%), ATTRACTIVE (>5%)
5. EV/EBITDA: EXCELLENT (<12x), FAIR (12–20x)

Gate: ≥2 methods in buy zone AND DCF MoS met → PASS_GREEN; ≥1 → PASS_CONDITIONAL; 0 → FAIL (Watchlist Tier 2).

WACC is risk-adjusted: 13% large-cap stable, 15% mid-cap, 16.5% small-cap, +1% for cyclical sectors.

### BatchScanner (`src/agent/batch_scanner.py`)

The scanner's two-phase design exists to control Claude API cost:

- **Phase 2 (pre-screen)** runs Step 0 only — purely deterministic, zero LLM calls. All 500 tickers in an index can be pre-screened for the cost of HTTP requests alone. Concurrency is capped by `asyncio.Semaphore(concurrency)` (default 5) to avoid Screener.in rate limits.
- **Phase 3 (full pipeline)** runs sequentially — one `InvestmentPipeline.analyze()` call per candidate, each managing its own HTTP client sessions. The `DataCache` singleton means any data already fetched in Phase 2 is reused here at no extra HTTP cost.
- **Fallback universe**: if NSE's index API returns 403, `NIFTY50_FALLBACK` (hardcoded 50 tickers) is used so the scan can still proceed in dev/CI environments.
- **Ranking** (`rank_results()`): BUY > WATCHLIST > PEER_SWITCH > REJECT, then by conviction HIGH > MEDIUM > LOW, then MoS% descending, then governance score descending.

### API clients (`src/api/`)

All clients except `YFinanceClient` extend `BaseHTTPClient` which provides `httpx.AsyncClient` with tenacity retry (3 attempts, exponential backoff) on `TimeoutException` and `ConnectError`.

**NSE quirk**: must visit the homepage first to establish session cookies — `_establish_session()` is called automatically on first API request. The JSON quote endpoint (`/api/quote-equity?symbol=X`) and index constituents endpoint (`/api/equity-stockIndices?index=X`) both require these cookies. NSE aggressively blocks bots with 403s in non-browser environments.

**YFinanceClient** (`src/api/yfinance_client.py`): wraps the synchronous `yfinance` library in `asyncio.get_event_loop().run_in_executor()` calls so it doesn't block the event loop. Provides `get_stock_quote()` and `get_valuation_data()`. NSE tickers map to Yahoo Finance symbols by appending `.NS` (e.g. `RELIANCE` → `RELIANCE.NS`). Data is ~15–20 min delayed — marked with `is_stale=True` on the returned `StockQuote`. Used as automatic fallback in `pipeline._prefetch_data()` when NSE quote returns None and in `batch_scanner._fetch_prescreen_data()` when NSE is blocked. Also used as fallback for Trendlyne valuation data (provides PE/PB/PEG via `yf.Ticker.info`; historical percentile data is not available from Yahoo Finance). Has a no-op async context manager so it fits the same `async with` pattern as HTTP clients.

**DataCache** (`src/api/cache.py`): module-level singleton with TTL-based invalidation. `pipeline._prefetch_data` checks the cache before each HTTP call and writes results back on success. TTLs: quote/valuation = 1 hour, financials/shareholding = 24 hours. Prevents redundant fetches in batch sector scans.

### SQLite Datastore (`src/db/`)

Every completed analysis is persisted to `investor.db` (path configured in `src/config.py` as `db_path`). Writes are always wrapped in `try/except` — DB failures never break the pipeline output.

**Schema** (`src/db/schema.py`): two tables:
- `analyses` — one row per `(ticker, analysis_date)` with all step scores, gates, recommendation, conviction, MoS%, DCF intrinsic, sector, flags. `UNIQUE(ticker, analysis_date)` + `INSERT OR REPLACE` means re-running an analysis on the same day overwrites rather than duplicates.
- `data_snapshots` — one row per `(ticker, snapshot_date, data_type)` storing the raw API payload as JSON. `data_type` is one of `quote | financials | governance | valuation`. The `source` column records the actual provider used (e.g. `yfinance` when NSE fallback fired, not just `nse`).

**Repository** (`src/db/repository.py`): all async functions using `aiosqlite`:
- `init_db(db_path)` — creates tables/indexes idempotently
- `save_analysis(db_path, state)` — upserts full pipeline result
- `save_snapshot(db_path, ticker, date, type, data, source)` — upserts raw data
- `get_latest_analysis(db_path, ticker)` — most recent row for ticker
- `get_analysis_history(db_path, ticker, limit)` — trend over time
- `list_recommendations(db_path, rec)` — all BUY / WATCHLIST / etc. across all dates
- `get_summary(db_path)` — all tickers ranked BUY → WATCHLIST → REJECT

**Pipeline integration** (`src/agent/pipeline.py`):
- After `_prefetch_data`: saves 4 raw snapshots (`quote`, `financials`, `governance`, `valuation`) with actual source names
- After `pipeline_complete` log: calls `save_analysis()`

**CLI commands** (`src/main.py`):
- `investor db-summary` — ranked table of every analysed ticker
- `investor db-history TICKER [--limit N]` — per-ticker trend (governance, financials, MoS% over time)
- `investor db-recommendations --type BUY|WATCHLIST|PEER_SWITCH|REJECT` — filter by outcome
- `investor db-snapshots TICKER [--type quote|financials|governance|valuation]` — inspect raw stored data

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

**Watchlist tiers**: Tier 1 = all steps passed + valuation in buy zone (max 15). Tier 2 = Steps 1–5 passed, valuation not attractive (max 30). Tier 3 = Steps 1–3 passed, research pending.

**Tranche plan** (always in BUY output): T1 40% @ CMP, T2 35% @ CMP×0.92, T3 25% @ CMP×0.85.

**Tax**: LTCG 12.5% on gains > ₹1.25L after 1 year; STCG 20% under 1 year. After any trade, update all four files in `portfolio/`.

---

## Reference Files (`skill/references/`)

Read situationally:

| File | When |
|------|------|
| `data-sources.md` | Start of every analysis — exact source URLs per data type |
| `sector-benchmarks.md` | Step 3 — sector-specific financial thresholds |
| `output-templates.md` | Step 9 — exact structured output formats |
| `edge-cases.md` | When any edge-case flag fires (pre-profit, cyclical, conglomerates) |
| `error-recovery.md` | When a data fetch fails or sources conflict |
