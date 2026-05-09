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

# Register + cleanup stale + cleanup expired locks + rotate events.log
# Uses file locking to prevent race conditions when multiple sessions start simultaneously
python - "$SF" "$SESSION_ID" "$TIMESTAMP" "$EVENTS_LOG" << 'PYEOF' > /dev/null 2>&1 || true
import json, sys, os, tempfile, shutil
from datetime import datetime, timezone, timedelta

state_file, session_id, ts, events_log = sys.argv[1:5]

# Validate inputs
if not session_id or not state_file:
    sys.exit(0)
# Sanitize: reject paths with directory traversal
if ".." in state_file or ".." in session_id:
    sys.exit(1)

# File locking for race condition prevention
lock_file = state_file + ".lock"
lock_fd = None

def acquire_lock():
    global lock_fd
    try:
        lock_fd = open(lock_file, "w")
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
        return True
    except (OSError, IOError):
        return False

def release_lock():
    global lock_fd
    try:
        if lock_fd:
            if sys.platform == "win32":
                import msvcrt
                lock_fd.seek(0)
                msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
            lock_fd.close()
    except (OSError, IOError):
        pass
    try:
        os.unlink(lock_file)
    except OSError:
        pass

if not acquire_lock():
    # Could not acquire lock, another hook is running. Wait briefly and retry.
    import time
    time.sleep(0.5)
    if not acquire_lock():
        sys.exit(0)  # Give up gracefully

try:
    # Load state with recovery chain: state.json -> .bak -> empty
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError):
        bak = state_file + ".bak"
        state = None
        if os.path.isfile(bak):
            try:
                with open(bak, "r", encoding="utf-8") as f:
                    state = json.load(f)
            except (json.JSONDecodeError, OSError):
                state = None
        if state is None:
            state = {"version":4,"sessions":{},"file_registry":{},"build_locks":{},"knowledge":{},"decisions":[],"bug_patterns":[]}

    now = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    cutoff = timedelta(minutes=15)

    state.setdefault("sessions", {})
    state.setdefault("file_registry", {})
    state.setdefault("build_locks", {})

    # Cleanup stale sessions (>15min no heartbeat)
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

    # Cleanup expired build locks (>10min held)
    for proj in list(state.get("build_locks", {})):
        lock = state["build_locks"][proj]
        if isinstance(lock, dict) and lock.get("acquired_at"):
            try:
                at = datetime.fromisoformat(lock["acquired_at"].replace("Z", "+00:00"))
                if (now - at).total_seconds() > 600:
                    del state["build_locks"][proj]
            except (ValueError, KeyError):
                del state["build_locks"][proj]

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

    # Backup before write
    if os.path.isfile(state_file):
        try:
            shutil.copy2(state_file, state_file + ".bak")
        except OSError:
            pass

    # Atomic write
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

    # Rotate events.log if too large (>500 lines -> keep last 200)
    if os.path.isfile(events_log):
        try:
            with open(events_log, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > 500:
                shutil.copy2(events_log, events_log + ".bak")
                with open(events_log, "w", encoding="utf-8") as f:
                    f.writelines(lines[-200:])
        except Exception:
            pass

finally:
    release_lock()
PYEOF

echo "$TIMESTAMP | $SESSION_ID | SESSION_STARTED | auto-registered" >> "$EVENTS_LOG" 2>/dev/null || true

# Inject reminder: must update task + graceful degradation hint
echo ""
echo "## Blackboard registration complete"
echo ""
echo "Your session_id: \`$SESSION_ID\`"
echo "Call \`bb_register(session_id=\"$SESSION_ID\", task=\"what you are doing\")\` to set your task description."
echo ""
echo "If MCP tools are unavailable, check status via: \`bash ~/.claude/blackboard/scripts/bb-status.sh\`"

exit 0
