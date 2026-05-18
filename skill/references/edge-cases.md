# Edge Case Taxonomy & Handling
# Companion file for SKILL.md — indian-equity-long-term-investor
# Read the relevant section when an edge case flag is triggered.

## Edge Case Index

| ID | Edge Case | Most Likely Step Triggered |
|----|-----------|--------------------------|
| EC-01 | Pre-profit / loss-making company | Step 0, Step 3 |
| EC-02 | Cyclical business | Step 3, Step 4, Step 5 |
| EC-03 | Financial services (bank/NBFC/insurer) | Step 3 |
| EC-04 | Conglomerate / multi-segment | Step 2, Step 5 |
| EC-05 | Recently listed (< 5Y of financials) | Step 0, Step 3 |
| EC-06 | Low promoter holding (MNC / widely held) | Step 0, Step 1 |
| EC-07 | Turnaround story (recovering from loss cycle) | Step 3, Step 5 |
| EC-08 | Stock in deep correction (> 20% from 52W H) | Step 5, Step 6 |
| EC-09 | Peer dominance detected | Step 7 |
| EC-10 | Government / PSU company | Step 1, Step 2 |
| EC-11 | Small cap (< ₹5,000 Cr market cap) | All steps |
| EC-12 | Global/export-oriented business | Step 3, Step 4 |
| EC-13 | M&A target or acquirer (post-acquisition) | Step 3, Step 5 |
| EC-14 | Regulatory overhang (pending SEBI/NCLT case) | Step 1 |
| EC-15 | Holding company / investment company | Step 2, Step 5 |

---

## EC-01: Pre-Profit / Loss-Making Company

**Trigger**: PAT < 0 for any of the last 3 years, OR PAT CAGR is not calculable.

**Modified criteria**:
- Drop: PAT CAGR, ROE, ROCE, CFO/NP thresholds (not applicable)
- Substitute metrics:
  - Revenue CAGR ≥ 30% (minimum; preferred ≥ 50%)
  - Gross Margin > 35% (minimum; preferred > 50% for SaaS/tech)
  - Path to PAT profitability within 3 years (confirmed in management guidance)
  - Unit economics must be positive (contribution margin > 0 per transaction/customer)
  - Burn rate: cash runway ≥ 18 months without further funding
- Valuation: Use P/S (Price/Sales) or EV/Gross Profit, not P/E or PEG
- Maximum allocation: 2–4% regardless of conviction
- Minimum MoS: 50% (higher uncertainty demands more cushion)

**Flag output with**: `[EDGE CASE EC-01: PRE-PROFIT — MODIFIED CRITERIA APPLIED]`

**Risk**: These are speculative positions. Premortem must include "what if funding dries up" and "what if path to profitability extends beyond 5 years."

---

## EC-02: Cyclical Business

**Trigger**: Sector is Metals, Commodities, Chemicals (commodity-grade), Shipping, or Sugar.
OR company revenue correlates > 70% with a single commodity price.

**Modified criteria**:
- Use 10-year through-the-cycle averages (not 5-year) for all growth and return metrics
- Use mid-cycle EBITDA margins for valuation (not peak-cycle)
- Classify tailwind as CYCLICAL regardless of management narrative
- Apply EV/EBITDA at mid-cycle as primary valuation metric (not P/E — earnings too volatile)
- Apply 10–15% additional discount to intrinsic value for cycle risk
- Cycle position assessment is mandatory: where is the commodity/sector in its cycle?
  - Early-cycle upcycle: favourable entry
  - Peak cycle: extreme caution; do not pay full multiple
  - Downcycle: monitor for entry on distress valuation
- Position size cap: 5% maximum regardless of conviction (concentration risk in cyclicals)

**Flag output with**: `[EDGE CASE EC-02: CYCLICAL — MID-CYCLE METRICS APPLIED]`

**Key check**: Does the company have strong enough balance sheet to survive the next downturn?
D/E must be < 0.5 and ICR > 8x at mid-cycle to handle a severe downturn.

---

## EC-03: Financial Services

**Trigger**: Company is classified as Bank, NBFC, Insurance, Asset Manager, or Brokerage.

**Modified criteria**:
- D/E threshold: not applicable. Use sector-specific metrics from sector-benchmarks.md.
- CFO/NP: not applicable. Omit from financial checklist.
- Working capital analysis: not applicable.
- Use sector-benchmarks.md for the specific sub-type (Private Banks, NBFCs, or Insurance).
- Primary qualitative check: Asset quality trend (GNPA/NNPA trajectory) is the equivalent
  of moat durability for financial companies.

**Flag output with**: `[EDGE CASE EC-03: FINANCIAL SERVICES — MODIFIED CRITERIA APPLIED]`

**Note**: The governance check at Step 1 is especially critical for financials.
Off-balance-sheet liabilities, evergreening of loans, and connected party lending
are common governance failures in this sector.

---

## EC-04: Conglomerate / Multi-Segment

**Trigger**: Company operates in ≥ 3 distinct business segments where no segment
accounts for > 60% of revenue.

**Modified approach**:
- Assess each segment separately: moat, growth rate, margin profile, competitive position
- Build Sum-of-the-Parts (SoTP) valuation:
  1. Value each segment using the appropriate valuation multiple for that segment's sector
  2. Sum segment values to get total enterprise value
  3. Apply holding company discount of 15–20% (standard for Indian conglomerates)
  4. Subtract net debt to get equity value
- Governance check must assess: are segments cross-subsidising each other?
  Are weaker segments diverting cash from stronger ones?
- Moat assessment: use the weakest segment moat as the governing concern for risk

**Flag output with**: `[EDGE CASE EC-04: CONGLOMERATE — SOTP VALUATION APPLIED]`

---

## EC-05: Recently Listed (< 5 Years of Public Financials)

**Trigger**: IPO < 5 years ago, so full 5-year financial history is unavailable.

**Modified criteria**:
- Use available years (state how many: "X years of data available")
- Apply 10% additional discount to intrinsic value for reduced data confidence
- Maximum allocation: 3% until 5 full years of listed company data are available
- Extra scrutiny on:
  - IPO prospectus financial history (promoter's representation before listing)
  - Use of IPO proceeds (were they used as stated?)
  - Post-IPO management track record vs. pre-IPO promises
- Flag any data gap explicitly in the financial snapshot

**Flag output with**: `[EDGE CASE EC-05: < 5Y DATA — X years available. Additional MoS applied.]`

---

## EC-06: Low Promoter Holding (MNC or Widely Held)

**Trigger**: Promoter / parent holding < 40%.

**Waiver condition**: Acceptable if:
- Company is a listed subsidiary of a reputable MNC (parent > 50% direct or indirect holding)
- OR company is professionally managed with strong institutional oversight
  (board independence, no controlling family, strong audit committee)

**Modified approach**:
- Verify: who effectively controls the company? Is control beneficent to minorities?
- For MNC subsidiaries: check parent's global governance standards and India track record
- For professionally managed companies: assess board quality, independent director engagement,
  and executive compensation alignment
- Assess risk of MNC parent delisting the subsidiary (often value-neutral or positive if at a premium)

**Flag output with**: `[EDGE CASE EC-06: LOW PROMOTER HOLDING — WAIVER: reason]`

---

## EC-07: Turnaround Story

**Trigger**: Company has recovered from 1–2 years of losses or significant margin compression,
and current metrics are improving but historical 5Y averages are dragged down.

**Modified approach**:
- Use the most recent 2–3 years as the primary basis for trend assessment (not the full 5Y average)
- Verify that the cause of the downturn was temporary (industry cycle, one-time event)
  and not structural (business model failure)
- Require 2+ consecutive quarters of improving key metrics before proceeding
- Apply a 10% additional MoS discount for the "is the turnaround real?" uncertainty
- Check if the management driving the turnaround is the same or new
  (new management turnaround = higher potential but also higher uncertainty)
- Cap allocation at 4% until 4 quarters of consistent improvement are confirmed

**Flag output with**: `[EDGE CASE EC-07: TURNAROUND — RECENT TREND PRIORITISED]`

---

## EC-08: Stock in Deep Correction (> 20% from 52W High)

**Trigger**: Current CMP is more than 20% below the 52-week high.

**Critical check first**: Is the correction due to:
- (A) Market-wide / sector-wide fall with no company-specific fundamental change → OPPORTUNITY
- (B) Company-specific fundamental deterioration → POTENTIAL VALUE TRAP, DO NOT BUY

**How to determine**:
1. Check if Nifty or sector index also fell significantly in the same period.
2. Check if there was a negative earnings surprise, governance event, or management change.
3. Re-read the last concall transcript and any news from the period of the fall.

**If Case A (opportunity)**:
- Re-run Step 5 with Correction Mode MoS thresholds (reduce required MoS by 5%)
- Consider simultaneous Tranche 1 + Tranche 2 entry (accelerated deployment)
- Flag: `[EDGE CASE EC-08: CORRECTION ENTRY — DUAL TRANCHE AUTHORISED IF THESIS INTACT]`

**If Case B (fundamental deterioration)**:
- Re-run Steps 1 and 3 immediately with updated data
- If fundamentals are broken → issue REJECTION_LOG, not a buy recommendation
- Flag: `[EDGE CASE EC-08: CORRECTION LIKELY FUNDAMENTAL — FULL RE-EVALUATION REQUIRED]`

---

## EC-09: Peer Dominance Detected

**Trigger**: At Step 7, a peer scores higher on both Quality Score AND Valuation Score.

**Action**:
1. Issue PEER_SWITCH_NOTICE immediately (use template from output-templates.md)
2. Pause the original analysis
3. Restart from Step 0 with the dominant peer
4. If the dominant peer also passes all steps → recommend the peer
5. If the dominant peer fails any step → return to original analysis
6. Document the comparison in both the Peer Switch notice and the final output

**Note**: If two peers dominate simultaneously, analyse the one with the higher Quality Score first.

---

## EC-10: Government / PSU Company

**Trigger**: Government of India or state government owns > 50% of the company.

**Modified governance assessment**:
- Promoter pledging: not applicable (government cannot pledge)
- Governance score adjusted: remove pledging sub-factor; redistribute its points (increase
  capital allocation score weight to 4 instead of 3)
- Apply additional scrutiny on:
  - Government interference in business decisions (pricing, staffing, social mandates)
  - Dividend payout discipline vs. capital retention for growth
  - Bureaucratic efficiency: management continuity and decision speed
  - Disinvestment risk (government selling stake can dilute and signal disinterest)

**Moat caveat**: Many PSUs have regulatory moats but may not have incentive to
capitalise on them efficiently. Document this explicitly.

**Valuation**: PSUs typically trade at structural discounts due to governance concerns.
Adjust peer comparison: compare to both listed PSU peers and private sector peers.

**Flag output with**: `[EDGE CASE EC-10: PSU — MODIFIED GOVERNANCE CRITERIA. GOVERNMENT INTERFERENCE RISK DOCUMENTED]`

---

## EC-11: Small Cap (Market Cap < ₹5,000 Cr)

**Trigger**: Market Cap < ₹5,000 Cr.

**Modified criteria across all steps**:
- Minimum MoS: 40–50% (vs. 25–30% for large caps)
- Maximum allocation: 4–6% per position
- Liquidity check mandatory: Average daily trading volume > ₹2 Cr (to ensure exit is possible)
- Governance scrutiny: Extra weight — small caps have historically higher governance failure rate
- Auditor quality: Big 4 preference becomes a stronger positive signal
- CFO/NP threshold: ≥ 85% (higher than standard — small caps more prone to earnings manipulation)
- Add stop-loss in the exit plan: 20–25% below cost price (small caps can fall faster)
- Promoter holding: prefer ≥ 50% (higher alignment needed given lower institutional oversight)

**Flag output with**: `[EDGE CASE EC-11: SMALL CAP — ENHANCED SCRUTINY. LIQUIDITY VERIFIED.]`

---

## EC-12: Export-Oriented / Global Revenue Business

**Trigger**: > 40% of revenue is from outside India (USD, EUR, or other foreign currency).

**Modified considerations**:
- Revenue must be assessed in USD or relevant currency terms (not just INR — FX inflates INR growth)
- Currency risk: Does the company hedge? What % of revenue is naturally hedged?
- Geopolitical exposure: US FDA (pharma), US tariffs (IT services, EMS), EU regulations
- Apply FX-adjusted growth rates in DCF
- Assess USD/INR sensitivity: every 5% INR depreciation = X% margin impact (calculate explicitly)

**Flag output with**: `[EDGE CASE EC-12: EXPORT-ORIENTED — FX-ADJUSTED METRICS USED]`

---

## EC-13: Post-Acquisition (Recent M&A)

**Trigger**: Company has completed a significant acquisition (> 15% of revenue) in the last 2 years.

**Modified approach**:
- Restate 5Y financials excluding the acquired entity to understand the organic business quality
- Assess acquisition price paid: was it value-accretive (acquired below business's fair value)?
- Check integration track record if management has done prior acquisitions
- Goodwill/Intangibles: if > 20% of Net Worth, assess impairment risk explicitly
- Apply 10% additional MoS until 2 full years of post-acquisition financial integration
  data are available
- Premortem must include: "acquisition fails to integrate / destroys value"

**Flag output with**: `[EDGE CASE EC-13: POST-ACQUISITION — ORGANIC VS INORGANIC SPLIT DOCUMENTED]`

---

## EC-14: Regulatory Overhang (Pending SEBI / NCLT / Tax Case)

**Trigger**: There is an active regulatory proceeding involving the company or promoter.

**Severity tiers**:

| Severity | Description | Action |
|----------|-------------|--------|
| Minor | Tax dispute < 5% of Net Worth; no fraud allegation | Note in risks; proceed with caution |
| Moderate | SEBI technical violation; no fraud; < 2 years pending | Reduce allocation by 50%; note prominently |
| Major | SEBI financial fraud investigation; NCLT/insolvency proceedings | REJECT immediately |
| Ongoing Fraud | ED, CBI involvement; allegations of siphoning | REJECT immediately; blacklist from future scans |

**Flag output with**: `[EDGE CASE EC-14: REGULATORY OVERHANG — SEVERITY: [Minor/Moderate/Major]]`

---

## EC-15: Holding Company / Investment Company

**Trigger**: The listed entity's primary asset is a shareholding in other companies,
not an operating business (e.g., Bajaj Holdings, Tata Investment Corporation).

**Modified approach**:
- Standard financial metrics (Revenue CAGR, EBITDA Margin) are not meaningful — do not apply
- Primary valuation: Discount to Net Asset Value (NAV) or Discount to Intrinsic Portfolio Value
  - Calculate NAV: sum of market value of listed holdings + estimated value of unlisted holdings
  - Historical discount range: Indian holding companies typically trade at 20–50% discount to NAV
  - Buy when: discount ≥ 40% to NAV (or ≥ 10% wider than the historical average discount)
- Key risk: Discount may persist indefinitely (the "holding company discount trap")
- Dividend yield from the portfolio is a secondary valuation support check

**Flag output with**: `[EDGE CASE EC-15: HOLDING COMPANY — NAV-BASED VALUATION APPLIED]`

---

## Edge Case Combination Rules

When multiple edge cases apply simultaneously:

| Combination | Rule |
|------------|------|
| EC-01 (pre-profit) + EC-11 (small cap) | Maximum 2% allocation; 50% MoS; extra liquidity check |
| EC-02 (cyclical) + EC-07 (turnaround) | Treat as highest-risk category; 3% max allocation; require 2Y of improvement data |
| EC-10 (PSU) + EC-07 (turnaround) | Only consider if new management has been in place ≥ 2 years with verified improvement |
| EC-03 (financial services) + EC-14 (regulatory overhang) | Severity "Moderate" or above = REJECT (financial + regulatory risk is too binary) |
| EC-04 (conglomerate) + EC-01 (pre-profit segment) | Only value profitable segments; explicitly exclude loss-making segments from SoTP |

**General rule**: When 3 or more edge cases apply simultaneously, cap allocation at 3%
regardless of conviction level, and require all hard gates to be met with no CONDITIONAL passes.
