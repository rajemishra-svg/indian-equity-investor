---
name: indian-equity-long-term-investor
description: >
  Agentic stock analysis skill for long-term value investing in the Indian equity market.
  Trigger this skill whenever the user asks to analyse a stock, screen for investment ideas,
  review their portfolio, check if a stock is worth buying, evaluate a sector, or detect
  market correction opportunities in Indian markets (NSE/BSE). Also trigger when the user
  asks about Nifty levels, index corrections, FII/DII flows, or sector tailwinds in the
  Indian context. Use this skill even for loosely phrased requests like "is X a good stock",
  "should I buy Y now", "what's the best defence stock to buy", or "is the market expensive".
  This skill implements the full 9-step investment framework from the companion reference:
  long_term_investing_context_v2.md.
compatibility:
  requires:
    - web_search (real-time price, filings, news)
    - web_fetch (NSE/BSE filings, Screener.in, Trendlyne)
  optional:
    - file_read (uploaded financials or annual reports)
  reference_files:
    - references/data-sources.md       # Where to fetch each data type
    - references/sector-benchmarks.md  # Sector-specific thresholds
    - references/output-templates.md   # All structured output formats
    - references/edge-cases.md         # Edge case taxonomy and handling rules
    - references/error-recovery.md     # Error recovery playbook
---

# Indian Equity Long-Term Investor — Skill Architecture

## 1. Skill Overview

This skill implements a disciplined, 9-step agentic research workflow for identifying
high-quality Indian equity investments that can compound at 15%+ CAGR over 5+ years.

**Operating principle**: Fail fast on hard gates. Never rationalise past a red flag.
**Data principle**: No hallucinated numbers. Label every estimate as [ESTIMATE]. Label
every unverified or unavailable figure as [DATA UNVERIFIED] or [NOT AVAILABLE].

---

## 2. Mode Detection (Always Run First)

Before any analysis, detect the operating mode. This determines urgency, capital
deployment posture, and which steps to prioritise.

```
MODE DETECTION PROCEDURE
─────────────────────────────────────────────────────────────────────
1. Fetch Nifty 50 current level and 52-week high.
2. Compute decline from peak: decline% = (peak - current) / peak * 100

   decline% < 5%      → MODE A: Normal Mode
   5% ≤ decline% < 8% → MODE A: Normal Mode (watchlist refresh alert)
   8% ≤ decline% < 12%→ MODE B: Correction Mode (Priority 2)
   12% ≤ decline% < 15%→ MODE B: Correction Mode (Priority 1)
   decline% ≥ 15%     → MODE B: Correction Mode (MAXIMUM PRIORITY)

3. If MODE B: notify user immediately before beginning stock analysis.
   Use this exact notification:

   ⚠️ CORRECTION ALERT
   Nifty 50 is [X]% below its 52-week peak of [Y].
   Correction Mode activated. Opportunistic buying criteria apply.
   Fast-recovering sectors: Private Banking → Defence/Cap Goods → Pharma → IT.
   Deploying max 20–25% of available cash per event. Staggered tranches recommended.

4. Store mode in working context. All subsequent steps reference it.
─────────────────────────────────────────────────────────────────────
```

**Edge case — Mode detection data unavailable**: If Nifty data cannot be fetched,
default to MODE A and append `[MODE UNCONFIRMED — NIFTY DATA UNAVAILABLE]` to output.

---

## 3. Step Execution Pipeline

Steps run sequentially. Each step has:
- **Input**: What is required to begin
- **Process**: What the agent must do
- **Gate**: Pass/Conditional/Fail decision
- **Output**: What must be produced before the next step begins
- **On Fail**: What to do when the gate is not passed

```
PIPELINE OVERVIEW
─────────────────────────────────────────────────────────────────────
STEP 0 → Quantitative Pre-Screen
STEP 1 → Governance & Management Gate       [HARD GATE — fail = reject]
STEP 2 → Business Quality & Moat
STEP 3 → Financial Strength & Consistency   [HARD GATE — fail = reject]
STEP 4 → Industry & Structural Tailwinds
STEP 5 → Valuation & Margin of Safety
STEP 6 → Technical Entry Confirmation
STEP 7 → Peer Benchmarking
STEP 8 → Premortem Risk Analysis
STEP 9 → Final Output Generation
─────────────────────────────────────────────────────────────────────
```

### STEP 0 — Quantitative Pre-Screen

**Purpose**: Eliminate clearly unsuitable stocks before expensive qualitative research.

**Input required**: Stock ticker or name.

**Data to fetch** (see references/data-sources.md for source URLs):
- Market Cap (₹ Cr)
- Revenue CAGR 5Y and PAT CAGR 5Y
- ROE 5Y average and ROCE 5Y average
- Debt-to-Equity (latest)
- CFO / Net Profit 3Y average
- Promoter Holding % (latest quarter)
- Promoter Pledging % (latest quarter)

**Scoring logic**:
```
Metric                    Threshold    Points if Met
─────────────────────────────────────────────────────
Market Cap ≥ ₹2,000 Cr              1
Revenue CAGR ≥ 12% (5Y)             1
PAT CAGR ≥ 15% (5Y)                 1
ROE ≥ 15% (5Y avg)                  1
ROCE ≥ 18% (5Y avg)                 1
Debt/Equity < 1.0                   1
CFO/Net Profit ≥ 70% (3Y avg)       1
Promoter Holding ≥ 40%              1
Promoter Pledging < 10%             1
─────────────────────────────────────────────────────
TOTAL                               /9
```

**Financial sector override**: For banks, NBFCs, insurance, and other financial services companies (detected by company name keywords: bank, banking, finance, financial, insurance, nbfc, finserv, fincorp), the Debt/Equity < 1.0 hurdle is **automatically waived** — leverage is the business model. All other hurdles apply normally. Log as `[SECTOR OVERRIDE: D/E waived — financial services sector]`.

**Gate**:
- Score ≥ 7 → PASS → proceed to Step 1
- Score 5–6 → CONDITIONAL → proceed only if sector context justifies; document exceptions
- Score < 5 → FAIL → reject; log: `[STEP 0 FAIL: Score X/9 — reasons listed]`

**On Fail**: Output the rejection log (see Section 6.1). Do not proceed.

**On Conditional**: Add `[CONDITIONAL PASS — exceptions: list metrics that failed]`
to all subsequent outputs. Apply extra scrutiny in Steps 1 and 3.

---

### STEP 1 — Governance & Management Gate (HARD GATE)

**Input required**: Step 0 PASS or CONDITIONAL.

**Data to fetch**:
- Promoter pledging trend (last 8 quarters) — sourced from NSE shareholding API first, then BSE, then Screener.in as fallback
- Auditor name, any changes in last 3 years, audit qualifications — sourced from Trendlyne governance page if available, otherwise via web_search on NSE/BSE filings
- RPT value as % of revenue (Notes to Accounts, latest annual report) — via web_search on annual report if not in shareholding data
- Contingent liabilities as % of Net Worth
- SEBI orders / ED notices (SEBI SCORES portal)
- Capital allocation history: ROIC on incremental capital, acquisition track record

**Governance data enrichment**: Before scoring, Step 1 runs a focused mini agentic loop (Haiku, ≤ 4 iterations, web_search + web_fetch) to fill any missing fields — auditor name, RPT %, SEBI orders. The loop only fires if any of these three fields are absent from the prefetched shareholding data. Search priority: NSE/BSE filings pages → Annual report Notes to Accounts → SEBI SCORES portal. Output is merged into the GovernanceData object before scoring begins.

**Scoring logic**:
```
Sub-Factor                              Score (0–3)
────────────────────────────────────────────────────────────────
Pledging: 0% = 3, 1–4% = 2, 5–10% = 1, >10% = IMMEDIATE FAIL
Audit quality: Big4/reputed = 3, others = 1, unknown = 0
RPT discipline: <8% = 3, 8–15% = 2, 15–20% = 1, >20% = 0
Capital allocation track record: excellent = 3, adequate = 2,
  poor/acquisitive without discipline = 1, destructive = 0
Clean regulatory history: clean = 3, minor = 2, SEBI/ED = 0
────────────────────────────────────────────────────────────────
TOTAL                                   /15
```

**Reputed auditors** (score 3): Big 4 (Deloitte, EY, KPMG, PwC) plus Walker Chandiok & Co, Grant Thornton Bharat, BDO/MSKA & Associates, Haribhakti & Co, Sharp & Tannan, Nanubhai & Co, Lodha & Co, Chaturvedi & Shah.

**Additional soft checks** (do not affect score — add to concerns list for analyst review):
- Pledging trend: if promoter pledging is increasing quarter-over-quarter for 3+ consecutive quarters, flag as `[PLEDGING_TREND: INCREASING]` even if below 10% threshold.
- Contingent liabilities: if > 100% of Net Worth → flag `[HIGH RISK: contingent liabilities X% of Net Worth]`; if 50–100% → add to concerns.
- SEBI record: if `sebi_record_clean = False` but no specific orders listed → treat conservatively as 1/3 on regulatory sub-factor with `[DATA UNVERIFIED: sebi_record — manual verification required]` until confirmed.

**Immediate rejection triggers** (any single trigger = FAIL regardless of total score):
- Promoter pledging > 10%
- Active SEBI/ED financial fraud investigation
- RPT > 20% of revenue (unexplained)
- Auditor resignation mid-year (without acceptable reason)
- Audit qualification on going concern or revenue recognition

**Gate**:
- Score ≥ 12 AND no immediate triggers → PASS GREEN
- Score 9–11 AND no immediate triggers → PASS YELLOW (document concerns; continue with heightened scrutiny)
- Score < 9 OR any immediate trigger → FAIL → reject

**On Fail**: Output rejection log. Do not proceed. Mark stock as [GOVERNANCE REJECT] in tracker.

---

### STEP 2 — Business Quality & Moat Assessment

**Input required**: Step 1 PASS (Green or Yellow).

**Process**:
1. Identify the **primary moat type** from: Brand / Network Effect / Cost Leadership /
   Switching Costs / Regulatory Moat / Scale Advantages / IP-Patents.
   If no moat is identifiable → flag as `[MOAT UNIDENTIFIED]` and note in risk section.
2. Assess moat **durability**: High / Medium / Low.
3. Verify market position: Rank 1–2 in primary market, OR credible path to top-3 in 5Y.
4. Check market share trend over 5 years: growing / stable / declining.
5. Assess TAM: must be ≥ 3× current revenue.
6. Check working capital: flag if DSO or Inventory Days rose > 20% YoY for 2+ consecutive years.

**Output required before Step 3**:
```
MOAT_TYPE: [type]
MOAT_DURABILITY: [High / Medium / Low]
MARKET_POSITION: [Rank X in Y market / Emerging — credible path]
MARKET_SHARE_TREND: [Growing / Stable / Declining]
TAM_MULTIPLE: [Xx current revenue]
WORKING_CAPITAL_FLAG: [Clean / FLAG: DSO +X% YoY for N years]
MOAT_NARRATIVE: [2–3 line explanation of competitive advantage]
```

**Edge case — Conglomerate or multi-segment business**:
Assess moat per **primary revenue-generating segment** (> 50% of revenue).
Note other segments separately. Use the weakest moat assessment as the governing score.

---

### STEP 3 — Financial Strength & Consistency (HARD GATE)

**Input required**: Step 2 complete.

**Data to fetch** (5-year and 3-year periods):
- Revenue, PAT, EBITDA absolute values and CAGRs
- ROE, ROCE, ROIC for each of last 5 years
- CFO, Net Profit, EBITDA for last 3 years (CFO/NP ratio)
- Debt/Equity, Interest Coverage, Current Ratio, Net Debt/EBITDA (latest)
- EBITDA Margin trend (5 years)
- Receivables growth vs. Revenue growth
- Other Income as % of PBT (3Y avg)

**Hard rejection triggers** (any single trigger = FAIL):
- CFO / Net Profit < 50% for 2+ consecutive years
- Debt/Equity > 3.0 (non-infrastructure)
- Interest Coverage < 3x
- ROE declining > 5 percentage points per year for 3+ consecutive years
- Receivables growing > 2× the rate of revenue for 2+ consecutive years
- Other Income > 40% of PBT consistently (masks core business weakness)

**Minimum hurdles** (≥ 5 of 7 must be met, or CONDITIONAL with documented exceptions):
```
Metric                           Minimum     Preferred
────────────────────────────────────────────────────────
Revenue CAGR 5Y                  ≥ 12%       ≥ 18%
PAT CAGR 5Y                      ≥ 15%       ≥ 20%
ROE 5Y average                   ≥ 15%       ≥ 20%
ROCE 5Y average                  ≥ 18%       ≥ 22%
CFO / Net Profit 3Y avg          ≥ 80%       ≥ 90%
Debt / Equity                    < 1.0       < 0.5
Interest Coverage                > 6x        > 10x
────────────────────────────────────────────────────────
```

**Financial sector override**: For financial services companies (banks, NBFCs, insurance — detected by company name or moat narrative keywords), the following are **automatically waived**:
- Debt/Equity < 1.0 hurdle and D/E > 3.0 hard trigger
- Interest Coverage > 6x hurdle and ICR < 3x hard trigger
Log as `[SECTOR OVERRIDE: D/E and ICR waived — financial services sector uses leverage as business model]`.

**Soft quality checks** (non-scoring — add to concerns for analyst review):
- Revenue deceleration: if 5Y CAGR vs 3Y CAGR gap > 8pp → flag as potential slowdown; investigate cause.
- PAT deceleration: if 5Y vs 3Y gap > 10pp → flag margin compression or base effect.
- EBITDA margin: < 8% → `[WATCH: very thin margin — sector benchmark check required]`; 8–10% → watch flag.
- ICR data gap: if interest_coverage = None but D/E > 0.1 → `[DATA UNVERIFIED: interest_coverage — company has debt but ICR not available; treat as passing but verify manually]`.

**Sector adjustment**: Before applying thresholds, read `references/sector-benchmarks.md`
and apply sector-specific overrides. Log any override explicitly with `[SECTOR OVERRIDE: reason]`.

**Gate**:
- 7/7 met AND no hard triggers → PASS GREEN
- 5–6/7 met AND no hard triggers → PASS CONDITIONAL
- < 5/7 met OR any hard trigger → FAIL → reject

---

### STEP 4 — Industry & Structural Tailwinds

**Input required**: Step 3 PASS.

**Process**:
1. Identify the **primary sector** from the priority list in long_term_investing_context_v2.md.
2. Classify the tailwind as:
   - **Structural** (multi-decade: demographics, regulation, technology shift) → strongest backing
   - **Policy-driven** (government programme, PLI scheme) → strong, but monitor political risk
   - **Cyclical** (commodity upcycle, rate cycle) → weakest; apply extra valuation caution
3. Assess if the company is positioned **early, mid, or late** in the tailwind cycle.
   Late-cycle positioning requires a valuation discount of ≥ 15% beyond normal MoS.
4. Check for sector headwinds: regulation changes, technology disruption, Chinese competition.
5. Estimate how many years of visible growth runway remain.

**Output required before Step 5**:
```
SECTOR: [name]
TAILWIND_TYPE: [Structural / Policy-driven / Cyclical]
CYCLE_POSITION: [Early / Mid / Late]
GROWTH_RUNWAY_YEARS: [X–Y years]
HEADWIND_FLAGS: [list or NONE]
TAILWIND_NARRATIVE: [2–3 lines]
```

---

### STEP 5 — Valuation & Margin of Safety

**Input required**: Steps 1–4 complete.

**Data to fetch**:
- Current P/E, EV/EBITDA, P/BV
- 10-year historical P/E and EV/EBITDA range (percentile position)
- Forward EPS consensus estimates (1Y and 2Y)
- Free Cash Flow (latest year and 3Y average)
- Net Debt (latest)
- Shares outstanding

**Valuation methods** — use minimum two, triangulate:

```
METHOD 1: HISTORICAL PERCENTILE
  - Fetch 10Y P/E range for the stock.
  - Compute current percentile: (current_PE - min_10Y) / (max_10Y - min_10Y) × 100
  - Compare to thresholds:
    < 30th percentile  → EXCELLENT (Buy Zone)
    30th–60th          → FAIR VALUE
    60th–80th          → EXPENSIVE (Caution)
    > 80th             → AVOID

METHOD 2: PEG RATIO
  - PEG = Current P/E / Forward 2Y EPS CAGR (consensus)
  - < 1.0  → Excellent
  - 1.0–1.3 → Fair
  - 1.3–1.7 → Expensive
  - > 1.7  → Avoid

METHOD 3: DCF (3 Scenarios)
  - Base (50% weight): Revenue growth = 5Y historical CAGR × 0.85
  - Bull (25% weight): Revenue growth = 5Y historical CAGR × 1.1
  - Bear (25% weight): Revenue growth = 5Y historical CAGR × 0.6
  - WACC (risk-adjusted by cap size and sector):
      Large Cap  : 13%  |  Mid Cap: 15%  |  Small Cap: 16.5%
      +1% premium for cyclical sectors (commodity, infra, real estate, metal, steel, cement)
  - Terminal growth: 6% (conservative India long-run sustainable rate; never use > 8%)
  - Intrinsic value = weighted average of 3 scenarios
  - Margin of Safety = (Intrinsic Value - CMP) / Intrinsic Value × 100

METHOD 4: FCF YIELD (validation check)
  - FCF Yield = FCF per share / CMP × 100
  - > 5%  → Attractive
  - 3–5%  → Fair
  - < 3%  → Expensive or low FCF business (flag)

METHOD 5: EV/EBITDA (sector-agnostic cross-check; skip for financial services)
  - < 12x   → EXCELLENT (buy zone)
  - 12–20x  → FAIR (acceptable)
  - 20–28x  → EXPENSIVE (caution)
  - > 28x   → AVOID
```

**Margin of Safety required by cap size**:
```
Cap Size               Normal Mode    Correction Mode    Maximum Opportunity (>15%)
Large Cap (Nifty 100)     25%              20%                  15%
Mid Cap                   35%              30%                  25%
Small Cap                 45%              40%                  35%
```

**Gate**:
- ≥ 2 of 5 methods in BUY ZONE AND MoS threshold met → PASS GREEN
- 1 method in BUY ZONE → CONDITIONAL (flag; consider Tranche 1 only)
- No method in BUY ZONE or MoS not met → DO NOT BUY NOW; add to Tier 2 watchlist

**On "DO NOT BUY NOW"**: Output a Watchlist Addition record (see output templates).
Do not proceed to Steps 6–8. Log as `[VALUATION HOLD — TIER 2]`.

---

### STEP 6 — Technical Entry Confirmation

**Purpose**: Improve entry price within a fundamentally justified buy zone.
**Note**: Technical signals do NOT veto a fundamentally sound stock. They only calibrate timing.

**Data to fetch**:
- Current price vs. 52-week low (proximity %)
- RSI 14-day (current)
- Current price vs. 200-day moving average (above/below; % distance)
- Volume trend on down-days (last 10 trading sessions: declining or sustained)

**Signal scoring**:
```
Signal                                          Met?   Points
──────────────────────────────────────────────────────────────
Within 15% of 52-week low                        Y/N      1
RSI (14-day) < 40                                Y/N      1
Price ≤ 200-day moving average                   Y/N      1
Volume declining on recent down-days             Y/N      1
Price ≥ 20% below 52-week high (meaningful dip)  Y/N      1
──────────────────────────────────────────────────────────────
TOTAL                                                    /5
```

**Entry guidance**:
- ≥ 3 of 5 signals met → GREEN LIGHT: begin Tranche 1 entry
- 1–2 signals met → AMBER: enter Tranche 1 with reduced size (50% of planned Tranche 1)
- 0 signals met → RED: defer to next quarterly result; set price alert at 200-DMA

**Tranche plan** (to be included in final output):
```
Tranche 1 (40% of target allocation): CMP ± 2% — triggered now
Tranche 2 (35% of target allocation): [CMP × 0.92] or next earnings confirmation
Tranche 3 (25% of target allocation): [CMP × 0.85] or 2nd quarter execution confirmation
```

---

### STEP 7 — Peer Benchmarking

**Input required**: Steps 1–6 complete.

**Process**:
1. Identify 3–5 direct peers (same sub-sector, similar business model).
2. Fetch the following for each peer: Revenue CAGR 5Y, PAT CAGR 5Y, EBITDA Margin,
   ROE 5Y avg, ROCE 5Y avg, D/E, Forward P/E, Forward EV/EBITDA, Promoter Holding,
   Pledging %.
3. Rank the target company and each peer on:
   - **Quality Score** (Revenue CAGR + PAT CAGR + ROE + ROCE — normalised 0–10)
   - **Valuation Score** (P/E + EV/EBITDA — lower = better, normalised 0–10)
4. Target must be in **top quartile** on Quality AND **bottom half** on Valuation
   vs. peers to proceed.

**Decision rule**:
```
If a peer scores HIGHER on Quality AND LOWER on Valuation than the target:
  → Flag: [PEER DOMINANCE DETECTED]
  → Switch recommendation to the peer stock
  → Restart from Step 0 with the peer ticker

If target is top-quartile quality AND bottom-half valuation:
  → PASS — confirmed relative attractiveness

If target is top-quartile quality but top-half valuation:
  → CONDITIONAL — note premium; verify moat justifies it
```

---

### STEP 8 — Premortem Risk Analysis

**Input required**: Steps 1–7 complete.

**Process**:
Explicitly answer: *"If this stock falls 50% in 2–3 years, what is the most likely cause?"*

Evaluate each risk category:
```
Risk Category           Evidence to Check              Flag if:
──────────────────────────────────────────────────────────────────────
Governance / Fraud      Pledging trend, audit, RPT     Any yellow flag from Step 1
Competitive Disruption  Moat assessment from Step 2    Moat durability = Low
Regulatory Change       Policy dependency              > 30% revenue from single policy
Industry Cyclicality    Cycle position from Step 4     Late-cycle AND cyclical tailwind
Capital Misallocation   Step 1 capital allocation score < 2/3 on that sub-factor
Leverage Risk           D/E and ICR from Step 3        D/E > 1.0 or ICR < 6x
Execution Failure       Order book, margin trend       Margins compressing + no explanation
Macro Sensitivity       INR, crude, global recession   High import dependency or USD revenues
```

**Decision rule**:
```
Primary identified risk is STRUCTURAL AND UNHEDGEABLE → DO NOT RECOMMEND
  (Examples: moat disappearing, governance questionable, business model disrupted)

Primary risks are CYCLICAL / TEMPORARY / MANAGEABLE → PROCEED
  → Size position conservatively if 2+ risks are present
  → Document all risks clearly in output
```

---

## 4. Output Templates

**Read `references/output-templates.md` before generating any output.**
All outputs must use the exact structured formats defined there.

### 4.1 Summary of Required Outputs

| Situation | Template to Use |
|-----------|----------------|
| Full buy recommendation | BUY_RECOMMENDATION |
| Watchlist addition (valuation hold) | WATCHLIST_ADDITION |
| Stock rejection | REJECTION_LOG |
| Peer dominance detected | PEER_SWITCH_NOTICE |
| Correction mode alert | CORRECTION_ALERT |
| Quarterly review | QUARTERLY_SURVEILLANCE |
| Portfolio-level scan | PORTFOLIO_SCAN_SUMMARY |

### 4.2 BUY_RECOMMENDATION Template (inline reference)

```
══════════════════════════════════════════════════════════════════════
STOCK ANALYSIS REPORT
══════════════════════════════════════════════════════════════════════
STOCK       : [Name] | [NSE/BSE Ticker] | [Sector] | [Market Cap ₹Cr]
DATE        : [YYYY-MM-DD]
ANALYST MODE: [Normal / Correction (X% below peak)]
CMP         : ₹[Price] | 52W High: ₹[X] | 52W Low: ₹[Y]
──────────────────────────────────────────────────────────────────────
GOVERNANCE  : [Score X/15] [GREEN / YELLOW]
MOAT        : [Type] | Durability: [High / Medium / Low]
PRE-SCREEN  : [Score X/9]
──────────────────────────────────────────────────────────────────────
INVESTMENT THESIS
[3–4 lines — why this business can compound at 15%+ CAGR]

FINANCIALS (5Y / 3Y)
  Revenue CAGR      : X%    PAT CAGR       : X%
  ROE (5Y avg)      : X%    ROCE (5Y avg)  : X%
  CFO/Net Profit    : X%    D/E            : X
  Interest Coverage : Xx    EBITDA Margin  : X%

VALUATION
  Current P/E       : Xx    Historical Percentile : Xth (10Y)
  PEG Ratio         : X     FCF Yield             : X%
  EV/EBITDA         : Xx    Verdict               : [EXCELLENT/FAIR/EXPENSIVE/AVOID]
  DCF Intrinsic Val : ₹X–X  [ESTIMATE]
  Methods in Buy Zone: X/5
  Margin of Safety  : X%    Required MoS          : X%  [MET / NOT MET]

PEER RANK  : Quality Rank X of [N] | Valuation Rank X of [N]

WATCHLIST TIER     : [1 / 2 / 3]
CONVICTION         : [High / Medium / Low]
SUGGESTED ALLOC    : X% of portfolio
TRANCHE PLAN
  Tranche 1 (40%)  : ₹[price] — enter now
  Tranche 2 (35%)  : ₹[price] — target or earnings confirmation
  Tranche 3 (25%)  : ₹[price] — deeper correction or 2nd qtr confirmation

RISKS (Premortem)
  Primary   : [Most likely cause of -50% scenario]
  Secondary : [Risk 2]
  Tertiary  : [Risk 3]

EXIT TRIGGERS
  Fundamental  : [Specific metric threshold that invalidates thesis]
  Valuation    : [Price at 85th–90th percentile = ₹X]
  Tax note     : [Days held: X | LTCG applies after: date]

REVIEW DATE        : [Next result date or 90 days, whichever sooner]
DATA QUALITY FLAGS : [List any [DATA UNVERIFIED] or [NOT AVAILABLE] items]
══════════════════════════════════════════════════════════════════════
```

### 4.3 REJECTION_LOG Template (inline reference)

```
──────────────────────────────────────────────────────────────────────
REJECTION LOG
Stock       : [Name] | [Ticker]
Date        : [YYYY-MM-DD]
Failed at   : STEP [X] — [Step Name]
Reason(s)   :
  1. [Primary rejection reason with specific metric]
  2. [Secondary reason if applicable]
Metrics     : [Key metrics that triggered rejection]
Re-evaluate : [Date or condition — e.g., "when pledging drops below 5%"]
──────────────────────────────────────────────────────────────────────
```

### 4.4 WATCHLIST_ADDITION Template

```
──────────────────────────────────────────────────────────────────────
WATCHLIST ADDITION
Stock       : [Name] | [Ticker] | [Sector]
Tier        : [2 — Quality confirmed, valuation not yet attractive]
Date added  : [YYYY-MM-DD]
Moat        : [Type] | Durability: [High / Medium / Low]
Current CMP : ₹[X]  |  Buy Zone Entry: ₹[X–Y]
Required Fall: [X%] from CMP to reach buy zone
Trigger     : Enter Tier 1 when CMP falls to ₹[X] (approx [Y]th pctile)
Quality note: [1 line on why quality is confirmed]
Watch for   : [1–2 specific catalysts or risks to monitor quarterly]
Alert set   : Tranche 1 ₹[price] | Tranche 2 ₹[price]
Re-evaluate : [Specific condition — e.g., "when P/E drops below Xth percentile"]
──────────────────────────────────────────────────────────────────────
```

---

## 5. Quality Assurance Gates

These checks run automatically at the end of Step 9, before output is shown to the user.

### QA Checklist (all must be GREEN before output is released)

```
□ QA-01: Data freshness — price < 24h old, financials < 90 days old
□ QA-02: No hallucinated numbers — every figure sourced or labelled [ESTIMATE]
□ QA-03: Hard gate compliance — Step 0 score recorded; Step 1 score recorded
□ QA-04: Sector benchmark override — if applied, labelled [SECTOR OVERRIDE]
□ QA-05: Margin of Safety calculated using ≥ 2 of 5 valuation methods (PE percentile, PEG, DCF, FCF yield, EV/EBITDA)
□ QA-06: Premortem completed — primary risk category explicitly named
□ QA-07: Exit triggers populated — both fundamental and valuation-based
□ QA-08: Tranche plan populated with specific price levels
□ QA-09: All [DATA UNVERIFIED] or [NOT AVAILABLE] fields logged in output
□ QA-10: Governance score and pre-screen score both appear in output header
□ QA-11: Peer comparison table complete with ≥ 3 peers
□ QA-12: Tax note included (days held and LTCG eligibility date)
```

**If any QA item is RED**: Do not release output. Fix the specific item first.
Log which QA item failed and why in an internal note before retrying.

---

## 6. Edge Case Handling

**Read `references/edge-cases.md` for the full edge case taxonomy.**
The most common edge cases are handled inline here:

### 6.1 New-Age / Pre-Profit Companies
- PAT CAGR and ROE thresholds cannot apply.
- Substitute: Revenue CAGR ≥ 30% + Gross Margin > 40% + path to profitability in ≤ 3Y.
- Apply P/S ratio and EV/Gross Profit as valuation proxies.
- Flag output with: `[EDGE CASE: PRE-PROFIT — MODIFIED CRITERIA APPLIED]`
- Reduce maximum allocation to 3–5% regardless of conviction.

### 6.2 Cyclical Businesses (Steel, Chemicals, Cement)
- Use **10-year through-the-cycle averages** instead of 5-year for all growth and return metrics.
- Use EV/EBITDA at mid-cycle margins (not peak) as primary valuation anchor.
- Classify tailwind as CYCLICAL regardless of management narrative.
- Apply 10% additional discount to intrinsic value for cycle risk.
- Flag output with: `[EDGE CASE: CYCLICAL — MID-CYCLE METRICS APPLIED]`

### 6.3 Financial Services (Banks, NBFCs, Insurance)
- D/E threshold is not applicable. Use Capital Adequacy Ratio (CAR ≥ 15% for banks).
- Replace ROE/ROCE with ROA (≥ 1.5% for banks, ≥ 2.5% for NBFCs).
- CFO/Net Profit ratio is not applicable for financials. Use GNPA trend instead.
- Primary quality metric: GNPA < 2% (banks), < 3% (NBFCs), declining trend.
- Flag output with: `[EDGE CASE: FINANCIAL SERVICES — MODIFIED CRITERIA APPLIED]`

### 6.4 Promoter Holding < 40% (MNC or Widely Held)
- Promoter holding threshold waived if company is a listed MNC subsidiary with parent > 50% or a
  professionally managed company with strong institutional oversight.
- Document waiver explicitly: `[EDGE CASE: LOW PROMOTER HOLDING — WAIVER REASON: X]`

### 6.5 Incomplete Data (< 5 Years of Financials)
- Company is recently listed: use available years; flag as `[EDGE CASE: < 5Y DATA — X years available]`
- Apply a 10% additional discount to intrinsic value for reduced data confidence.
- Max allocation capped at 3% until 5 years of data are available.

### 6.6 Conglomerate / Multi-Segment Business
- Assess each segment separately for moat, growth, and margins.
- Use SoTP (Sum-of-the-Parts) valuation. Discount holding company structure by 15–20%.
- Flag output with: `[EDGE CASE: CONGLOMERATE — SOTP VALUATION APPLIED]`

### 6.7 Stock in Active Correction (≥ 20% from 52W High)
- Re-run Step 5 with Correction Mode MoS thresholds (reduce required MoS by 5%).
- Check if fundamental thesis has changed: re-read last 2 concall transcripts.
- If thesis intact AND correction > 20% → accelerate to Tranche 1 + Tranche 2 simultaneously.
- Flag output with: `[EDGE CASE: CORRECTION ENTRY — DUAL TRANCHE AUTHORISED]`

### 6.8 Peer Dominance Detected (Step 7)
- Issue PEER_SWITCH_NOTICE immediately.
- Pause analysis of original stock.
- Restart from Step 0 with the dominant peer as the new subject.
- If the peer also passes all steps, recommend the peer over the original.

---

## 7. Error Recovery Playbook

**Read `references/error-recovery.md` for extended error scenarios.**

### 7.1 Data Fetch Failures

| Error Type | Recovery Action |
|-----------|----------------|
| NSE/BSE unreachable | Retry once after 30s; fallback to Screener.in or Trendlyne |
| Screener.in rate limit | Wait 60s; retry; if still failing, proceed with partial data and flag `[PARTIAL DATA]` |
| No 10Y historical P/E available | Use 5Y range; flag as `[5Y PERCENTILE — 10Y DATA UNAVAILABLE]`; apply 5% additional MoS |
| Concall transcript not available | Note `[CONCALL UNAVAILABLE]`; rely on press release and annual report commentary |
| Pledging data missing | Fetch from BSE shareholding pattern directly; if still missing, treat as `[PLEDGING UNKNOWN]` and apply max scrutiny in Step 1 |

### 7.2 Conflicting Data Across Sources

**Rule**: When two reputable sources conflict on a financial metric:
1. Prefer NSE/BSE official filings over third-party aggregators.
2. If the conflict is > 10% on a key metric (Revenue, PAT, ROE), halt the relevant step.
3. Output: `[DATA CONFLICT: metric X shows Y on source A, Z on source B. Using NSE/BSE filing value. Flagging for user review.]`
4. Continue with the filing-sourced value.

### 7.3 Stale Data Detected

**Trigger**: Price data > 48h old, or financial statements > 90 days old post result date.

**Action**:
1. Flag: `[STALE DATA: [metric] last updated [date] — [X] days old]`
2. Attempt re-fetch once.
3. If re-fetch fails, continue with stale data but append `[ANALYSIS MAY BE OUTDATED]` to the output header.
4. Recommend user verify CMP and latest shareholding before acting.

### 7.4 Step Failure Mid-Pipeline

**If a hard gate fails mid-pipeline** (Step 1 or Step 3):
1. Stop immediately. Do not proceed to next step.
2. Issue REJECTION_LOG with specific failing step and metric.
3. Note re-evaluation condition (e.g., "re-evaluate when pledging < 5%").
4. Add to rejection tracker. Do not output a partial recommendation.

### 7.5 Mode Detection Failure (Nifty Data Unavailable)

1. Default to MODE A (Normal Mode).
2. Append `[MODE UNCONFIRMED — NIFTY FETCH FAILED]` to output header.
3. Do not issue Correction Alert.
4. Proceed with standard MoS thresholds.

### 7.6 All Valuation Methods Inconclusive

**Trigger**: DCF range is too wide (bull/bear scenarios differ > 100%) AND no 10Y historical range is available.

**Action**:
1. Flag: `[VALUATION INCONCLUSIVE — HIGH UNCERTAINTY]`
2. Do not issue a BUY_RECOMMENDATION.
3. Issue WATCHLIST_ADDITION at Tier 3 (track only).
4. Note: "Recommend waiting for ≥ 2 more quarters of financial data before valuation assessment."

---

## 8. Ongoing Surveillance Trigger Rules

After a stock enters the portfolio, the following events trigger an immediate re-run of
Steps 1 and 3 (governance and financial checks):

```
EVENT                                 URGENCY        ACTION
────────────────────────────────────────────────────────────────────────
Promoter stake sale > 2% in a quarter Immediate      Re-run Step 1; review conviction
Pledging increase > 2pp               Immediate      Re-run Step 1; potential exit flag
Quarterly revenue miss > 10%          Within 48h     Full Steps 1–5 re-run
SEBI/ED notice issued                 Immediate      Step 1 re-run; likely exit
Auditor change (unexpected)           Within 24h     Step 1 re-run; exit if unjustified
CFO/NP falls below 60%                Next result    Step 3 re-run; flag for review
Concall: guidance withdrawn/cut       Within 48h     Steps 3–5 re-run; tranche pause
Credit rating downgrade               Within 24h     Step 3 re-run; check ICR and D/E
Corporate action (bonus/split)        5 days         Update price targets & tranches
────────────────────────────────────────────────────────────────────────
```

**Quarterly Surveillance Checklist** (auto-generate within 48h of each result):
```
□ Revenue growth on track vs thesis? (expected: X%, actual: X%)
□ EBITDA margin: expanding / stable / compressing?
□ CFO/Net Profit this quarter: > 80%?
□ Governance events in the quarter: YES/NO (detail if YES)
□ Management tone on concall: confident / cautious / defensive?
□ Order book / pipeline growth: YES/NO
□ Promoter shareholding change: +/-X pp
□ Working capital: DSO/inventory days stable?
□ Analyst estimate revisions: up / flat / down by X%?
□ Valuation percentile: Xth (still in hold / buy / trim zone?)
□ Thesis: INTACT / PARTIALLY IMPAIRED / BROKEN
□ Action: HOLD / ADD / TRIM / EXIT
```

Scoring: ≥ 8 green → HOLD or ADD | 5–7 green → MONITOR | < 5 or thesis broken → INITIATE EXIT

---

## 9. Reference Files Index

Read these files when the situation requires deeper reference:

| File | When to Read |
|------|-------------|
| `references/data-sources.md` | At the start of every analysis; maps each data need to a specific URL and fetch method |
| `references/sector-benchmarks.md` | At Step 3 when applying financial thresholds; read the specific sector subsection |
| `references/output-templates.md` | At Step 9 before generating any output; contains full templates for all 7 output types |
| `references/edge-cases.md` | When any edge case flag is triggered during Steps 0–7 |
| `references/error-recovery.md` | When any data fetch fails or data conflict is detected |

---

## 10. Skill Self-Check (Run Before Every Session)

```
□ Is the companion framework (long_term_investing_context_v2.md) accessible?
□ Are all 5 reference files accessible?
□ Is web_search available? (required for real-time data)
□ Is web_fetch available? (required for NSE/BSE filings)
□ Is today's date known? (required for data freshness checks and LTCG tax calculations)
□ Has the user specified a stock, sector, or portfolio to analyse?
   → If not: ask "Which stock or sector would you like me to analyse?"
```

If any prerequisite is missing, notify the user before beginning analysis.
Do not attempt analysis with broken tooling — partial outputs with hallucinated data
are worse than no output.

---

*Skill version: 1.0 | Companion framework: long_term_investing_context_v2.md*
*Architecture review: annually or after major market regime change*
