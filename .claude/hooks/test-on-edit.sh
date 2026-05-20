#!/usr/bin/env bash
# Hook 2 — PostToolUse (Edit)
# Maps the edited src/ file to its corresponding test file and runs it.
# Exits non-zero on test failure so Claude sees the output and can fix it.
set -euo pipefail

INPUT=$(cat)
FILE=$(echo "$INPUT" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d.get('tool_input', {}).get('file_path', ''))
" 2>/dev/null || echo "")

# Only trigger on src/ Python files
[[ "$FILE" == src/*.py || "$FILE" == src/*/*.py || "$FILE" == src/*/*/*.py ]] || exit 0
[[ -f "$FILE" ]] || exit 0

BASENAME=$(basename "$FILE" .py)
TEST_FILE=""

# step files: step1_governance.py → tests/test_agent/test_step1.py
if [[ "$BASENAME" =~ ^step([0-9]+) ]]; then
    TEST_FILE="tests/test_agent/test_step${BASH_REMATCH[1]}.py"
# pipeline / batch scanner
elif [[ "$BASENAME" == "pipeline" || "$BASENAME" == "batch_scanner" ]]; then
    TEST_FILE="tests/test_agent/test_${BASENAME}.py"
# api clients: try exact name, then strip _client suffix
elif [[ "$FILE" == src/api/* ]]; then
    if [[ -f "tests/test_api/test_${BASENAME}.py" ]]; then
        TEST_FILE="tests/test_api/test_${BASENAME}.py"
    else
        SHORT="${BASENAME/_client/}"
        TEST_FILE="tests/test_api/test_${SHORT}.py"
    fi
# sector
elif [[ "$FILE" == src/sector/* ]]; then
    TEST_FILE=$(find tests/test_sector -name "test_${BASENAME}.py" 2>/dev/null | head -1)
# db
elif [[ "$FILE" == src/db/* ]]; then
    TEST_FILE="tests/test_db/test_${BASENAME}.py"
# portfolio
elif [[ "$FILE" == src/portfolio/* ]]; then
    TEST_FILE="tests/test_portfolio/test_${BASENAME}.py"
fi

[[ -n "$TEST_FILE" && -f "$TEST_FILE" ]] || exit 0

echo "→ pytest $TEST_FILE"
uv run pytest "$TEST_FILE" -q --tb=short 2>&1
