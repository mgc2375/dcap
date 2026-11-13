#!/bin/sh
# Runs one real Claude Code agent on an ordinary task, in a throwaway folder.
# It has no DCAP tool or instruction. DCAP only listens through project hooks.
# This spawns an autonomous agent that can use a shell, and it uses your Claude account.
# Usage: examples/live_agent/run.sh "task for the agent"
set -eu
DCAP="$(cd "$(dirname "$0")/../.." && pwd)"
WORK="$(mktemp -d)"
mkdir -p "$WORK/.claude"
printf '"""App."""\n' > "$WORK/main.py"
cat > "$WORK/hook.sh" <<EOF
#!/bin/sh
PYTHONPATH="$DCAP/src" exec python3 -m dcap.cli --definitions "$DCAP/examples/swagger" hook
EOF
chmod +x "$WORK/hook.sh"
cat > "$WORK/.claude/settings.json" <<EOF
{"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "$WORK/hook.sh"}]}],
           "PostToolUse": [{"hooks": [{"type": "command", "command": "$WORK/hook.sh"}]}],
           "Stop": [{"hooks": [{"type": "command", "command": "$WORK/hook.sh"}]}]}}
EOF
cd "$WORK"
claude -p "$1" --strict-mcp-config --setting-sources project --model sonnet --max-budget-usd 1 \
  --tools Bash Read Write Edit --permission-mode acceptEdits --no-session-persistence
echo "--- main.py"; cat main.py
echo "--- DCAP listener state"; cat .dcap/listener.json 2>/dev/null || echo "(nothing heard)"
echo "--- folder: $WORK"
