#!/usr/bin/env bash
# bb-hook-session-start.sh — SessionStart hook: 注册session + 清理stale
# 只写state.json，不调MCP。MCP server会自动reload。
set -uo pipefail

INPUT=$(cat 2>/dev/null || echo "{}")

SESSION_ID=$(echo "$INPUT" | python -c "import json,sys; d=json.load(sys.stdin); print(d.get('session_id',''))" 2>/dev/null || echo "")
CWD=$(echo "$INPUT" | python -c "import json,sys; d=json.load(sys.stdin); print(d.get('cwd',''))" 2>/dev/null || echo "")

[ -z "$SESSION_ID" ] && exit 0

# 发现state.json
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

# 初始化state.json（如果不存在）
[ ! -f "$SF" ] && python -c "
import json,os
os.makedirs('$BB_DIR',exist_ok=True)
s={'version':4,'last_updated':'$TIMESTAMP','sessions':{},'file_registry':{},'build_locks':{},'knowledge':{},'decisions':[],'bug_patterns':[]}
with open('$SF','w',encoding='utf-8') as f: json.dump(s,f,indent=2,ensure_ascii=False)
" 2>/dev/null || true

# 注册 + 清理stale(>15min无心跳) — 一次Python调用
python - "$SF" "$SESSION_ID" "$TIMESTAMP" << 'PYEOF' > /dev/null 2>&1 || true
import json, sys, os, tempfile
from datetime import datetime, timezone, timedelta

state_file, session_id, ts = sys.argv[1:4]
try:
    with open(state_file, 'r', encoding='utf-8') as f:
        state = json.load(f)
except (json.JSONDecodeError, OSError):
    state = {"version":4,"sessions":{},"file_registry":{},"build_locks":{},"knowledge":{},"decisions":[],"bug_patterns":[]}

now = datetime.fromisoformat(ts.replace('Z', '+00:00'))
cutoff = timedelta(minutes=15)

state.setdefault('sessions', {})
state.setdefault('file_registry', {})
state.setdefault('build_locks', {})

# 清理stale session
for sid in list(state['sessions']):
    s = state['sessions'][sid]
    if s.get('status') != 'active':
        continue
    try:
        hb = datetime.fromisoformat(s['heartbeat'].replace('Z', '+00:00'))
        if now - hb > cutoff:
            for fp in s.get('claimed_files', []):
                state['file_registry'].pop(fp, None)
            del state['sessions'][sid]
    except (ValueError, KeyError):
        del state['sessions'][sid]

# 注册当前session
if session_id not in state['sessions']:
    state['sessions'][session_id] = {
        'name': session_id, 'started_at': ts, 'heartbeat': ts,
        'status': 'active', 'task': '⚠️未设置-请调用bb_register更新task', 'claimed_files': [],
    }
else:
    state['sessions'][session_id]['heartbeat'] = ts
    state['sessions'][session_id]['status'] = 'active'

state['last_updated'] = ts
dn = os.path.dirname(state_file)
fd, tmp = tempfile.mkstemp(dir=dn, suffix='.tmp')
try:
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, state_file)
except:
    os.unlink(tmp)
PYEOF

echo "$TIMESTAMP | $SESSION_ID | SESSION_STARTED | auto-registered" >> "$EVENTS_LOG" 2>/dev/null || true

# 注入提醒：必须更新task
echo ""
echo "## ⚠️ Blackboard注册完成 — 你必须立即更新task"
echo ""
echo "你的session已自动注册到Blackboard，session_id: \`$SESSION_ID\`"
echo "**立即调用**: \`bb_register(\"$SESSION_ID\", \"你正在做什么\")\`"
echo "例: \`bb_register(\"$SESSION_ID\", \"修复登录失败bug\")\`"
echo ""
echo "**注意**: name参数必须用 \`$SESSION_ID\`，不要用自定义名字，否则会创建重复session。"

exit 0
