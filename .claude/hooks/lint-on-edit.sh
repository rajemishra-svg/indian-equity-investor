#!/usr/bin/env bash
# Hook 1 — PostToolUse (Edit|Write)
# Runs ruff on any edited Python file. Auto-fixes safe issues; exits non-zero
# on remaining violations so Claude sees them and can address them.
set -euo pipefail

INPUT=$(cat)
FILE=$(echo "$INPUT" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d.get('tool_input', {}).get('file_path', ''))
" 2>/dev/null || echo "")

# Only act on Python files that exist on disk
[[ "$FILE" == *.py ]] || exit 0
[[ -f "$FILE" ]]       || exit 0

echo "→ ruff check $FILE"
uv run ruff check "$FILE" --fix 2>&1
