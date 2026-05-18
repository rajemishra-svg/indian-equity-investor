# CHANGELOG

## [2026-05-17] — Sector-Aware Scoring & SQLite Datastore

### New Features

**Sector-Aware Scoring (`src/sector/`)**
- `src/sector/profiles.py`: `SectorProfile` dataclass with per-sector threshold overrides for every financial metric. `None` = waive the check entirely. Seven profiles: `default`, `financial_services`, `defence_govt`, `infrastructure_utility`, `capital_goods`, `commodities_cyclical`, `recently_listed`.
- `src/sector/classifier.py`: keyword-set-based sector detection from company name + optional moat narrative enrichment. Priority order: financial > defence > infra > capital_goods > commodities > recently_listed > default.
- `src/models.py`: `sector_name: Optional[str]` added to `AnalysisState`.
- `src/agent/pipeline.py`: `classify_sector()` called after `_prefetch_data()` completes; result stored in `state.sector_name` before Step 0 runs.
- `src/agent/steps/step0_prescreen.py`: all 9 pre-screen metrics use `profile.*` thresholds; re-classifies sector if not set (standalone use).
- `src/agent/steps/step1_governance.py`: `profile.capital_allocation_note` injected into the capital allocation Claude prompt so the model knows it's evaluating a bank vs. an industrial company.
- `src/agent/steps/step3_financials.py`: all 7 hurdles and 3 hard triggers use `profile.*` thresholds; `None` = waived. Removed previous hardcoded `_FINANCIAL_SECTOR_NAME_KEYWORDS` and `_is_financial_sector()`.
- `src/agent/steps/step5_valuation.py`: `wacc += profile.wacc_adjustment`; EV/EBITDA method skipped when `not profile.ev_ebitda_applicable`.
- All sector overrides logged as `[SECTOR OVERRIDE: ...]` in `state.all_data_flags`.

**Key false-negative fixes from sector profiles:**
- Defence PSUs (BEL, GRSE, HBLENGINE, ZENTEC): CFO/NP threshold relaxed to 40% (hurdle) / 25% (hard trigger) — milestone billing compresses ratio structurally.
- Financial services (BAJFINANCE, CANHLIFE, CRAMC, ICICIBANK, PNBHOUSING, TATACAP): D/E, ICR, CFO/NP entirely waived; EV/EBITDA skipped.
- Recently listed (LGEINDIA — IPO 2025): 5Y metrics waived when `listing_years < 3.0`.
- Infrastructure (TATAPOWER, ADANIPORTS): D/E hurdle raised to 3.0×.
- Commodities (ONGC, OIL): correctly classified as cyclical (not infra); WACC +1%.

**Classifier bug fixes:**
- `"gas"` keyword removed from infra set (matched ONGC as infra); replaced with `"city gas"`, `"gas distribution"` etc.
- `"tata capital"` added to financial keywords.
- ONGC/OIL: `"oil & natural gas"`, `"oil india"` added to commodity keywords.

**SQLite Datastore (`src/db/`)**
- `src/db/schema.py`: `analyses` table (one row per ticker per day, `UNIQUE(ticker, analysis_date)`) and `data_snapshots` table (one row per ticker/date/type). Four indexes for fast lookup by ticker, date, and recommendation.
- `src/db/repository.py`: async functions — `init_db`, `save_analysis`, `save_snapshot`, `get_latest_analysis`, `get_analysis_history`, `list_recommendations`, `get_summary`. All use `INSERT OR REPLACE` for idempotent upserts. JSON fields (`governance_sub_scores`, `all_data_flags`, etc.) serialised on write and deserialised on read.
- `src/db/__init__.py`: re-exports `AnalysisRepository` (thin OO wrapper) and `init_db`.
- `pyproject.toml`: `aiosqlite>=0.20.0` added to dependencies.
- `src/config.py`: `db_path: str = "investor.db"` setting.
- `src/agent/pipeline.py`: after `_prefetch_data` saves 4 raw snapshots with actual source names (tracks yfinance fallback vs. nse/trendlyne); after `pipeline_complete` saves full analysis result — both wrapped in `try/except` (DB failures never terminate the pipeline).
- `src/main.py`: four new CLI commands — `db-summary`, `db-history`, `db-recommendations`, `db-snapshots`.
- `portfolio-review` output now includes: `Results saved to SQLite: N analyses · investor.db`.

### Tests Added

- `tests/test_sector/test_classifier.py` — 24 tests: all 7 sectors, moat narrative enrichment, edge cases (empty name, exactly 3 years listed, name-priority over narrative)
- `tests/test_sector/test_sector_step0.py` — 7 tests: financial D/E waived, defence CFO/NP threshold at 40% boundary (pass/fail), recently_listed 5Y waived, infra D/E relaxed, sector flag logged
- `tests/test_sector/test_sector_step3.py` — 7 tests: financial hard triggers waived, defence 25% hard trigger boundary, infra 5× D/E boundary, default 50% trigger, recently_listed 5Y hurdles waived
- `tests/test_db/test_repository.py` — 16 tests: `init_db` idempotent, save/retrieve round-trip (minimal + full state), upsert on same day, unknown ticker, case-insensitive lookup, REJECT state, JSON deserialisation, history limit, `list_recommendations` filter, `get_summary` ordering, snapshot save/upsert/multiple types

### Documentation Updated

- `CLAUDE.md`: replaced old "Financial sector overrides" section with full Sector-Aware Scoring section; added SQLite Datastore section; updated data-flow diagram; added `db-*` CLI commands
- `README.md`: added `src/sector/` and `src/db/` to project structure; updated Quick Start; added Sector-Aware Scoring and SQLite Persistence sections
- `skill/references/sector-benchmarks.md`: added note linking manual benchmarks to `src/sector/profiles.py` code
- `framework/CHANGELOG.md`: this entry

## [2026-05-16] — Governance Data Pipeline & Agentic Loop Robustness

### New Features

**NSE shareholding API as primary governance source (nse.py)**
- `NSEClient.get_shareholding(symbol)` added — calls `/api/corporate-shareholding-pattern?symbol=X` using existing session cookies (no extra session round-trip)
- `_parse_shareholding()` handles 3 NSE JSON response shapes: `shareholdingPatterns`, `data`/`shareHoldingList`/`results` dict keys, and flat list
- Returns `GovernanceData` with `promoter_holding_pct`, `promoter_pledging_pct`, `promoter_pledging_trend` (last 8 quarters), `pledging_trend_direction`

**Trendlyne governance page scraper (trendlyne.py)**
- `TrendlyneClient.get_governance_data(ticker)` added — fetches `/fundamentals/governance/TICKER/`
- `_parse_governance()` extracts auditor name (3 regex patterns + structured HTML sibling scan), promoter holding %, pledging % from full-page text
- Used as enrichment source when auditor/pledging fields are missing after primary shareholding fetch

**4-layer shareholding fallback chain (pipeline.py)**
- Shareholding fetch order: NSE (concurrent with other prefetch) → BSE (sequential fallback) → Screener.in (sequential fallback) → governance_data = empty GovernanceData with error flags
- `_enrich_governance_from_trendlyne()` method added: merges Trendlyne auditor/pledging into existing GovernanceData when fields are missing; skips if both auditor_name and promoter_holding_pct already populated
- Resolved flags are cleaned from `data_flags` after successful merge

**Step 1 governance mini-research loop (step1_governance.py)**
- `_enrich_governance_data(state)` added: runs Haiku agentic loop (≤4 iterations, web_search + web_fetch) to research missing auditor name, RPT % of revenue, SEBI orders before scoring
- Skips entirely if all three fields are already populated (zero cost path)
- Results merged into existing GovernanceData; only fills fields that are currently None
- Prevents the systematic 8/15 default score caused by missing auditor/RPT sub-scores

### Bug Fixes

**Agentic loop iteration cap leaves no text response (base.py)**
- Root cause: when `_agentic_loop` hit `max_iterations` while Claude still returned `stop_reason = "tool_use"`, the loop exited with the last `response.content` containing only tool_use blocks — no text. `_parse_json_response("")` raised `ValueError`, the step caught it and used defaults (e.g. auditor score 1/3, RPT score 1/3), producing a systematic false 8/15 governance score → REJECT.
- Fix: after executing tool results on the final iteration, make one additional Claude call **without tools** and with a synthesis instruction ("respond NOW with only the required JSON object"). This call always returns `end_turn` with a text block.
- Affects all agentic steps: governance enrichment (Step 1, cap=4), moat (Step 2, cap=6), peer benchmarking (Step 7, cap=8).

### Tests Updated

- `tests/test_agent/test_pipeline.py`: added `mock_nse.get_shareholding = AsyncMock(return_value=SAMPLE_GOVERNANCE)` and `mock_trendlyne.get_governance_data = AsyncMock(return_value=None)` to `mock_pipeline_env` fixture
- `test_step1_governance_fail_terminates_with_rejection`: updated to override `env["nse"].get_shareholding` (now primary source) instead of `env["bse"].get_shareholding`

## [2026-05-16] — Codebase Hardening & Domain Improvements

### New Features

**Dynamic index constituent fetching (batch_scanner.py)**
- NSE archives CSV endpoint added as second fallback layer between NSE JSON API and hardcoded list
- Supports Nifty 50, 100, 200, 500, Next 50, Midcap 100, Smallcap 100 via `_NSE_ARCHIVES_CSV` mapping
- `_fetch_constituents_from_archives()` uses httpx to fetch CSV without browser session cookies
- Fallback chain: NSE JSON API → NSE Archives CSV → `NIFTY50_FALLBACK` (last resort)

**EV/EBITDA as 5th valuation method (step5_valuation.py)**
- Added `_EV_EBITDA_BANDS` and `_ev_ebitda_verdict()`: EXCELLENT (<12x), FAIR (12–20x), EXPENSIVE (20–28x), AVOID (>28x)
- Gate now requires ≥ 2 of **5** methods in buy zone (was 4)
- `ValuationResult` model extended with `max_methods: int` and `ev_ebitda_verdict: Optional[str]` fields

**Financial sector overrides (step0_prescreen.py + step3_financials.py)**
- Banks, NBFCs, insurance companies automatically bypass D/E hurdle (Step 0) and D/E + ICR hurdles (Step 3)
- Step 0 detects financial sector by company name keywords (runs before moat step)
- Step 3 additionally checks moat narrative for financial sector keywords
- Hard triggers for D/E > 3.0 and ICR < 3x also waived for financial sector in Step 3
- All bypasses logged as `[SECTOR OVERRIDE: ...]` flags

**5th technical signal (step6_technical.py)**
- Added Signal 5: price ≥ 20% below 52-week high (meaningful pullback from peak)
- Entry guidance updated: GREEN ≥ 3/5 signals, AMBER ≥ 1/5, RED = 0/5

**Moat quality in conviction scoring (step9_output.py)**
- `_set_conviction()` now weights moat durability: High = 1.0, Medium = 0.70, Low = 0.35
- Companies with moat_type = "none" capped at conviction score ≤ 0.40
- Late-cycle tailwind position applies -0.03 penalty to tech bonus

**Cap-size-adjusted stop-loss (step9_output.py)**
- `_build_exit_strategy()` uses cap-size-specific multipliers: Large Cap ×0.82, Mid Cap ×0.75, Small Cap ×0.70
- Replaces previous flat 25% stop-loss for all companies

### Improvements

**Governance checks (step1_governance.py)**
- Expanded `reputed_auditors` set: added Walker Chandiok, Grant Thornton Bharat, BDO/MSKA, Haribhakti, Sharp & Tannan, Nanubhai, Lodha, Chaturvedi & Shah
- Added pledging trend concern: increasing pledging for 3+ consecutive quarters flagged even if below 10%
- Added contingent liabilities check: >100% of Net Worth → HIGH RISK flag; 50–100% → concern
- Fixed `_score_regulatory()`: `sebi_record_clean=False` with no orders now returns 1/3 with `[DATA UNVERIFIED]` (was incorrectly returning 2/3)

**Financial soft quality checks (step3_financials.py)**
- `_soft_quality_checks()` added as non-scoring quality checks:
  - Revenue deceleration: 5Y vs 3Y CAGR gap > 8pp → concern
  - PAT deceleration: 5Y vs 3Y gap > 10pp → concern
  - EBITDA margin < 8% → flag + concern; 8–10% → watch flag
  - ICR = None with D/E > 0.1 → `[DATA UNVERIFIED: interest_coverage]` flag

**Risk-adjusted WACC in DCF (step5_valuation.py)**
- Cap-size WACC: 13% large-cap, 15% mid-cap, 16.5% small-cap (was flat 12–14%)
- +1% cyclical sector premium (commodity, infrastructure, real estate, metal, steel, cement)
- Terminal growth fixed at 6% (was configurable; now conservative India sustainable rate)

**Margin of Safety thresholds (models.py)**
- MAXIMUM_OPPORTUNITY mode (Nifty >15% below peak) reduces MoS by 10pp (was 5pp — same as Correction mode)
- Correction mode still reduces by 5pp

**Step 0 data availability checks (step0_prescreen.py)**
- `_data_available()` now checks specific fields (e.g., `f.revenue_cagr_5y is not None`) instead of just `f is not None`, preventing false "data available" assertions

**Step 9 output improvements (step9_output.py)**
- BUY output: added EV/EBITDA line in valuation section, methods count `/5`, peer comparison section
- WATCHLIST output: now shows target buy price, current price, moat type, specific alert trigger per step
- `_re_eval_condition()` provides step-specific re-evaluation conditions instead of generic text

### Tests Added

- `test_batch_scanner.py`: fallback to archives CSV, fallback to hardcoded when both fail
- `test_step1.py`: pledging trend increasing, high contingent liabilities, SEBI not-clean no orders, reputed Indian auditor scores 3
- `test_step3.py`: financial sector D/E bypass, non-financial D/E still applies, revenue deceleration, thin EBITDA margin, ICR None with debt

### Documentation Updated

- `CLAUDE.md`: added financial sector overrides section, valuation methods table with WACC
- `skill/SKILL.md`: Step 0–6 specifications updated; inline templates updated
- `skill/references/output-templates.md`: BUY_RECOMMENDATION (5 valuation methods, stop-loss table), WATCHLIST_ADDITION (moat field, re-evaluate condition)
- `framework/CHANGELOG.md`: this file
