#!/usr/bin/env bash
# bb-hook-session-end.sh — SessionEnd hook: deregister session + release files/locks
# Triggered when a Claude Code session closes.
set -uo pipefail

INPUT=$(cat 2>/dev/null || echo "{}")

SESSION_ID=$(echo "$INPUT" | python -c "import json,sys; d=json.load(sys.stdin); print(d.get('session_id',''))" 2>/dev/null || echo "")
CWD=$(echo "$INPUT" | python -c "import json,sys; d=json.load(sys.stdin); print(d.get('cwd',''))" 2>/dev/null || echo "")

[ -z "$SESSION_ID" ] && exit 0

# Validate session_id format
if echo "$SESSION_ID" | grep -qE '[;&|`$(){}!]'; then
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
    SF="$HOME/.claude/blackboard/state.json"
fi

BB_DIR=$(dirname "$SF")
EVENTS_LOG="$BB_DIR/events.log"
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# Deregister session with file locking
python - "$SF" "$SESSION_ID" "$TIMESTAMP" << 'PYEOF' > /dev/null 2>&1 || true
import json, sys, os, tempfile, shutil
from datetime import datetime, timezone

state_file, session_id, ts = sys.argv[1:4]

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
    if not os.path.isfile(state_file):
        sys.exit(0)

    try:
        with open(state_file, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError):
        sys.exit(0)

    if session_id not in state.get("sessions", {}):
        sys.exit(0)

    session = state["sessions"][session_id]

    # Release all claimed files
    released_files = []
    for fp in list(session.get("claimed_files", [])):
        if state.get("file_registry", {}).get(fp) == session_id:
            state["file_registry"].pop(fp, None)
            released_files.append(fp)

    # Release build locks
    released_locks = []
    for proj in list(state.get("build_locks", {})):
        lock = state["build_locks"][proj]
        owner = lock if isinstance(lock, str) else lock.get("session_id", "")
        if owner == session_id:
            del state["build_locks"][proj]
            released_locks.append(proj)

    # Mark session as ended
    session["status"] = "ended"
    session["heartbeat"] = ts
    state["last_updated"] = ts

    # Backup + atomic write
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

finally:
    release_lock()
PYEOF

echo "$TIMESTAMP | $SESSION_ID | SESSION_ENDED | auto-deregistered" >> "$EVENTS_LOG" 2>/dev/null || true

exit 0
