#!/usr/bin/env bash
# bb-hook-session-start.sh — SessionStart hook: register session + cleanup stale
# Only writes state.json, does not call MCP. MCP server auto-reloads from disk.
set -uo pipefail

INPUT=$(cat 2>/dev/null || echo "{}")

SESSION_ID=$(echo "$INPUT" | python -c "import json,sys; d=json.load(sys.stdin); print(d.get('session_id',''))" 2>/dev/null || echo "")
CWD=$(echo "$INPUT" | python -c "import json,sys; d=json.load(sys.stdin); print(d.get('cwd',''))" 2>/dev/null || echo "")

[ -z "$SESSION_ID" ] && exit 0

# Validate session_id format (must be UUID or simple identifier, no shell metacharacters)
if echo "$SESSION_ID" | grep -qE '[;&|`$(){}!]'; then
    echo "BLOCKED: Invalid session_id format" >&2
    exit 1
fi

# Discover state.json
SF=""
for d in "$CWD" "$(pwd)"; do
    if [ -f "$d/.claude/blackboard/state.json" ]; then
        SF="$d/.claude/blackboard/state.json"
        break
    fi
done
if [ -z "$SF" ]; then
    BB_DIR="$HOME/.claude/blackboard"
    mkdir -p "$BB_DIR"
    SF="$BB_DIR/state.json"
fi

BB_DIR=$(dirname "$SF")
EVENTS_LOG="$BB_DIR/events.log"
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# Initialize state.json if not exists (pass path as argument, not interpolated)
[ ! -f "$SF" ] && python - "$SF" "$TIMESTAMP" << 'PYINIT' > /dev/null 2>&1 || true
import json, os, sys
sf, ts = sys.argv[1:3]
os.makedirs(os.path.dirname(sf), exist_ok=True)
s = {"version":4,"last_updated":ts,"sessions":{},"file_registry":{},"build_locks":{},"knowledge":{},"decisions":[],"bug_patterns":[]}
with open(sf, "w", encoding="utf-8") as f:
    json.dump(s, f, indent=2, ensure_ascii=False)
PYINIT

# Register + cleanup stale (>15min no heartbeat) — pass all args, no interpolation
python - "$SF" "$SESSION_ID" "$TIMESTAMP" << 'PYEOF' > /dev/null 2>&1 || true
import json, sys, os, tempfile
from datetime import datetime, timezone, timedelta

state_file, session_id, ts = sys.argv[1:4]

# Validate inputs
if not session_id or not state_file:
    sys.exit(0)
# Sanitize: reject paths with directory traversal
if ".." in state_file or ".." in session_id:
    sys.exit(1)

try:
    with open(state_file, "r", encoding="utf-8") as f:
        state = json.load(f)
except (json.JSONDecodeError, OSError):
    state = {"version":4,"sessions":{},"file_registry":{},"build_locks":{},"knowledge":{},"decisions":[],"bug_patterns":[]}

now = datetime.fromisoformat(ts.replace("Z", "+00:00"))
cutoff = timedelta(minutes=15)

state.setdefault("sessions", {})
state.setdefault("file_registry", {})
state.setdefault("build_locks", {})

# Cleanup stale sessions
for sid in list(state["sessions"]):
    s = state["sessions"][sid]
    if s.get("status") != "active":
        continue
    try:
        hb = datetime.fromisoformat(s["heartbeat"].replace("Z", "+00:00"))
        if now - hb > cutoff:
            for fp in s.get("claimed_files", []):
                state["file_registry"].pop(fp, None)
            del state["sessions"][sid]
    except (ValueError, KeyError):
        del state["sessions"][sid]

# Register current session
if session_id not in state["sessions"]:
    state["sessions"][session_id] = {
        "name": session_id, "started_at": ts, "heartbeat": ts,
        "status": "active", "task": "not set - call bb_register to update", "claimed_files": [],
    }
else:
    state["sessions"][session_id]["heartbeat"] = ts
    state["sessions"][session_id]["status"] = "active"

state["last_updated"] = ts
dn = os.path.dirname(state_file)
fd, tmp = tempfile.mkstemp(dir=dn, suffix=".tmp")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, state_file)
except Exception:
    try:
        os.unlink(tmp)
    except OSError:
        pass
PYEOF

echo "$TIMESTAMP | $SESSION_ID | SESSION_STARTED | auto-registered" >> "$EVENTS_LOG" 2>/dev/null || true

# Inject reminder: must update task
echo ""
echo "## Blackboard registration complete"
echo ""
echo "Your session_id: \`$SESSION_ID\`"
echo "Call \`bb_register(session_id=\"$SESSION_ID\", task=\"what you are doing\")\` to set your task description."

exit 0