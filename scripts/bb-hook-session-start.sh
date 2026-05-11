#!/usr/bin/env bash
# bb-hook-session-start.sh — SessionStart hook: register session to GLOBAL state.json
# All projects share the same global registry. File claims/locks are per-project.
set -uo pipefail

INPUT=$(cat 2>/dev/null || echo "{}")

SESSION_ID=$(echo "$INPUT" | python -c "import json,sys; d=json.load(sys.stdin); print(d.get('session_id',''))" 2>/dev/null || echo "")
CWD=$(echo "$INPUT" | python -c "import json,sys; d=json.load(sys.stdin); print(d.get('cwd',''))" 2>/dev/null || echo "")

[ -z "$SESSION_ID" ] && exit 0

# Validate session_id format
if echo "$SESSION_ID" | grep -qE '[;&|`$(){}!]'; then
    echo "BLOCKED: Invalid session_id format" >&2
    exit 1
fi

# Always write to GLOBAL state.json
BB_DIR="$HOME/.claude/blackboard"
mkdir -p "$BB_DIR"
SF="$BB_DIR/state.json"
EVENTS_LOG="$BB_DIR/events.log"
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# Initialize if not exists
[ ! -f "$SF" ] && python - "$SF" "$TIMESTAMP" << 'PYINIT' > /dev/null 2>&1 || true
import json, os, sys
sf, ts = sys.argv[1:3]
os.makedirs(os.path.dirname(sf), exist_ok=True)
s = {"version":4,"last_updated":ts,"sessions":{},"knowledge":{},"decisions":[],"bug_patterns":[]}
with open(sf, "w", encoding="utf-8") as f:
    json.dump(s, f, indent=2, ensure_ascii=False)
PYINIT

# Register + cleanup stale + rotate events.log
# Uses file locking to prevent race conditions
python - "$SF" "$SESSION_ID" "$TIMESTAMP" "$CWD" "$EVENTS_LOG" << 'PYEOF' > /dev/null 2>&1 || true
import json, sys, os, tempfile, shutil
from datetime import datetime, timezone, timedelta

state_file, session_id, ts, cwd, events_log = sys.argv[1:6]

if not session_id or not state_file:
    sys.exit(0)
if ".." in state_file or ".." in session_id:
    sys.exit(1)

# File locking
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
    import time
    time.sleep(0.5)
    if not acquire_lock():
        sys.exit(0)

try:
    # Load with recovery chain
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
            state = {"version":4,"sessions":{},"knowledge":{},"decisions":[],"bug_patterns":[]}

    now = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    soft_cutoff = timedelta(minutes=30)
    hard_cutoff = timedelta(minutes=35)
    ended_cutoff = timedelta(hours=24)

    state.setdefault("sessions", {})
    state.setdefault("knowledge", {})
    state.setdefault("decisions", [])
    state.setdefault("bug_patterns", [])

    # Two-level stale cleanup
    for sid in list(state["sessions"]):
        s = state["sessions"][sid]
        if s.get("status") not in ("active", "stale"):
            continue
        try:
            hb = datetime.fromisoformat(s["heartbeat"].replace("Z", "+00:00"))
            if s.get("status") == "stale" and (now - hb) > hard_cutoff:
                s["status"] = "ended"
                s["claimed_files"] = []
            elif (now - hb) > soft_cutoff and s.get("status") == "active":
                s["status"] = "stale"
        except (ValueError, KeyError):
            pass

    # Ended purge (>24h)
    for sid in list(state["sessions"]):
        s = state["sessions"][sid]
        if s.get("status") == "ended":
            try:
                hb = datetime.fromisoformat(s["heartbeat"].replace("Z", "+00:00"))
                if (now - hb) > ended_cutoff:
                    del state["sessions"][sid]
            except (ValueError, KeyError):
                del state["sessions"][sid]

    # Register current session
    if session_id not in state["sessions"]:
        state["sessions"][session_id] = {
            "name": session_id, "started_at": ts, "heartbeat": ts,
            "status": "active", "task": "not set - call bb_register to update",
            "claimed_files": [], "project_dir": cwd,
        }
    else:
        state["sessions"][session_id]["heartbeat"] = ts
        state["sessions"][session_id]["status"] = "active"
        if cwd:
            state["sessions"][session_id]["project_dir"] = cwd

    state["last_updated"] = ts

    # Backup + atomic write
    if os.path.isfile(state_file):
        try:
            shutil.copy2(state_file, state_file + ".bak")
        except OSError:
            pass
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

    # Rotate events.log
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

echo "$TIMESTAMP | $SESSION_ID | SESSION_STARTED | cwd=$CWD" >> "$EVENTS_LOG" 2>/dev/null || true

# Inject reminder
echo ""
echo "## Blackboard registration complete"
echo ""
echo "Your session_id: \`$SESSION_ID\`"
echo "Call \`bb_register(session_id=\"$SESSION_ID\", task=\"what you are doing\")\` to set your task description."
echo ""
echo "If MCP tools are unavailable, check status via: \`bash ~/.claude/blackboard/scripts/bb-status.sh\`"

exit 0
