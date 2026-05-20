#!/usr/bin/env bash
# Hook 5 — PostToolUse (Bash)
# After `investor add-trade` runs successfully, prints a compact portfolio
# summary so the trade can be confirmed at a glance without a separate command.
set -euo pipefail

INPUT=$(cat)
CMD=$(echo "$INPUT" | python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
print(d.get('tool_input', {}).get('command', ''))
" 2>/dev/null || echo "")

echo "$CMD" | grep -q "investor add-trade" || exit 0

HOLDINGS="portfolio/holdings.md"
[[ -f "$HOLDINGS" ]] || exit 0

python3 -c "
import sys

path = sys.argv[1]
lines = open(path).read().splitlines()

rows = []
for line in lines:
    line = line.strip()
    if not line.startswith('|') or '---' in line or 'Ticker' in line:
        continue
    cells = [c.strip() for c in line.strip('|').split('|')]
    if len(cells) >= 6:
        try:
            rows.append({
                'ticker': cells[0],
                'company': cells[1],
                'avg_cost': cells[2],
                'qty': cells[3],
                'date': cells[4],
                'alloc': float(cells[5].replace('%', '').strip()),
            })
        except (ValueError, IndexError):
            pass

if not rows:
    sys.exit(0)

total = sum(r['alloc'] for r in rows)
print()
print('  Portfolio after trade:')
print(f\"  {'Ticker':<14} {'Company':<24} {'Cost':>10} {'Qty':>6} {'Date':<12} {'Alloc':>6}\")
print(f\"  {'-'*14} {'-'*24} {'-'*10} {'-'*6} {'-'*12} {'-'*6}\")
for r in rows:
    print(f\"  {r['ticker']:<14} {r['company'][:24]:<24} {r['avg_cost']:>10} {r['qty']:>6} {r['date']:<12} {r['alloc']:>5.1f}%\")
print(f\"  {'':>72} {total:>5.1f}%  <- total\")
print()
" "$HOLDINGS"
