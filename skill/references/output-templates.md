# Output Templates Reference
# Companion file for SKILL.md — indian-equity-long-term-investor
# Use the exact templates below. Do not abbreviate or skip fields.
# Fields marked [REQUIRED] must always be populated.
# Fields marked [IF APPLICABLE] can be omitted only when genuinely not applicable.

---

## Template Index
1. BUY_RECOMMENDATION — Full buy with tranche plan
2. WATCHLIST_ADDITION — Quality pass but valuation not ready
3. REJECTION_LOG — Any step gate failure
4. PEER_SWITCH_NOTICE — Peer dominance detected at Step 7
5. CORRECTION_ALERT — Mode B activation notification
6. QUARTERLY_SURVEILLANCE — Post-result portfolio check
7. PORTFOLIO_SCAN_SUMMARY — Batch scan of multiple stocks

---

## Template 1: BUY_RECOMMENDATION

Use when: All 9 steps pass and conviction is sufficient for entry.

```
══════════════════════════════════════════════════════════════════════════
                        STOCK ANALYSIS REPORT
══════════════════════════════════════════════════════════════════════════
STOCK           : [Full Name] | [NSE Ticker] | [Sector / Sub-Sector]    [REQUIRED]
MARKET CAP      : ₹[X,XXX] Cr | [Large / Mid / Small Cap]              [REQUIRED]
REPORT DATE     : [YYYY-MM-DD HH:MM IST]                                [REQUIRED]
ANALYST MODE    : [Normal / Correction — Nifty X% below peak]           [REQUIRED]
DATA FRESHNESS  : Price as of [date/time] | Financials as of [Q period] [REQUIRED]
──────────────────────────────────────────────────────────────────────────
PRICE DATA
  CMP           : ₹[X.XX]
  52W High      : ₹[X] | 52W Low: ₹[X]
  % from 52W H  : [X%] | % from 52W L: [X%]
  200-DMA       : ₹[X] | CMP vs 200-DMA: [Above / Below by X%]
──────────────────────────────────────────────────────────────────────────
SCREENING SUMMARY
  Pre-Screen Score  : [X/9]  [PASS / CONDITIONAL / FAIL]              [REQUIRED]
  Governance Score  : [X/15] [GREEN / YELLOW / FAIL]                  [REQUIRED]
  Step 3 Gate       : [PASS GREEN / PASS CONDITIONAL / FAIL]           [REQUIRED]
  Valuation Gate    : [BUY ZONE / FAIR VALUE / DO NOT BUY]             [REQUIRED]
  Technical Signal  : [X/5 signals] [GREEN / AMBER / RED]             [REQUIRED]
  Peer Rank         : Quality [X of N] | Valuation [X of N]           [REQUIRED]
──────────────────────────────────────────────────────────────────────────
MOAT ASSESSMENT
  Type              : [Brand / Network / Cost / Switching / Regulatory / Scale / IP]
  Durability        : [High / Medium / Low]
  Moat Narrative    : [2–3 lines describing the competitive advantage]  [REQUIRED]
──────────────────────────────────────────────────────────────────────────
INVESTMENT THESIS                                                        [REQUIRED]
  [3–4 sentences: why this business can compound at 15%+ CAGR,
   what structural advantage it holds, and why now is a reasonable
   entry point]
──────────────────────────────────────────────────────────────────────────
FINANCIAL PERFORMANCE
                        5Y CAGR     3Y CAGR     Latest Year
  Revenue               [X%]        [X%]        ₹[X] Cr
  EBITDA                [X%]        [X%]        ₹[X] Cr | Margin: [X%]
  PAT                   [X%]        [X%]        ₹[X] Cr

  Return Ratios         5Y Avg      Latest
  ROE                   [X%]        [X%]
  ROCE                  [X%]        [X%]

  Earnings Quality      3Y Avg      Latest
  CFO / Net Profit      [X%]        [X%]

  Balance Sheet         Latest
  Debt / Equity         [X]         Interest Coverage: [Xx]
  Net Debt / EBITDA     [Xx]        Current Ratio: [X]
──────────────────────────────────────────────────────────────────────────
VALUATION
  Current P/E           : [Xx]      10Y Historical Percentile: [Xth]   Verdict: [EXCELLENT/FAIR/EXPENSIVE/AVOID]
  Current EV/EBITDA     : [Xx]      10Y Historical Percentile: [Xth]   Verdict: [EXCELLENT/FAIR/EXPENSIVE/AVOID]
  PEG Ratio             : [X.X]     (Forward 2Y EPS CAGR: [X%])        Verdict: [EXCELLENT/FAIR/EXPENSIVE/AVOID]
  FCF Yield             : [X%]                                          Verdict: [ATTRACTIVE/FAIR/EXPENSIVE]
  DCF Intrinsic Value   : ₹[X] – ₹[X] [ESTIMATE]
    Base case (50%)     : ₹[X]
    Bull case (25%)     : ₹[X]
    Bear case (25%)     : ₹[X]
  Methods in Buy Zone   : [X/5]
  Peer Median P/E       : [Xx]      Premium/Discount to peers: [+/-X%]
  Margin of Safety      : [X%]      Required MoS: [X%]  [MET / NOT MET]
──────────────────────────────────────────────────────────────────────────
INDUSTRY TAILWIND
  Sector                : [name]
  Tailwind Type         : [Structural / Policy-driven / Cyclical]
  Cycle Position        : [Early / Mid / Late]
  Growth Runway         : [X–Y years visible]
  Tailwind Narrative    : [2 lines]                                    [REQUIRED]
──────────────────────────────────────────────────────────────────────────
PEER COMPARISON                                                          [REQUIRED]
  Company      Rev CAGR  PAT CAGR  ROE   ROCE  Fwd P/E  EV/EBITDA  Pledging
  [TARGET]     [X%]      [X%]      [X%]  [X%]  [Xx]     [Xx]       [X%]  ← focus
  [Peer 1]     [X%]      [X%]      [X%]  [X%]  [Xx]     [Xx]       [X%]
  [Peer 2]     [X%]      [X%]      [X%]  [X%]  [Xx]     [Xx]       [X%]
  [Peer 3]     [X%]      [X%]      [X%]  [X%]  [Xx]     [Xx]       [X%]
──────────────────────────────────────────────────────────────────────────
RECOMMENDATION
  Watchlist Tier    : [1]  (Ready to Buy)                             [REQUIRED]
  Conviction        : [High / Medium / Low]                           [REQUIRED]
  Suggested Alloc   : [X%] of portfolio                               [REQUIRED]

  ENTRY PLAN (STAGGERED)                                              [REQUIRED]
  Tranche 1 (40%)   : ₹[price] — enter at CMP | Condition: [now / on dip to ₹X]
  Tranche 2 (35%)   : ₹[price] — [condition: e.g., 8% further dip or Q[X] result]
  Tranche 3 (25%)   : ₹[price] — [condition: e.g., 15% dip or 2nd qtr confirmation]
──────────────────────────────────────────────────────────────────────────
RISK ANALYSIS (PREMORTEM)                                               [REQUIRED]
  Primary Risk    : [Most likely cause of -50% scenario — specific and named]
  Secondary Risk  : [Risk 2]
  Tertiary Risk   : [Risk 3]
  Risk Assessment : [Cyclical/Manageable → Proceed | Structural → Do Not Recommend]
──────────────────────────────────────────────────────────────────────────
EXIT STRATEGY                                                           [REQUIRED]
  Fundamental Exit: [Specific trigger — e.g., "ROE falls below 14% for 2 years"]
  Valuation Exit  : ₹[X] (approx 85th–90th P/E percentile = Xx)
  Stop-Loss       : ₹[X]  (Large Cap: 18% below CMP | Mid Cap: 25% | Small Cap: 30%)
  LTCG Note       : Purchased [date] → LTCG eligible after [date]
──────────────────────────────────────────────────────────────────────────
DATA QUALITY                                                            [REQUIRED]
  Flags           : [List all [DATA UNVERIFIED], [NOT AVAILABLE], [ESTIMATE],
                    [STALE DATA], [SECTOR OVERRIDE], [CONDITIONAL PASS] items]
                    OR: "All data verified. No flags."
══════════════════════════════════════════════════════════════════════════
  NEXT REVIEW DATE: [Next quarterly result date or 90 days, whichever sooner]
══════════════════════════════════════════════════════════════════════════
```

---

## Template 2: WATCHLIST_ADDITION

Use when: Stock passes Steps 1–5 quality checks but valuation is not yet in buy zone.

```
──────────────────────────────────────────────────────────────────────────
WATCHLIST ADDITION
──────────────────────────────────────────────────────────────────────────
Stock           : [Name] | [Ticker] | [Sector]                         [REQUIRED]
Tier            : [2 — Quality confirmed, valuation not in buy zone]   [REQUIRED]
Date Added      : [YYYY-MM-DD]                                          [REQUIRED]
Pre-Screen      : [X/9] | Governance: [X/15]                           [REQUIRED]
Moat            : [Type] | Durability: [High / Medium / Low]           [REQUIRED]
Quality Summary : [1-line on why this business is worth watching]      [REQUIRED]

PRICE CONTEXT
  CMP           : ₹[X]
  Buy Zone Entry: ₹[X] – ₹[Y]  (approx [Xth] P/E percentile)
  Required Fall : [X%] from CMP to reach buy zone
  Margin of Safety at CMP: [X%] (Required: [Y%]) — [NOT MET]

ALERT SETTINGS                                                          [REQUIRED]
  Tranche 1 Alert: Set at ₹[X]  (= [X%] below CMP)
  Tranche 2 Alert: Set at ₹[X]  (= [X%] below CMP)

WATCH ITEMS                                                             [REQUIRED]
  1. [Specific metric or event to monitor — e.g., "Promoter pledging must stay < 5%"]
  2. [Catalyst to watch — e.g., "Order book growth in Q2FY27 result"]
  3. [Risk to watch — e.g., "US FDA inspection at Gujarat plant Q3FY27"]

RE-EVALUATE TRIGGER: [Condition under which to run full 9-step analysis again]
  e.g., "Re-run full analysis when P/E drops to Xth percentile or CMP reaches ₹[X]"
NEXT REVIEW DATE : [Next quarterly result date or 90 days]
──────────────────────────────────────────────────────────────────────────
```

---

## Template 3: REJECTION_LOG

Use when: Any hard gate (Step 0, 1, or 3) is failed, or Step 5 shows DO NOT BUY.

```
──────────────────────────────────────────────────────────────────────────
REJECTION LOG
──────────────────────────────────────────────────────────────────────────
Stock           : [Name] | [Ticker] | [Sector]                         [REQUIRED]
Date            : [YYYY-MM-DD]                                          [REQUIRED]
Rejected at     : STEP [X] — [Step Name]                               [REQUIRED]
Rejection Type  : [HARD REJECT / CONDITIONAL FAIL / VALUATION HOLD]    [REQUIRED]

REJECTION REASONS                                                        [REQUIRED]
  1. [Primary reason — specific metric with value]
     e.g., "Promoter pledging at 18% (threshold: < 10%)"
  2. [Secondary reason if applicable]
  3. [Tertiary reason if applicable]

KEY METRICS AT REJECTION
  [List 3–5 most relevant metrics with their values]

RE-EVALUATE CONDITION: [Specific condition under which to re-run analysis]
  e.g., "Re-evaluate when pledging falls below 5% for 2 consecutive quarters"
  OR: "Do not re-evaluate — structural issue (governance fraud allegation)"

REJECTION CATEGORY:
  [ ] Governance failure (GOVERNANCE REJECT — do not re-evaluate unless condition clears)
  [ ] Financial weakness (revisit after 2 quarters of improvement)
  [ ] Valuation — add to Tier 2 watchlist
  [ ] Business model concern (structural — low probability of re-evaluation)
──────────────────────────────────────────────────────────────────────────
```

---

## Template 4: PEER_SWITCH_NOTICE

Use when: At Step 7, a peer is identified that scores higher on quality AND lower on valuation.

```
──────────────────────────────────────────────────────────────────────────
⚠ PEER DOMINANCE DETECTED — ANALYSIS REDIRECTED
──────────────────────────────────────────────────────────────────────────
Original Stock  : [Name] | [Ticker]
Dominant Peer   : [Name] | [Ticker]
Date            : [YYYY-MM-DD]

COMPARISON
  Metric          [Original]    [Dominant Peer]   Difference
  Quality Score   [X/10]        [X/10]            Peer +[X]
  Valuation Score [X/10]        [X/10]            Peer +[X]
  Rev CAGR        [X%]          [X%]              Peer +[X%]
  ROE             [X%]          [X%]              Peer +[X%]
  Fwd P/E         [Xx]          [Xx]              Peer lower
  Governance      [X/15]        [X/15]            Peer higher

ACTION TAKEN:
  ✗ Analysis of [Original] paused.
  ✓ Restarting full 9-step analysis on [Dominant Peer] from Step 0.
  [If original stock was in portfolio]: Note for portfolio review — consider switch.

NOTE TO USER:
  [1-line explanation of why the peer appears more attractive]
──────────────────────────────────────────────────────────────────────────
```

---

## Template 5: CORRECTION_ALERT

Use when: Mode detection (pre-Step 0) finds Nifty ≥ 8% below 52-week peak.

```
══════════════════════════════════════════════════════════════════════════
⚠  MARKET CORRECTION ALERT — MODE B ACTIVATED
══════════════════════════════════════════════════════════════════════════
Date            : [YYYY-MM-DD HH:MM IST]
Nifty 50 CMP    : [X,XXX]
Nifty 52W High  : [X,XXX] on [date]
Decline from Peak: [X%]
Correction Level : [Priority 2 (8–12%) / Priority 1 (12–15%) / MAXIMUM (>15%)]

IMPLICATION
  [Describe what this correction level means for deployment posture]
  e.g., "Priority 1 — High urgency scan. Deploy Tranche 1 on confirmed Tier-1
  names. Maintain Tranche 2 & 3 in reserve. Do not fully deploy cash."

FAST-RECOVERY SECTOR PRIORITY (deploy in this order):
  1. Private Banking & NBFCs
  2. Defence & Capital Goods
  3. Pharma & Healthcare
  4. IT & Technology
  5. Consumption
  6. Renewables & Infrastructure

CAPITAL DEPLOYMENT RULES:
  Max per correction event    : 20–25% of available cash
  Staging window              : 4–8 weeks (or faster if decline > 20%)
  Cash buffer to retain       : ≥ 10–15%
  Priority action             : Add to existing high-conviction holdings FIRST

TIER-1 WATCHLIST STATUS (auto-generated):
  [List all Tier-1 stocks with current CMP vs. Tranche 1 entry price]
  Stock     Tranche 1 Target   CMP       Status
  [Name]    ₹[X]               ₹[X]      [ENTER NOW / WAIT X% MORE]
  ...

NEXT UPDATE: Re-run mode detection at market open tomorrow.
══════════════════════════════════════════════════════════════════════════
```

---

## Template 6: QUARTERLY_SURVEILLANCE

Use when: Within 48 hours of a quarterly result for any portfolio holding or Tier-1 stock.

```
──────────────────────────────────────────────────────────────────────────
QUARTERLY SURVEILLANCE REPORT
──────────────────────────────────────────────────────────────────────────
Stock           : [Name] | [Ticker]
Quarter         : Q[X]FY[YY]  |  Report Date: [YYYY-MM-DD]
Analysis Date   : [YYYY-MM-DD]
Current Holding : [X% of portfolio] | Avg Cost: ₹[X] | Days held: [X]
──────────────────────────────────────────────────────────────────────────
RESULT SNAPSHOT
  Revenue       : ₹[X] Cr  |  YoY: [+/-X%]  |  vs Estimate: [+/-X%]
  EBITDA        : ₹[X] Cr  |  Margin: [X%]   |  vs Last Qtr: [+/-Xpp]
  PAT           : ₹[X] Cr  |  YoY: [+/-X%]  |  vs Estimate: [+/-X%]
  CFO (QoQ)     : ₹[X] Cr  |  CFO/PAT: [X%]
──────────────────────────────────────────────────────────────────────────
SURVEILLANCE CHECKLIST
  □ Revenue growth on track vs thesis?           [YES / NO — expected X%, actual X%]
  □ EBITDA margin stable/expanding?              [YES / NO — Xpp change]
  □ CFO/PAT > 80%?                               [YES (X%) / NO (X%)]
  □ Governance events this quarter?              [NONE / FLAG: describe]
  □ Management tone on concall?                  [Confident / Cautious / Defensive]
  □ Order book / pipeline growing?               [YES / NO / NA]
  □ Promoter shareholding change?                [+/-Xpp | New pledging?]
  □ Working capital (DSO/Inventory) stable?      [YES / FLAG: DSO +X%]
  □ Analyst estimate revisions?                  [Up X% / Flat / Down X%]
  □ Valuation percentile (post-result)?          [Xth — Hold/Buy/Trim zone]
  □ Thesis intact?                               [INTACT / PARTIALLY IMPAIRED / BROKEN]
──────────────────────────────────────────────────────────────────────────
SCORE   : [X/11 green]
ACTION  : [HOLD / ADD Tranche X / TRIM / EXIT]                         [REQUIRED]

ACTION RATIONALE:
  [2–3 lines explaining why the action is recommended]

[IF EXIT or TRIM]
EXIT EXECUTION NOTE:
  Tax status     : [LTCG eligible (held > 12m) / STCG applies]
  Sell price     : ₹[X]  (current / limit order)
  Estimated gain/loss: [+/-X% from cost]

NEXT REVIEW DATE: [Next quarterly result or 90 days]
──────────────────────────────────────────────────────────────────────────
```

---

## Template 7: PORTFOLIO_SCAN_SUMMARY

Use when: Running a batch scan of multiple stocks (e.g., monthly portfolio review or sector scan).

```
══════════════════════════════════════════════════════════════════════════
PORTFOLIO / SECTOR SCAN SUMMARY
══════════════════════════════════════════════════════════════════════════
Scan Date         : [YYYY-MM-DD]
Scan Type         : [Monthly Portfolio Review / Sector Scan / Correction Scan]
Nifty Mode        : [Normal / Correction X%]
Stocks Scanned    : [N]
──────────────────────────────────────────────────────────────────────────
SCAN RESULTS
  Stock    Tier  Action         Pre-Screen  Governance  Valuation    Flag
  [Name]   [1]   [HOLD]         [X/9]       [X/15]      [Xth pctile] [—]
  [Name]   [1]   [ADD T2]       [X/9]       [X/15]      [Xth pctile] [CORRECTION ENTRY]
  [Name]   [2]   [WATCH]        [X/9]       [X/15]      [—]          [AWAIT BUY ZONE]
  [Name]   [—]   [EXIT]         [X/9]       [X/15]      [—]          [THESIS BROKEN]
  [Name]   [—]   [REJECT]       [X/9]       [X/15]      [—]          [STEP 1 FAIL]
──────────────────────────────────────────────────────────────────────────
SUMMARY
  Buys / Adds         : [N stocks]
  Holds               : [N stocks]
  Trims / Exits       : [N stocks]
  Rejections          : [N stocks]
  Watchlist Additions : [N stocks]

KEY THEMES IDENTIFIED:
  [2–3 lines on patterns observed — e.g., "Defence sector broadly in buy zone.
  Private banks showing correction entry opportunity. IT valuations still elevated."]

PORTFOLIO HEALTH
  Total portfolio conviction score: [X/10]
  Stocks with deteriorating thesis: [N]
  Highest conviction adds this cycle: [Name 1, Name 2]
  Stocks approaching exit zone (valuation): [Name 1, Name 2]

NEXT SCHEDULED SCAN: [Date — typically 30 days or next batch of results]
══════════════════════════════════════════════════════════════════════════
```

---

## Output Quality Rules (Apply to All Templates)

1. **Never truncate required fields.** If a data point is unavailable, write `[NOT AVAILABLE]`.
2. **Never round aggressively.** P/E = 24.3x not "~24x"; CAGR = 18.4% not "~18%".
3. **Label all estimates.** Any DCF output, forward earnings assumption, or projected figure must carry `[ESTIMATE]`.
4. **Label all overrides.** Any sector exception must carry `[SECTOR OVERRIDE: reason]`.
5. **Data flags section is mandatory.** Even if clean, write "All data verified. No flags."
6. **Date and time in IST.** Always include timezone for price data.
7. **Tranche prices must be specific.** "₹2,340 (8% below CMP)" is acceptable. "Around CMP" is not.
