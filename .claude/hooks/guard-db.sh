#!/usr/bin/env bash
# Hook 3 — PreToolUse (Bash)
# Blocks any Bash command that combines investor.db with a destructive keyword.
# investor.db is the single source of truth — accidental truncation is unrecoverable.
# Exit 2 blocks the tool and surfaces the message to Claude / the user.
set -euo pipefail

INPUT=$(cat)
CMD=$(echo "$INPUT" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d.get('tool_input', {}).get('command', ''))
" 2>/dev/null || echo "")

# Check for investor.db touched by a destructive operation
if echo "$CMD" | grep -q "investor\.db"; then
    if echo "$CMD" | grep -qiE '\b(DELETE|DROP TABLE|DROP INDEX|rm |truncate|unlink|shutil\.rmtree)\b'; then
        echo "BLOCKED: destructive operation on investor.db detected."
        echo ""
        echo "Command: $CMD"
        echo ""
        echo "investor.db holds all pipeline analyses, watchlist entries, and raw snapshots."
        echo "Run this command in your terminal manually if you are certain."
        exit 2
    fi
fi
