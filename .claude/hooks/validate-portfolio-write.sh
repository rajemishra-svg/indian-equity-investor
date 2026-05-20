#!/usr/bin/env bash
# Hook 4 — PreToolUse (Write)
# Validates that any write to portfolio/*.md preserves a consistent markdown
# table structure. get_holdings() is sensitive to column count mismatches —
# a bad row silently drops entries from `investor portfolio` and portfolio-review.
# Exit 2 blocks the write and surfaces the error.
set -euo pipefail

INPUT=$(cat)
echo "$INPUT" | python3 -c "
import sys, json, re

try:
    d = json.loads(sys.stdin.read())
except json.JSONDecodeError:
    sys.exit(0)

ti = d.get('tool_input', {})
file_path = ti.get('file_path', '')
content = ti.get('content', '')

if not re.search(r'portfolio/.+\.md$', file_path):
    sys.exit(0)

if not content.strip():
    sys.exit(0)

lines = content.splitlines()
table_lines = [l.strip() for l in lines if l.strip().startswith('|')]
data_rows = [l for l in table_lines if not all(c in '|-: ' for c in l)]

if len(data_rows) < 2:
    sys.exit(0)

header_cols = data_rows[0].count('|')
errors = []
for i, row in enumerate(data_rows[1:], start=2):
    cols = row.count('|')
    if cols != header_cols:
        errors.append(f'  Row {i}: expected {header_cols} pipes, got {cols}: {row[:100]}')

if errors:
    print(f'BLOCKED: malformed markdown table in {file_path}')
    for e in errors:
        print(e)
    print()
    print('Fix column count before writing — get_holdings() silently drops mismatched rows.')
    sys.exit(2)
"
