# Error Recovery Playbook
# Companion file for SKILL.md — indian-equity-long-term-investor
# Read this file whenever a data fetch fails, a conflict is detected,
# or an unexpected situation arises during analysis.

## Recovery Principle
> Incomplete analysis with honest labelling is always better than complete analysis with
> hallucinated data. Label every gap. Never substitute training knowledge for live financial data.

---

## Error Index

| ID | Error Type | Section |
|----|-----------|---------|
| ER-01 | Source unreachable / timeout | 1 |
| ER-02 | Rate limiting / blocked | 2 |
| ER-03 | Data conflict between sources | 3 |
| ER-04 | Stale data detected | 4 |
| ER-05 | Partial data (some metrics unavailable) | 5 |
| ER-06 | Hard gate failure mid-pipeline | 6 |
| ER-07 | Valuation inconclusive | 7 |
| ER-08 | Mode detection failure | 8 |
| ER-09 | Peer comparison data unavailable | 9 |
| ER-10 | Concall / management data unavailable | 10 |
| ER-11 | Historical P/E range not available | 11 |
| ER-12 | Annual report inaccessible | 12 |
| ER-13 | SEBI search returns no results | 13 |
| ER-14 | DCF inputs too uncertain for reliable estimate | 14 |
| ER-15 | Stock not listed on NSE (BSE-only) | 15 |

---

## ER-01: Source Unreachable / Timeout

**Trigger**: web_fetch or web_search returns timeout or connection refused for a primary source.

**Recovery sequence**:
```
Attempt 1: Retry the same URL after 30 seconds
Attempt 2: Switch to the fallback source listed in data-sources.md for that data type
Attempt 3: Use web_search with specific query to find the data (e.g., "TICKER ROE ROCE screener")
Attempt 4: If all attempts fail, label the metric [NOT AVAILABLE — SOURCE UNREACHABLE]

Primary → Fallback source mapping:
  Screener.in      → Trendlyne.com
  NSE India        → BSE India (https://www.bseindia.com)
  Trendlyne        → Moneycontrol financials page
  SEBI SCORES      → SEBI enforcement orders page (direct URL)
  NSE filings      → BSE corporate filings
```

**Output label**: `[NOT AVAILABLE — SOURCE UNREACHABLE: source name]`
**Action**: Continue analysis with remaining available data. Do not halt the pipeline.

---

## ER-02: Rate Limiting / Blocked

**Trigger**: Source returns HTTP 429 (Too Many Requests) or displays a CAPTCHA or anti-bot page.

**Recovery sequence**:
```
1. Wait exactly 90 seconds before any retry on the rate-limited source.
2. During the wait: fetch from the fallback source (see ER-01 mapping).
3. After 90s: retry once. If blocked again, mark as [RATE LIMITED] and use fallback data.
4. Do not attempt more than 2 retries on a rate-limited source in a single session.
5. If all fallbacks are also rate-limited: label all affected metrics [DATA TEMPORARILY UNAVAILABLE]
   and complete the analysis with a strong recommendation to user to verify before acting.
```

**Output label**: `[DATA TEMPORARILY UNAVAILABLE — RATE LIMITED]`
**User notification**: Append at end of output: "Note: [X] data points were temporarily unavailable
due to source rate limiting. Please verify these figures before executing any trade."

---

## ER-03: Data Conflict Between Sources

**Trigger**: The same metric shows meaningfully different values across two reputable sources
(difference > 10% on key metrics like Revenue, PAT, ROE, or Debt/Equity).

**Example**: Screener.in shows ROE of 18%, Trendlyne shows ROE of 23% for the same period.

**Recovery sequence**:
```
Priority rule: NSE/BSE official filing > Screener.in > Trendlyne > Moneycontrol

Step 1: Identify which source is using consolidated vs. standalone financials.
        Indian companies often report both. Most analysis should use CONSOLIDATED.
        If the source mismatch is consolidated vs. standalone: use consolidated; note it.

Step 2: If both sources are using the same reporting basis and still conflict:
        → Fetch the original BSE/NSE filing (the XBRL annual results) and use that value.
        → Label: [DATA CONFLICT RESOLVED — USING NSE/BSE FILING VALUE]

Step 3: If the original filing cannot be fetched:
        → Use the lower (more conservative) of the two values.
        → Label: [DATA CONFLICT — CONSERVATIVE VALUE USED: metric = X (Source A: Y, Source B: Z)]

Step 4: Note the conflict prominently in the Data Quality section of the output.
```

**Output label**: `[DATA CONFLICT: metric = X (conservative). Source A: Y, Source B: Z]`
**Risk implication**: Data conflicts for key metrics (ROE, Debt/Equity) should reduce conviction
by one level (High → Medium; Medium → Low) until resolved.

---

## ER-04: Stale Data Detected

**Trigger**:
- Price data > 48 hours old
- Quarterly financials > 90 days old
- Annual report > 15 months old
- Shareholding pattern > 95 days old (one quarter delayed)

**Recovery sequence**:
```
Step 1: Attempt re-fetch with a fresh search query including today's date.
Step 2: If fresh data is unavailable, explicitly label stale items.
Step 3: Assess staleness risk:
        - Price data stale: do not use stale price for tranche plan. State "verify CMP before acting."
        - Financial data stale: note which period it covers; proceed but flag.
        - Shareholding stale: use last available; note when next filing is due.
```

**Output labels**:
```
[STALE PRICE: last trade date. Verify CMP before executing any order]
[STALE FINANCIALS: Q[X]FY[YY] data. Next expected: date]
[STALE SHAREHOLDING: [Quarter]FY[YY]. Next filing due: date]
```

**User notification**: When price data is stale, explicitly state: "Tranche prices below are
based on a stale CMP of ₹[X] as of [date]. Recalculate based on live price before ordering."

---

## ER-05: Partial Data (Some Metrics Unavailable)

**Trigger**: Multiple data points are missing but the analysis can still be completed
with the available data.

**Step-specific handling**:

| Step | Missing Data | Action |
|------|-------------|--------|
| Step 0 | 1–2 metrics unavailable | Score remaining metrics; apply [NOT AVAILABLE] to missing ones; note in scoring |
| Step 0 | > 4 metrics unavailable | Halt Step 0; flag as [INSUFFICIENT DATA FOR PRE-SCREEN]; move to Tier 3 watchlist |
| Step 1 | Pledging data unavailable | Apply [PLEDGING UNKNOWN — MAXIMUM SCRUTINY]; reduce Step 1 score by 2 |
| Step 1 | RPT data unavailable | Mark [RPT UNKNOWN]; note in risks; continue |
| Step 3 | CFO data unavailable | Note [CFO/NP UNAVAILABLE]; apply 10% additional MoS discount |
| Step 5 | 10Y historical P/E unavailable | Use 5Y range (ER-11); apply 5% additional MoS |
| Step 7 | Peer data unavailable for all peers | Skip formal comparison; note limitation; do not issue PEER_SWITCH_NOTICE |

**Overall partial data rule**: If > 30% of required metrics are unavailable:
- Downgrade recommendation from BUY to WATCHLIST_ADDITION (Tier 2)
- Note: "Insufficient data for full conviction. Recommend completing research before entry."

---

## ER-06: Hard Gate Failure Mid-Pipeline

**Trigger**: Step 1 or Step 3 returns a FAIL result after the pipeline has already started.

**Action**:
```
1. Stop immediately. Do not execute any further steps.
2. Generate REJECTION_LOG using the template from output-templates.md.
3. Do not generate a partial BUY_RECOMMENDATION with caveats. A partial recommendation
   after a hard gate failure is more dangerous than no recommendation.
4. Add to rejection tracker with the specific gate that failed and the metric values.
5. State clearly: "Re-evaluation condition: [specific condition that must change]"
6. Do not present speculation about "what if the metric improves."
```

**Never do**: Issue a conditional buy recommendation when a hard gate is failed.
Hard gates exist precisely because no valuation discount justifies those risks.

---

## ER-07: Valuation Inconclusive

**Trigger**: DCF bear/bull scenario spread is > 100% (e.g., bear = ₹100, bull = ₹250)
AND 10Y historical P/E range is unavailable.

**This means**: The business is either too early-stage, too cyclical, or too data-sparse
for a reliable intrinsic value estimate.

**Action**:
```
1. Do not issue a BUY_RECOMMENDATION.
2. Do not guess at a "central estimate" — the spread is too wide to be useful.
3. Issue WATCHLIST_ADDITION at Tier 3 (track only):
   "Recommend waiting for ≥ 2 more quarters of financial data before valuation is feasible."
4. Note in the watchlist entry: what additional data or time is needed before
   a proper valuation can be conducted.
5. Flag: [EDGE CASE ER-07: VALUATION INCONCLUSIVE — TIER 3 WATCHLIST]
```

**Output label**: `[VALUATION INCONCLUSIVE — HIGH UNCERTAINTY. Tier 3 watchlist only.]`

---

## ER-08: Mode Detection Failure (Nifty Data Unavailable)

**Trigger**: NSE Nifty 50 data cannot be fetched from any source.

**Action**:
```
1. Default to MODE A (Normal Mode).
2. Do not issue a CORRECTION_ALERT (cannot confirm correction without data).
3. Append to output header: [MODE UNCONFIRMED — NIFTY FETCH FAILED. Defaulting to Normal Mode.]
4. Use standard MoS thresholds (do not apply Correction Mode discount).
5. Advise user to manually check Nifty level and notify if correction thresholds are met.
```

**Output label**: `[MODE UNCONFIRMED — NIFTY DATA UNAVAILABLE]`

---

## ER-09: Peer Comparison Data Unavailable

**Trigger**: Cannot fetch financial data for ≥ 2 of the 3 required peers.

**Action**:
```
1. If 1 peer available: conduct partial comparison; note limitation.
2. If 0 peers available: skip formal peer table.
3. Provide qualitative peer commentary based on known sector context:
   "Peer comparison data was unavailable. Based on sector context, [Company] appears
   [positioned / priced] [describe qualitatively]."
4. Note the limitation clearly in Data Quality Flags.
5. Do not issue a PEER_SWITCH_NOTICE without data to support it.
```

**Output label**: `[PEER DATA UNAVAILABLE — QUALITATIVE COMPARISON ONLY]`
**Impact**: Reduce conviction by one level (High → Medium) when peer data is absent.

---

## ER-10: Concall / Management Commentary Unavailable

**Trigger**: The most recent concall transcript or investor presentation cannot be fetched.

**Recovery**:
```
1. Search for press release / result PDF from the company's IR page.
2. Look for analyst summaries of the concall on Trendlyne or IndiaNotes.
3. Check if the company posts concall audio/video (transcribe key comments if needed).
4. If all fail: note [CONCALL UNAVAILABLE — Q[X]FY[YY]].
5. Rely on the annual report's management discussion & analysis (MD&A) section instead.
6. For Step 8 premortem: note "management forward-looking guidance unverified" as an
   additional minor risk factor.
```

**Output label**: `[CONCALL UNAVAILABLE — MD&A SECTION USED AS SUBSTITUTE]`

---

## ER-11: 10-Year Historical P/E Range Unavailable

**Trigger**: Trendlyne or comparable source cannot provide 10-year P/E historical range.

**Recovery**:
```
1. Use 5-year P/E range instead.
2. If 5-year range is also unavailable, use the following sector median P/E ranges
   as a rough reference (document source as "sector median estimate"):
   FMCG: 35–65x | IT: 20–40x | Pharma: 18–35x | Banks: 10–25x (P/BV: 1–4x)
   Capital Goods: 20–50x | Renewables: 20–45x | Specialty Chemicals: 18–40x
3. Apply 5% additional MoS discount when using < 10Y data.
4. Label the percentile as: [5Y PERCENTILE — 10Y UNAVAILABLE] or [SECTOR ESTIMATE — INDIVIDUAL DATA UNAVAILABLE].
```

**Output label**: `[5Y PERCENTILE — 10Y HISTORICAL DATA UNAVAILABLE. 5% ADDITIONAL MoS APPLIED]`

---

## ER-12: Annual Report Inaccessible

**Trigger**: The company's annual report PDF cannot be fetched from the IR page or BSE.

**Impact**: Step 1 (RPT, contingent liabilities, auditor notes) is partially incomplete.

**Recovery**:
```
1. Search BSE corporate filings for the annual report (search by ISIN or company name).
2. Try NSE's annual report archive.
3. Search: "[Company Name] annual report [year] site:bseindia.com"
4. If still unavailable, use the quarterly investor presentation as a partial substitute.
5. Note explicitly which governance checks could not be verified.
6. Reduce Step 1 score by 2 for unverified sub-factors.
7. Note: "Annual report unavailable. RPT, contingent liability, and auditor checks
   are partially unverified. Governance score reflects this uncertainty."
```

**Output label**: `[ANNUAL REPORT UNAVAILABLE — GOVERNANCE CHECKS PARTIALLY UNVERIFIED]`

---

## ER-13: SEBI Search Returns No Results

**Trigger**: SEBI SCORES or enforcement page search for the company name returns no results.

**Interpretation**:
```
No results on SEBI SCORES = no complaints registered against the company = POSITIVE signal.
No results on SEBI enforcement = no orders against the company = POSITIVE signal.
Do NOT interpret as "data unavailable." A clean SEBI record is confirmatory, not missing data.
```

**Action**: Record as "SEBI record: Clean — no enforcement orders or SCORES complaints found."
Give full marks for the "Clean regulatory history" governance sub-factor.

---

## ER-14: DCF Inputs Too Uncertain for Reliable Estimate

**Trigger**: The business is in an early stage, highly cyclical, or undergoing significant
restructuring, making 10-year FCF projections unreliable even in ranges.

**Indicator**: If bear case intrinsic value < 50% of bull case intrinsic value.

**Recovery**:
```
1. Do not use DCF as a primary valuation method for this stock.
2. Use Historical Percentile + PEG Ratio + Peer Multiple as the two required methods.
3. If those two methods also produce a wide range: trigger ER-07 (Valuation Inconclusive).
4. If those two methods are usable: proceed without DCF; note the omission explicitly.
```

**Output label**: `[DCF OMITTED — INSUFFICIENT FORWARD VISIBILITY. HISTORICAL + PEER MULTIPLES USED]`

---

## ER-15: Stock Listed Only on BSE (Not on NSE)

**Trigger**: The stock does not have an NSE listing; it is BSE-only.

**Implications**:
```
1. Liquidity is typically lower for BSE-only stocks. Verify daily trading volume > ₹2 Cr.
2. Use BSE as the primary data source instead of NSE for all price and filing data.
3. Historical P/E data may be harder to find — try Screener.in or Trendlyne with BSE code.
4. If market cap < ₹2,000 Cr AND BSE-only: apply small cap edge case rules (EC-11).
5. Note the BSE-only status in the output header.
```

**Output label**: `[BSE-ONLY LISTING — LIQUIDITY VERIFIED: avg daily volume ₹X Cr]`

---

## General Recovery Decision Tree

```
Data fetch attempted
        │
        ├── SUCCESS → proceed normally
        │
        └── FAILURE
                │
                ├── Retry (30s wait) → SUCCESS → proceed with [RETRY flag]
                │
                └── RETRY FAILURE
                        │
                        ├── Fallback source available?
                        │       │
                        │       ├── YES → fetch from fallback → SUCCESS → [FALLBACK flag]
                        │       │
                        │       └── NO → label [NOT AVAILABLE]
                        │
                        └── Is this a hard-gate metric?
                                │
                                ├── YES (pledging, auditor, PAT, ROE for hard gate)
                                │   → Apply maximum conservative assumption
                                │   → Reduce conviction by one level
                                │   → Note limitation prominently
                                │
                                └── NO → proceed; note in Data Quality flags
```

---

## Recovery Quality Standards

After any error recovery, verify:
```
□ Every recovered metric is clearly labelled with the appropriate error tag
□ The Data Quality Flags section of the output lists ALL error tags
□ Conviction level has been adjusted if ≥ 3 error tags are present
□ User has been notified if price data is stale (tranche prices need recalculation)
□ No metric has been estimated or recalled from training data without a label
□ The final recommendation accounts for data gaps (e.g., reduced conviction, lower allocation)
```

**Rule**: If ≥ 5 distinct error tags are present in a single analysis:
→ Downgrade the output from BUY_RECOMMENDATION to WATCHLIST_ADDITION.
→ State: "Insufficient data quality for a full conviction recommendation. Complete data
verification recommended before initiating a position."
