# Data Sources Reference
# Companion file for SKILL.md — indian-equity-long-term-investor
# Read this file at the start of every analysis session.

## Fetch Priority Rule
NSE/BSE official filings > Screener.in > Trendlyne > Broker reports.
Never use social media, forums, or unverified blog data for financial metrics.

---

## Data Type → Source Mapping

### 1. Real-Time Stock Price & Market Data
| Data | Source | URL / Method |
|------|--------|-------------|
| CMP, 52W High, 52W Low | NSE India | https://www.nseindia.com/get-quotes/equity?symbol=TICKER |
| Nifty 50 level & 52W High | NSE Index | https://www.nseindia.com/market-data/live-market-indices |
| Historical price chart (for 200-DMA, RSI) | NSE or Trendlyne | web_search: "TICKER 200 DMA RSI site:trendlyne.com" |
| Volume data | NSE | NSE equity quote page |

**Freshness requirement**: Price data must be < 24 hours old.
**If market is closed**: Use the most recent closing price and note `[LAST CLOSE: date]`.

---

### 2. Financial Statements (P&L, Balance Sheet, Cash Flow)
| Data | Source | URL / Method |
|------|--------|-------------|
| Revenue, PAT, EBITDA (annual, 5–10Y) | Screener.in | https://www.screener.in/company/TICKER/consolidated/ |
| Quarterly results | NSE corporate filings | https://www.nseindia.com/companies-listing/corporate-filings-financial-results |
| Cash Flow Statement (CFO, CFI, CFF) | Screener.in | Same as above — "Cash Flows" section |
| Balance Sheet (D/E, Current Ratio) | Screener.in | Same as above — "Balance Sheet" section |
| XBRL structured data | BSE | https://www.bseindia.com/corporates/Comp_Resultsnew.aspx |

**Freshness requirement**: Financial statements must be < 90 days old (< 45 days after result announcement date).
**Fallback**: If Screener.in is unreachable, use Trendlyne: https://trendlyne.com/fundamentals/TICKER/

---

### 3. Shareholding & Promoter Data
| Data | Source | URL / Method |
|------|--------|-------------|
| Promoter holding % (current & 8Q trend) | BSE shareholding | https://www.bseindia.com/shareholding.html (search by ISIN) |
| Promoter pledging % | NSE/BSE filings | NSE: corporate filings → Insider Trading / Pledging section |
| FII/DII holding % | Screener.in or NSE | Screener: "Shareholders" section |
| Bulk & block deals | NSE | https://www.nseindia.com/market-data/bulk-deals |
| SAST filings (open market buys/sells) | BSE/NSE | web_search: "TICKER promoter SAST filing BSE" |

**Note**: Pull 8 quarters of pledging data to assess trend direction.
Rising pledging is more dangerous than a stable elevated number.

---

### 4. Governance & Regulatory
| Data | Source | URL / Method |
|------|--------|-------------|
| SEBI orders against company | SEBI SCORES | https://scores.sebi.gov.in/ |
| SEBI enforcement actions | SEBI website | https://www.sebi.gov.in/enforcement/orders.html |
| Auditor details & changes | Annual Report (Notes) | Company IR page or BSE/NSE annual report filings |
| RPT disclosures | Annual Report | Notes to Accounts — Related Party section |
| Contingent liabilities | Annual Report | Notes to Accounts — Contingent Liabilities section |

**Note**: For auditor change check, look at the last 3 Annual Reports.
An unexpected mid-year auditor resignation is a severe red flag.

---

### 5. Valuation & Peer Data
| Data | Source | URL / Method |
|------|--------|-------------|
| Current P/E, EV/EBITDA, P/BV | Screener.in | Company page → "Ratios" section |
| 10-year historical P/E range | Trendlyne | https://trendlyne.com/valuation/TICKER/ |
| Peer comparison table | Screener.in | Company page → "Peers" section |
| Consensus forward EPS estimates | Trendlyne | "Estimates" tab on company page |
| FCF calculation | Screener.in | CFO - Capex (from Cash Flow section) |

**If 10Y data unavailable**: Use 5Y range and flag `[5Y PERCENTILE — 10Y DATA UNAVAILABLE]`.
Apply additional 5% MoS discount.

---

### 6. Industry & Macro Data
| Data | Source | URL / Method |
|------|--------|-------------|
| RBI repo rate, policy stance | RBI | https://www.rbi.org.in/scripts/BS_PressReleaseDisplay.aspx |
| CPI inflation | MOSPI | https://www.mospi.gov.in/ |
| GST collections | Ministry of Finance | web_search: "GST collection monthly 2026" |
| IIP (Industrial Production) | MOSPI | web_search: "IIP data latest MOSPI" |
| FII/DII net flows | NSDL | https://www.fpi.nsdl.co.in/web/Reports/Yearwise.aspx |
| 10-year G-sec yield | RBI / NSE | web_search: "India 10 year gsec yield today" |
| USD/INR rate | NSE currency | web_search: "USD INR rate today NSE" |

---

### 7. Concall Transcripts & Management Communication
| Data | Source | URL / Method |
|------|--------|-------------|
| Concall transcripts | Trendlyne / IndiaNotes | web_search: "TICKER Q[X] concall transcript 2026" |
| Investor presentations | Company IR page | web_search: "TICKER investor presentation 2026 site:[company].com" |
| AGM proceedings | BSE/NSE | Corporate filings section |
| Annual reports | Company IR or BSE | web_search: "TICKER annual report 2025 PDF" |

---

## Fetch Sequence for a Full Analysis

Run fetches in this order to minimise redundant calls:

```
1. NSE: CMP, 52W High/Low, Nifty level            [Mode detection + price context]
2. Screener.in: Full company page                   [Financial ratios, peers, cash flows]
3. BSE/NSE: Latest shareholding pattern (8Q)        [Promoter data, pledging trend]
4. Trendlyne: 10Y P/E range, concall transcript     [Valuation percentile, mgmt tone]
5. BSE Annual Report: Auditor, RPT, contingencies   [Governance verification]
6. SEBI SCORES: Search for company name             [Regulatory check]
7. web_search: "[TICKER] peer comparison sector"    [Peer list confirmation]
```

---

## Rate Limiting & Retry Rules

| Source | Rate Limit | Retry Strategy |
|--------|-----------|----------------|
| Screener.in | ~20 req/min | Wait 60s if blocked; then retry once |
| NSE India | Moderate | Retry after 30s; use BSE as fallback |
| Trendlyne | Moderate | Retry after 30s |
| SEBI | Low traffic | Direct fetch; no retry needed |

If all primary sources fail for a metric: label `[NOT AVAILABLE]` and proceed.
Never substitute with estimated or recalled training data for financial metrics.
