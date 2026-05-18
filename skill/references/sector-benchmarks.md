# Sector-Specific Financial Benchmarks
# Companion file for SKILL.md — indian-equity-long-term-investor
# Read the relevant sector subsection at Step 3 before applying financial thresholds.

## How Sector Overrides Work (Code)

Sector classification and threshold overrides are now **enforced programmatically** in the pipeline. The classifier (`src/sector/classifier.py`) assigns one of seven sector names to `state.sector_name` before Step 0 runs. Each sector name maps to a `SectorProfile` dataclass (`src/sector/profiles.py`) that holds per-metric threshold overrides — `None` means the check is waived entirely.

**Code sectors vs. this reference file:**

| Code profile | Maps to benchmarks section |
|--------------|---------------------------|
| `financial_services` | Private Banks / NBFCs / Insurance (§1–3) |
| `capital_goods` | Capital Goods & Engineering (§7) |
| `commodities_cyclical` | Specialty Chemicals / Metals (§8, §11) |
| `infrastructure_utility` | Renewables & Infrastructure (§10) |
| `recently_listed` | 5Y metrics waived — all sectors |
| `defence_govt` | Capital Goods §7 with relaxed CFO/NP |
| `default` | FMCG / IT / Pharma / Hospitals (§4–6, §9) |

This file contains **qualitative benchmarks** (sector-specific moat checks, ratios not in the pipeline like NIM, GNPA, ARPOB). Use it for the Step 2 moat assessment and Step 3 soft quality checks where these metrics matter.

## Override Rule
When a sector-specific threshold differs from the default in SKILL.md Step 3,
the sector-specific threshold **takes precedence**. Always label the override:
`[SECTOR OVERRIDE: reason]`

---

## 1. Private Banks

| Metric | Minimum | Preferred | Flag if |
|--------|---------|-----------|---------|
| ROA (5Y avg) | ≥ 1.2% | ≥ 1.8% | < 1.0% for 2+ years |
| ROE (5Y avg) | ≥ 12% | ≥ 16% | < 10% |
| NIM (Net Interest Margin) | ≥ 3.2% | ≥ 4.0% | Declining > 50bps |
| GNPA (Gross NPA %) | < 3.0% | < 1.5% | Rising trend 2+ years |
| NNPA (Net NPA %) | < 1.0% | < 0.5% | > 2.0% |
| CAR (Capital Adequacy) | ≥ 15% | ≥ 17% | < 13% |
| PCR (Provision Coverage Ratio) | ≥ 65% | ≥ 75% | < 60% |
| Credit Cost | < 1.5% | < 0.8% | Rising > 2.0% |
| CASA Ratio | ≥ 35% | ≥ 45% | < 30% (funding cost risk) |

**D/E**: Not applicable for banks. Use CAR and Leverage Ratio instead.
**CFO/NP**: Not applicable in the traditional sense. Use Operating Profit / NII trend.
**Primary moat check**: Franchise strength, CASA franchise, digital capability, asset quality discipline.
**Key risk**: Credit cycle turn, NPA cycle, RBI regulatory action.

---

## 2. NBFCs (Non-Banking Financial Companies)

| Metric | Minimum | Preferred | Flag if |
|--------|---------|-----------|---------|
| ROA (5Y avg) | ≥ 2.0% | ≥ 3.0% | < 1.5% |
| ROE (5Y avg) | ≥ 14% | ≥ 18% | < 12% |
| NIM | ≥ 5.0% | ≥ 7.0% | < 4.0% |
| GNPA | < 4.0% | < 2.0% | Rising 2+ years |
| Debt/Equity | < 5.0x | < 3.5x | > 7.0x (systemic risk) |
| ALM (Asset-Liability Match) | < 3 months gap | < 1 month | Significant mismatch |
| CoF (Cost of Funds) | Stable or declining | Declining | Rising > 100bps |

**Key risk**: ALM mismatch, credit cost surprise, RBI licensing/regulatory change, CoF spike.
**Extra check**: What % of borrowing is from capital markets vs. banks? Heavy capital market dependency = liquidity risk.

---

## 3. Insurance Companies

| Metric | Minimum | Preferred | Flag if |
|--------|---------|-----------|---------|
| Combined Ratio (General Insurance) | < 105% | < 95% | > 110% for 2+ years |
| VNB Margin (Life Insurance) | ≥ 15% | ≥ 25% | Declining trend |
| Embedded Value Growth | ≥ 12% | ≥ 18% | < 8% |
| Solvency Ratio | ≥ 150% | ≥ 200% | < 130% (regulatory minimum is 150%) |
| New Business Premium Growth | ≥ 12% | ≥ 20% | < 5% for 2+ years |

**Primary valuation metric**: P/EV (Price to Embedded Value) for life insurers; P/B for general.
**ROE/ROCE**: Not directly applicable. Use VNB margin and EV growth as proxies.

---

## 4. FMCG & Consumer Staples

| Metric | Minimum | Preferred | Flag if |
|--------|---------|-----------|---------|
| ROE (5Y avg) | ≥ 22% | ≥ 35% | Declining > 5pp in 3Y |
| ROCE (5Y avg) | ≥ 25% | ≥ 40% | — |
| EBITDA Margin | ≥ 15% | ≥ 22% | < 12% sustained |
| Volume Growth | ≥ 5% | ≥ 10% | < 2% for 2+ years (market share loss signal) |
| D/E | < 0.3 | Net cash | > 0.5 (unusual for quality FMCG) |
| CFO/NP (3Y avg) | ≥ 90% | ≥ 95% | < 80% (red flag for FMCG) |

**Key distinction**: Separate volume growth from price-led growth. Sustained volume growth = moat strengthening.
**Key risk**: Rural demand slowdown, raw material cost inflation, new-age D2C disruption.

---

## 5. IT Services & Software

| Metric | Minimum | Preferred | Flag if |
|--------|---------|-----------|---------|
| Revenue Growth (USD) | ≥ 8% | ≥ 15% | < 5% for 2+ years (structural concern) |
| EBIT Margin | ≥ 15% | ≥ 20% | < 12% (pricing pressure) |
| ROE (5Y avg) | ≥ 22% | ≥ 30% | — |
| Attrition Rate | < 18% | < 12% | > 25% (talent cost risk) |
| D/E | Net cash | Net cash | Any significant debt (unusual) |
| CFO/NP (3Y avg) | ≥ 85% | ≥ 95% | < 75% |

**Revenue**: Use USD revenue growth (INR growth is partly FX-driven and misleading).
**Valuation**: Use EV/EBIT or P/E (not EV/EBITDA — D&A is minimal for IT).
**Key risk**: USD/INR appreciation, pricing pressure, AI disruption of labour-intensive services, deal concentration.

---

## 6. Pharmaceuticals — Formulations (Domestic + Exports)

| Metric | Minimum | Preferred | Flag if |
|--------|---------|-----------|---------|
| Revenue CAGR (5Y) | ≥ 12% | ≥ 18% | — |
| EBITDA Margin | ≥ 18% | ≥ 25% | < 14% sustained |
| ROE (5Y avg) | ≥ 16% | ≥ 22% | — |
| ROCE (5Y avg) | ≥ 18% | ≥ 24% | — |
| R&D/Sales | ≥ 5% | ≥ 8% | < 4% (moat erosion risk) |
| D/E | < 0.5 | Net cash | > 1.0 |
| US ANDA pipeline | ≥ 30 pending | ≥ 60 | < 15 (limited US optionality) |

**Key risk**: US FDA import alert or warning letter, USFDA pricing erosion for generics, API supply chain disruption.
**CRAMS/CDMO sub-sector**: Prefer long-term contracts (3–5Y) with multi-national customers. Check customer concentration.

---

## 7. Capital Goods & Engineering

| Metric | Minimum | Preferred | Flag if |
|--------|---------|-----------|---------|
| Order Book / TTM Revenue | ≥ 2.0x | ≥ 3.0x | < 1.5x (near-term growth at risk) |
| Revenue CAGR (5Y) | ≥ 12% | ≥ 18% | — |
| EBITDA Margin | ≥ 10% | ≥ 14% | Compressing > 2pp with no explanation |
| ROCE (5Y avg) | ≥ 16% | ≥ 22% | < 12% |
| D/E | < 0.75 | < 0.4 | > 1.5 |
| Working Capital Days | < 120 | < 90 | Rising > 20% YoY sustained |
| CFO/NP (3Y avg) | ≥ 70% | ≥ 85% | < 60% (working capital trap) |

**Key metric**: Order book visibility is the primary forward-looking indicator.
**Key risk**: Execution delays, commodity input cost spikes, government budget reallocation.

---

## 8. Specialty Chemicals

| Metric | Minimum | Preferred | Flag if |
|--------|---------|-----------|---------|
| Revenue CAGR (5Y) | ≥ 15% | ≥ 22% | — |
| EBITDA Margin | ≥ 16% | ≥ 22% | < 14% sustained |
| ROCE (5Y avg) | ≥ 18% | ≥ 25% | — |
| D/E | < 0.5 | Net cash | > 1.0 |
| Customer Concentration | < 30% from top customer | < 20% | > 40% (severe) |
| R&D/Sales | ≥ 3% | ≥ 5% | — |

**Key risk**: Chinese oversupply and dumping, raw material price volatility, single large customer exit.
**Check**: Multi-year supply contracts with global innovators → strong signal.

---

## 9. Hospitals & Healthcare Services

| Metric | Minimum | Preferred | Flag if |
|--------|---------|-----------|---------|
| ARPOB Growth (per bed) | ≥ 8% | ≥ 12% | Declining (volume or payor mix issue) |
| Occupancy Rate | ≥ 60% | ≥ 70% | < 55% for 2+ years |
| EBITDA Margin (Mature) | ≥ 18% | ≥ 24% | < 15% |
| ROCE (5Y avg) | ≥ 10% | ≥ 14% | < 8% |
| D/E | < 1.5 | < 0.8 | > 2.5 |
| Beds Ramping | Brownfield > Greenfield | — | > 70% greenfield (long payback) |

**New hospital ramp-up**: Expect ROCE drag for 3–5 years on new beds. Adjust ROCE for mature bed base.
**Key risk**: NHMA pricing controls, regulatory burden, doctor attrition, payor mix shift to government schemes.

---

## 10. Renewables & Infrastructure

| Metric | Minimum | Preferred | Flag if |
|--------|---------|-----------|---------|
| ROCE (Project level) | ≥ 10% | ≥ 13% | < 8% |
| D/E | < 3.0 | < 2.0 | > 4.0 |
| PPA Coverage | ≥ 75% of capacity | ≥ 90% | < 60% (merchant risk) |
| PLF / CUF | ≥ 22% (solar) / ≥ 28% (wind) | — | Declining 2+ years |
| DSCR | ≥ 1.3x | ≥ 1.6x | < 1.1x (refinancing risk) |
| Revenue Visibility | ≥ 15Y PPA | — | < 10Y |

**D/E exception**: Up to 3.0x is acceptable if debt is project-level, non-recourse, and fully matched to contracted revenue.
**Key risk**: Tariff revision by DISCOM, land acquisition delays, curtailment risk, interest rate sensitivity on refinancing.

---

## 11. EMS / Electronics Manufacturing Services

| Metric | Minimum | Preferred | Flag if |
|--------|---------|-----------|---------|
| Revenue CAGR (5Y) | ≥ 20% | ≥ 35% | — |
| EBITDA Margin | ≥ 4% | ≥ 7% | < 3% (structural thin margin risk) |
| ROCE (5Y avg) | ≥ 16% | ≥ 22% | — |
| D/E | < 0.5 | Net cash | > 1.0 |
| Customer Concentration | < 40% top customer | < 25% | > 60% (existential concentration) |
| Working Capital Days | < 60 | < 45 | Rising > 20% |

**Note**: EMS is inherently thin-margin. Evaluate ROCE (not margin alone) as the primary quality metric.
**Key risk**: Customer program cancellation, geopolitical disruption, thin margin amplifies any volume decline.

---

## Sector Classification Quick Reference

| Company Type | Use Sector | Key Override |
|-------------|-----------|-------------|
| Private bank (savings deposits) | Private Banks | ROA-based; CAR instead of D/E |
| Gold finance / microfinance | NBFCs | Higher GNPA tolerance; ALM check |
| Life insurer | Insurance | P/EV valuation; VNB margin |
| General insurer | Insurance | Combined Ratio primary |
| Listed pharma with US generics | Pharma — Formulations | ANDA pipeline check |
| CDMO / CRAMS pharma | Pharma — CRAMS | Contract tenure check |
| Defence PSU or private | Capital Goods | Order book 3x+ preferred |
| Solar / wind developer | Renewables | DSCR, PPA coverage |
| EV component maker | Capital Goods | EV platform diversification check |
| Branded apparel / food | FMCG | Volume growth primary |
| IT product company | IT Services | Modified — check ARR growth, NRR |
