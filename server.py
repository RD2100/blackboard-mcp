#!/usr/bin/env python3
"""Blackboard MCP Server v4 — 双层架构

双层state.json:
  - 全局(~/.claude/blackboard/state.json): sessions + knowledge + decisions + bug_patterns
  - 项目级(项目/.claude/blackboard/state.json): file_registry + build_locks

所有项目共享同一个全局注册中心，文件冲突和编译锁按项目隔离。

生命周期:
  active → (30min无心跳) → stale → (5min恢复窗口) → ended → (24h) → 删除
  stale session心跳恢复时自动回到active
"""

import json
import os
import shutil
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("blackboard")

# ─── 配置 ───

STALE_SOFT_MINUTES = 30
STALE_HARD_MINUTES = 35
ENDED_PURGE_HOURS = 24
BUILD_LOCK_TIMEOUT_SECONDS = 600
KNOWLEDGE_HALF_LIFE_DAYS = 30
KNOWLEDGE_ARCHIVE_DAYS = 60
SOLIDIFY_THRESHOLD = 0.8
DECAY_INTERVAL = 3600

# ─── 状态管理 ───

_lock = threading.Lock()
_state = {}       # 全局: sessions + knowledge + decisions + bug_patterns
_proj_state = {}  # 项目级: file_registry + build_locks
_global_file = None
_proj_file = None
_events_file = None
_global_mtime = 0
_proj_mtime = 0

GLOBAL_BB_DIR = os.path.join(os.path.expanduser("~"), ".claude", "blackboard")


def _discover_project_dir():
    """发现项目目录"""
    for env_var in ["CLAUDE_PROJECT_DIR", "PWD"]:
        d = os.environ.get(env_var, "")
        if d and ".." not in d:
            return d
    try:
        import subprocess
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return None


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_dt():
    return datetime.now(timezone.utc)


def _is_uuid(s):
    try:
        import uuid as _uuid
        _uuid.UUID(s)
        return True
    except (ValueError, AttributeError):
        return False


def _require_uuid(session_id: str) -> str:
    if not session_id or not _is_uuid(session_id):
        return f"ERROR: session_id必须是UUID格式，收到'{session_id}'。请使用SessionStart hook注入的session_id。"
    return ""


def _try_load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def _file_mtime(path):
    try:
        return os.path.getmtime(path) if path and os.path.isfile(path) else 0
    except OSError:
        return 0


def _atomic_write(path, data):
    """原子写入 + .bak备份"""
    dn = os.path.dirname(path)
    os.makedirs(dn, exist_ok=True)
    if os.path.isfile(path):
        try:
            shutil.copy2(path, path + ".bak")
        except OSError:
            pass
    fd, tmp = tempfile.mkstemp(dir=dn, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _load_state():
    """双层加载：全局(sessions+knowledge) + 项目级(file_registry+build_locks)"""
    global _state, _proj_state, _global_file, _proj_file, _events_file, _global_mtime, _proj_mtime

    # 全局路径
    os.makedirs(GLOBAL_BB_DIR, exist_ok=True)
    _global_file = os.path.join(GLOBAL_BB_DIR, "state.json")
    _events_file = os.path.join(GLOBAL_BB_DIR, "events.log")

    # 项目级路径
    proj_dir = _discover_project_dir()
    if proj_dir:
        proj_bb = os.path.join(proj_dir, ".claude", "blackboard")
        os.makedirs(proj_bb, exist_ok=True)
        _proj_file = os.path.join(proj_bb, "state.json")
    else:
        _proj_file = None

    # 加载全局（恢复链: state.json → .bak → 空）
    _state = _try_load_json(_global_file)
    if _state is None:
        bak = _global_file + ".bak"
        _state = _try_load_json(bak) if os.path.isfile(bak) else None
    if _state is None:
        _state = {"version": 4, "last_updated": _now(), "sessions": {},
                  "knowledge": {}, "decisions": [], "bug_patterns": []}
    _state.setdefault("sessions", {})
    _state.setdefault("knowledge", {})
    _state.setdefault("decisions", [])
    _state.setdefault("bug_patterns", [])

    # MCP重启韧性：2h内ended/stale → active
    now = _now_dt()
    for sid in list(_state["sessions"]):
        s = _state["sessions"][sid]
        if s.get("status") in ("ended", "stale"):
            try:
                hb = datetime.fromisoformat(s["heartbeat"].replace("Z", "+00:00"))
                if now - hb < timedelta(hours=2):
                    s["status"] = "active"
                    s["heartbeat"] = _now()
            except (ValueError, KeyError):
                pass

    # 加载项目级
    if _proj_file:
        _proj_state = _try_load_json(_proj_file)
        if _proj_state is None:
            bak = _proj_file + ".bak"
            _proj_state = _try_load_json(bak) if os.path.isfile(bak) else None
        if _proj_state is None:
            _proj_state = {"version": 4, "file_registry": {}, "build_locks": {}}
        _proj_state.setdefault("file_registry", {})
        _proj_state.setdefault("build_locks", {})
    else:
        _proj_state = {"version": 4, "file_registry": {}, "build_locks": {}}

    _global_mtime = _file_mtime(_global_file)
    _proj_mtime = _file_mtime(_proj_file) if _proj_file else 0


def _save_state():
    """双层原子写入"""
    global _global_mtime, _proj_mtime
    _state["last_updated"] = _now()
    _atomic_write(_global_file, _state)
    _global_mtime = _file_mtime(_global_file)
    if _proj_file:
        _proj_state["last_updated"] = _now()
        _atomic_write(_proj_file, _proj_state)
        _proj_mtime = _file_mtime(_proj_file)


def _maybe_reload_from_disk():
    """检测磁盘变化，自动reload"""
    gm = _file_mtime(_global_file)
    pm = _file_mtime(_proj_file) if _proj_file else 0
    if gm > _global_mtime or pm > _proj_mtime:
        _load_state()


def _append_event(msg):
    try:
        with open(_events_file, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def _auto_cleanup(caller_sid=None):
    """自动清理：磁盘reload + stale两级 + orphan + expired locks + ended purge"""
    _maybe_reload_from_disk()

    now = _now_dt()
    soft_cutoff = now - timedelta(minutes=STALE_SOFT_MINUTES)
    hard_cutoff = now - timedelta(minutes=STALE_HARD_MINUTES)
    ended_cutoff = now - timedelta(hours=ENDED_PURGE_HOURS)

    # 两级stale清理
    for sid in list(_state.get("sessions", {})):
        s = _state["sessions"][sid]
        if s.get("status") not in ("active", "stale"):
            continue
        try:
            hb = datetime.fromisoformat(s["heartbeat"].replace("Z", "+00:00"))
            if s.get("status") == "stale" and hb < hard_cutoff:
                s["status"] = "ended"
                for fp in list(s.get("claimed_files", [])):
                    _proj_state.get("file_registry", {}).pop(fp, None)
                s["claimed_files"] = []
                for proj in list(_proj_state.get("build_locks", {})):
                    lock = _proj_state["build_locks"][proj]
                    owner = lock if isinstance(lock, str) else lock.get("session_id", "")
                    if owner == sid:
                        del _proj_state["build_locks"][proj]
            elif hb < soft_cutoff and s.get("status") == "active":
                s["status"] = "stale"
        except (ValueError, KeyError):
            pass

    # Orphan资源清理
    active_sids = {sid for sid, s in _state.get("sessions", {}).items() if s.get("status") == "active"}
    for fp in list(_proj_state.get("file_registry", {})):
        if _proj_state["file_registry"][fp] not in active_sids:
            del _proj_state["file_registry"][fp]
    for proj in list(_proj_state.get("build_locks", {})):
        lock = _proj_state["build_locks"][proj]
        owner = lock if isinstance(lock, str) else lock.get("session_id", "")
        if owner not in active_sids:
            del _proj_state["build_locks"][proj]

    # Expired build locks (>10min)
    for proj in list(_proj_state.get("build_locks", {})):
        lock = _proj_state["build_locks"][proj]
        if isinstance(lock, dict) and lock.get("acquired_at"):
            try:
                at = datetime.fromisoformat(lock["acquired_at"].replace("Z", "+00:00"))
                if (now - at).total_seconds() > BUILD_LOCK_TIMEOUT_SECONDS:
                    del _proj_state["build_locks"][proj]
            except (ValueError, KeyError):
                del _proj_state["build_locks"][proj]

    # Ended purge (>24h)
    for sid in list(_state.get("sessions", {})):
        s = _state["sessions"][sid]
        if s.get("status") == "ended":
            try:
                hb = datetime.fromisoformat(s["heartbeat"].replace("Z", "+00:00"))
                if hb < ended_cutoff:
                    del _state["sessions"][sid]
            except (ValueError, KeyError):
                del _state["sessions"][sid]

    # 刷新调用者心跳
    if caller_sid and caller_sid in _state.get("sessions", {}):
        _state["sessions"][caller_sid]["heartbeat"] = _now()
        _state["sessions"][caller_sid]["status"] = "active"


def _rotate_events(max_lines=500, keep_lines=200):
    if not os.path.isfile(_events_file):
        return
    try:
        with open(_events_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > max_lines:
            shutil.copy2(_events_file, _events_file + ".bak")
            with open(_events_file, "w", encoding="utf-8") as f:
                f.writelines(lines[-keep_lines:])
    except Exception:
        pass


# ─── 后台线程 ───

def _stale_cleanup_loop():
    """每60秒执行自动清理"""
    while True:
        time.sleep(60)
        try:
            with _lock:
                _auto_cleanup()
                _save_state()
        except Exception:
            pass


def _decay_loop():
    while True:
        time.sleep(DECAY_INTERVAL)
        try:
            with _lock:
                now = _now_dt()
                changed = False
                to_archive = []
                for fp, k in list(_state.get("knowledge", {}).items()):
                    last_ref = k.get("last_referenced", k.get("created_at", ""))
                    if not last_ref:
                        continue
                    try:
                        ref_dt = datetime.fromisoformat(last_ref.replace("Z", "+00:00"))
                        age_days = (now - ref_dt).days
                    except (ValueError, KeyError):
                        continue
                    if age_days >= KNOWLEDGE_ARCHIVE_DAYS:
                        to_archive.append(fp)
                    elif age_days >= KNOWLEDGE_HALF_LIFE_DAYS:
                        periods = age_days // KNOWLEDGE_HALF_LIFE_DAYS
                        new_conf = max(0.1, k.get("original_confidence", k.get("confidence", 0.5)) * (0.5 ** periods))
                        if abs(k.get("confidence", 0) - new_conf) > 0.01:
                            k["confidence"] = round(new_conf, 2)
                            changed = True
                if to_archive:
                    _state.setdefault("archived_knowledge", {})
                    for fp in to_archive:
                        _state["knowledge"][fp]["status"] = "archived"
                        _state["archived_knowledge"][fp] = _state["knowledge"].pop(fp)
                    changed = True
                if changed:
                    _save_state()
        except Exception:
            pass


# ─── MCP工具 ───

@mcp.tool()
def bb_register(session_id: str, name: str = "", task: str = "") -> str:
    """注册或更新session信息。新session启动时调用。
    session_id: 必填。由SessionStart hook注入的UUID。
    name: 显示名称（可选）。
    task: 当前任务描述。"""
    if not _is_uuid(session_id):
        return f"ERROR: session_id必须是UUID格式，收到'{session_id}'。请使用SessionStart hook注入的session_id。"
    with _lock:
        _auto_cleanup(session_id)
        ts = _now()
        proj_dir = _discover_project_dir() or ""

        if session_id in _state["sessions"]:
            existing = _state["sessions"][session_id]
            existing["heartbeat"] = ts
            existing["status"] = "active"
            if task:
                existing["task"] = task
            if name:
                existing["display_name"] = name
            if proj_dir:
                existing["project_dir"] = proj_dir
            _save_state()
            _rotate_events()
            _append_event(f"{ts} | {session_id} | REGISTERED | name={name} task={task}")
            display = existing.get("display_name", session_id[:8])
            return f"Updated: {display} (task={existing.get('task','')})"

        _state["sessions"][session_id] = {
            "name": name or session_id, "display_name": name or "",
            "started_at": ts, "heartbeat": ts,
            "status": "active", "task": task, "claimed_files": [],
            "project_dir": proj_dir,
        }
        _save_state()
        _rotate_events()
    _append_event(f"{ts} | {session_id} | REGISTERED | name={name} task={task}")
    return f"Registered: {name or session_id[:8]} (sid={session_id})"


@mcp.tool()
def bb_deregister(session_id: str) -> str:
    """注销session，释放所有文件占用和编译锁。"""
    err = _require_uuid(session_id)
    if err: return err
    released = []
    with _lock:
        _auto_cleanup(session_id)
        if session_id in _state.get("sessions", {}):
            for fp in _state["sessions"][session_id].get("claimed_files", []):
                if _proj_state.get("file_registry", {}).get(fp) == session_id:
                    _proj_state["file_registry"].pop(fp, None)
                    released.append(fp)
            for proj in list(_proj_state.get("build_locks", {})):
                lock = _proj_state["build_locks"][proj]
                owner = lock if isinstance(lock, str) else lock.get("session_id", "")
                if owner == session_id:
                    del _proj_state["build_locks"][proj]
            _state["sessions"][session_id]["status"] = "ended"
            _save_state()
    _append_event(f"{_now()} | {session_id} | DEREGISTERED | released={len(released)} files")
    return f"Deregistered: {session_id}, released {len(released)} files"


@mcp.tool()
def bb_heartbeat(session_id: str) -> str:
    """刷新session心跳。"""
    err = _require_uuid(session_id)
    if err: return err
    with _lock:
        _auto_cleanup(session_id)
        if session_id in _state.get("sessions", {}):
            _state["sessions"][session_id]["heartbeat"] = _now()
            _state["sessions"][session_id]["status"] = "active"
            _save_state()
            return f"Heartbeat: {session_id}"
        return f"Session {session_id} not found"


@mcp.tool()
def bb_claim_file(session_id: str, file_path: str) -> str:
    """声明文件占用（项目级）。冲突返回CONFLICT，已占用返回OWN，成功返回CLAIMED。"""
    err = _require_uuid(session_id)
    if err: return err
    file_path = file_path.lstrip("./")
    with _lock:
        _auto_cleanup(session_id)
        fr = _proj_state.setdefault("file_registry", {})
        if file_path in fr:
            owner = fr[file_path]
            if owner == session_id:
                return f"OWN: {file_path}"
            owner_s = _state.get("sessions", {}).get(owner, {})
            if owner_s.get("status") == "active":
                return f"CONFLICT: {file_path} claimed by '{owner_s.get('display_name', owner[:8])}'"
        fr[file_path] = session_id
        sess = _state.get("sessions", {}).get(session_id, {})
        claimed = sess.setdefault("claimed_files", [])
        if file_path not in claimed:
            claimed.append(file_path)
        _save_state()
    return f"CLAIMED: {file_path}"


@mcp.tool()
def bb_release_file(session_id: str, file_path: str) -> str:
    """释放文件占用（项目级）。"""
    err = _require_uuid(session_id)
    if err: return err
    file_path = file_path.lstrip("./")
    with _lock:
        _auto_cleanup(session_id)
        fr = _proj_state.get("file_registry", {})
        if fr.get(file_path) == session_id:
            del fr[file_path]
            claimed = _state.get("sessions", {}).get(session_id, {}).get("claimed_files", [])
            if file_path in claimed:
                claimed.remove(file_path)
            _save_state()
            return f"RELEASED: {file_path}"
        return f"NOT_OWNED: {file_path}"


@mcp.tool()
def bb_check_conflicts(file_paths: str = "") -> str:
    """查询文件占用状态（项目级）。file_paths逗号分隔，空则查全部。"""
    with _lock:
        _auto_cleanup()
        fr = _proj_state.get("file_registry", {})
        if file_paths:
            paths = [p.strip().lstrip("./") for p in file_paths.split(",") if p.strip()]
        else:
            paths = list(fr.keys())
        if not paths:
            return "No files claimed"
        lines = []
        for fp in paths:
            owner = fr.get(fp, "")
            if owner:
                name = _state.get("sessions", {}).get(owner, {}).get("display_name", owner[:8])
                lines.append(f"  {fp} -> {name}")
            else:
                lines.append(f"  {fp} -> FREE")
        return "\n".join(lines)


@mcp.tool()
def bb_acquire_build_lock(session_id: str, project_dir: str = "") -> str:
    """获取编译锁（项目级）。同一项目只允许1个session编译。"""
    err = _require_uuid(session_id)
    if err: return err
    with _lock:
        _auto_cleanup(session_id)
        locks = _proj_state.setdefault("build_locks", {})
        existing = locks.get(project_dir)
        if existing:
            owner = existing if isinstance(existing, str) else existing.get("session_id", "")
            if owner != session_id:
                owner_s = _state.get("sessions", {}).get(owner, {})
                if owner_s.get("status") == "active":
                    acquired_at = existing.get("acquired_at", "") if isinstance(existing, dict) else ""
                    if acquired_at:
                        try:
                            at = datetime.fromisoformat(acquired_at.replace("Z", "+00:00"))
                            if (_now_dt() - at).total_seconds() > BUILD_LOCK_TIMEOUT_SECONDS:
                                pass  # timeout, will be replaced
                            else:
                                return f"CONFLICT: Build lock held by '{owner_s.get('display_name', owner[:8])}'."
                        except (ValueError, KeyError):
                            pass
                    else:
                        return f"CONFLICT: Build lock held by '{owner_s.get('display_name', owner[:8])}'."
        locks[project_dir] = {"session_id": session_id, "acquired_at": _now()}
        _save_state()
    _append_event(f"{_now()} | {session_id} | BUILD_LOCK_ACQUIRED | project={project_dir}")
    return f"ACQUIRED: Build lock for {project_dir}"


@mcp.tool()
def bb_release_build_lock(session_id: str, project_dir: str = "") -> str:
    """释放编译锁（项目级）。"""
    err = _require_uuid(session_id)
    if err: return err
    with _lock:
        _auto_cleanup(session_id)
        locks = _proj_state.get("build_locks", {})
        lock = locks.get(project_dir)
        if lock:
            owner = lock if isinstance(lock, str) else lock.get("session_id", "")
            if owner == session_id:
                del locks[project_dir]
                _save_state()
    _append_event(f"{_now()} | {session_id} | BUILD_LOCK_RELEASED | project={project_dir}")
    return f"RELEASED: Build lock for {project_dir}"


@mcp.tool()
def bb_status() -> str:
    """查看Blackboard完整状态：全局sessions + 项目级文件占用/编译锁 + 知识 + 健康检查。"""
    with _lock:
        _auto_cleanup()
        lines = ["=== Blackboard Status ==="]

        # Sessions (全局)
        active = sum(1 for s in _state["sessions"].values() if s.get("status") == "active")
        stale = sum(1 for s in _state["sessions"].values() if s.get("status") == "stale")
        ended = sum(1 for s in _state["sessions"].values() if s.get("status") == "ended")
        lines.append(f"\nSessions: {len(_state['sessions'])} total, {active} active, {stale} stale, {ended} ended")
        for sid, s in _state["sessions"].items():
            status = s.get("status", "?")
            display = s.get("display_name", sid[:8])
            task = s.get("task", "")
            proj = s.get("project_dir", "")
            proj_short = os.path.basename(proj) if proj else "?"
            files = len(s.get("claimed_files", []))
            lines.append(f"  [{status:>7}] {display} | proj={proj_short} | files={files} | {task}")

        # File registry (项目级)
        lines.append(f"\nFile claims: {len(_proj_state['file_registry'])}")
        for fp, owner in list(_proj_state["file_registry"].items())[:20]:
            name = _state["sessions"].get(owner, {}).get("display_name", owner[:8])
            lines.append(f"  {fp} -> {name}")

        # Build locks (项目级)
        if _proj_state["build_locks"]:
            lines.append(f"\nBuild locks: {len(_proj_state['build_locks'])}")
            for proj, lock in _proj_state["build_locks"].items():
                owner = lock if isinstance(lock, str) else lock.get("session_id", "")
                name = _state["sessions"].get(owner, {}).get("display_name", owner[:8])
                lines.append(f"  {proj} -> {name}")

        # Knowledge (全局)
        kn = _state.get("knowledge", {})
        high = sum(1 for k in kn.values() if k.get("confidence", 0) >= SOLIDIFY_THRESHOLD)
        lines.append(f"\nKnowledge: {len(kn)} entries, {high} high-confidence")
        for fp, k in list(kn.items())[:10]:
            conf = k.get("confidence", 0)
            cat = k.get("category", "")
            text = k.get("text", "")[:60]
            lines.append(f"  [{conf:.1f}] ({cat}) {fp}: {text}")

        # Decisions
        decs = _state.get("decisions", [])
        lines.append(f"\nDecisions: {len(decs)}")
        for d in decs[-5:]:
            lines.append(f"  {d.get('timestamp', '')[:16]} | {d.get('decision', '')[:60]}")

        # Bug patterns
        bps = _state.get("bug_patterns", [])
        lines.append(f"\nBug patterns: {len(bps)}")
        for b in bps[-5:]:
            lines.append(f"  {b.get('timestamp', '')[:16]} | {b.get('pattern', '')[:60]}")

        # Health Check
        lines.append("\n--- Health Check ---")
        checks = []
        checks.append(("global state", "OK" if _state else "CORRUPT"))
        checks.append(("project state", "OK" if _proj_state else "CORRUPT"))
        checks.append(("events.log", "OK" if os.path.isfile(_events_file or "") else "MISSING"))
        dead_locks = 0
        for proj, lock in _proj_state.get("build_locks", {}).items():
            owner = lock if isinstance(lock, str) else lock.get("session_id", "")
            if owner not in _state.get("sessions", {}) or _state["sessions"][owner].get("status") != "active":
                dead_locks += 1
        checks.append(("build_locks", "OK" if dead_locks == 0 else f"{dead_locks} orphaned"))
        for name, status in checks:
            icon = "✅" if status == "OK" else "❌"
            lines.append(f"  {icon} {name}: {status}")

        return "\n".join(lines)


@mcp.tool()
def bb_event(session_id: str, event_type: str, details: str = "") -> str:
    """记录事件。"""
    err = _require_uuid(session_id)
    if err: return err
    with _lock:
        _auto_cleanup(session_id)
        if session_id in _state.get("sessions", {}):
            _state["sessions"][session_id]["event_count"] = _state["sessions"][session_id].get("event_count", 0) + 1
            _save_state()
    msg = f"{_now()} | {session_id} | {event_type} | {details}"
    _append_event(msg)
    return f"Event logged: {event_type}"


@mcp.tool()
def bb_session_files(session_id: str) -> str:
    """列出session占用的所有文件。"""
    err = _require_uuid(session_id)
    if err: return err
    with _lock:
        _auto_cleanup(session_id)
        session = _state.get("sessions", {}).get(session_id)
        if not session:
            return f"Session {session_id} not found"
        claimed = session.get("claimed_files", [])
        if not claimed:
            return f"No files claimed by {session.get('display_name', session_id[:8])}"
        return f"Files claimed by {session.get('display_name', session_id[:8])}:\n" + "\n".join(f"  {f}" for f in claimed)


# ─── 知识管理 ───

@mcp.tool()
def bb_share_knowledge(session_id: str, fingerprint: str, category: str, text: str) -> str:
    """共享知识。fingerprint用于去重。category: bug_fix/architecture/performance/api/config/pattern/other"""
    err = _require_uuid(session_id)
    if err: return err
    ts = _now()
    with _lock:
        _auto_cleanup(session_id)
        kn = _state.setdefault("knowledge", {})
        if fingerprint in kn:
            kn[fingerprint].update({"text": text, "category": category, "updated_at": ts, "updated_by": session_id, "last_referenced": ts})
            _save_state()
            return f"UPDATED: {fingerprint} (confidence={kn[fingerprint].get('confidence', 0.5)})"
        kn[fingerprint] = {
            "text": text, "category": category, "confidence": 0.5, "original_confidence": 0.5,
            "created_at": ts, "created_by": session_id, "updated_at": ts, "updated_by": session_id,
            "last_referenced": ts, "confirmations": 0, "refutations": 0, "status": "active",
        }
        _save_state()
    _append_event(f"{ts} | {session_id} | KNOWLEDGE_SHARED | fp={fingerprint} cat={category}")
    return f"SHARED: {fingerprint} (confidence=0.5, category={category})"


@mcp.tool()
def bb_search_knowledge(query: str, category: str = "") -> str:
    """搜索知识库。query匹配fingerprint/text，category可选过滤。"""
    ts = _now()
    with _lock:
        _auto_cleanup()
        kn = _state.get("knowledge", {})
        q = query.lower()
        results = []
        for fp, k in kn.items():
            if k.get("status") == "archived":
                continue
            if category and k.get("category", "") != category:
                continue
            if q in fp.lower() or q in k.get("text", "").lower():
                results.append((fp, k))
                k["last_referenced"] = ts
        if not results:
            return f"No knowledge found for '{query}'"
        results.sort(key=lambda x: -x[1].get("confidence", 0))
        _save_state()
        lines = [f"Found {len(results)} entries for '{query}':"]
        for fp, k in results[:20]:
            conf = k.get("confidence", 0)
            cat = k.get("category", "")
            text = k.get("text", "")[:80]
            lines.append(f"  [{conf:.1f}] ({cat}) {fp}")
            lines.append(f"       {text}")
            if conf >= SOLIDIFY_THRESHOLD:
                lines.append(f"       ★ HIGH CONFIDENCE - 建议写入永久记忆")
        return "\n".join(lines)


@mcp.tool()
def bb_validate_knowledge(session_id: str, fingerprint: str, verdict: str, note: str = "") -> str:
    """验证知识。verdict: confirmed(+0.2)/refuted(-0.5)/observed(+0.1)"""
    err = _require_uuid(session_id)
    if err: return err
    ts = _now()
    with _lock:
        _auto_cleanup(session_id)
        kn = _state.get("knowledge", {})
        if fingerprint not in kn:
            return f"NOT_FOUND: {fingerprint}"
        k = kn[fingerprint]
        if verdict == "confirmed":
            k["confidence"] = min(1.0, k.get("confidence", 0.5) + 0.2)
            k["confirmations"] = k.get("confirmations", 0) + 1
        elif verdict == "refuted":
            k["confidence"] = max(0.0, k.get("confidence", 0.5) - 0.5)
            k["refutations"] = k.get("refutations", 0) + 1
        elif verdict == "observed":
            k["confidence"] = min(1.0, k.get("confidence", 0.5) + 0.1)
        else:
            return f"INVALID verdict: {verdict}. Use: confirmed/refuted/observed"
        k["last_referenced"] = ts
        k["updated_at"] = ts
        _save_state()
        conf = k["confidence"]
        extra = ""
        if conf >= SOLIDIFY_THRESHOLD:
            extra = f" ★ HIGH CONFIDENCE ({conf:.1f}) - 建议写入永久记忆"
        elif conf <= 0.2:
            extra = f" ⚠ LOW CONFIDENCE ({conf:.1f}) - 即将被归档"
    _append_event(f"{ts} | {session_id} | KNOWLEDGE_{verdict.upper()} | fp={fingerprint} note={note}")
    return f"VALIDATED: {fingerprint} verdict={verdict} confidence={conf:.2f}{extra}"


@mcp.tool()
def bb_get_recent_knowledge(hours: int = 48) -> str:
    """获取最近N小时的知识、决策、Bug模式。新session启动时调用。"""
    now = _now_dt()
    cutoff = now - timedelta(hours=hours)
    with _lock:
        _auto_cleanup()
        lines = [f"=== Recent Knowledge (last {hours}h) ==="]

        recent_kn = []
        for fp, k in _state.get("knowledge", {}).items():
            if k.get("status") == "archived":
                continue
            try:
                created = datetime.fromisoformat(k.get("created_at", "").replace("Z", "+00:00"))
                if created >= cutoff:
                    recent_kn.append((fp, k))
            except (ValueError, KeyError):
                recent_kn.append((fp, k))
        if recent_kn:
            lines.append(f"\nKnowledge ({len(recent_kn)}):")
            for fp, k in sorted(recent_kn, key=lambda x: -x[1].get("confidence", 0)):
                lines.append(f"  [{k.get('confidence', 0):.1f}] ({k.get('category', '')}) {fp}: {k.get('text', '')[:80]}")

        recent_dec = _state.get("decisions", [])
        if recent_dec:
            lines.append(f"\nDecisions ({len(recent_dec)}):")
            for d in recent_dec:
                lines.append(f"  - {d.get('decision', '')[:80]}")
                if d.get("rationale"):
                    lines.append(f"    Why: {d['rationale'][:60]}")

        recent_bp = _state.get("bug_patterns", [])
        if recent_bp:
            lines.append(f"\nBug Patterns ({len(recent_bp)}):")
            for b in recent_bp:
                lines.append(f"  - {b.get('pattern', '')[:60]}")
                lines.append(f"    Fix: {b.get('fix', '')[:60]}")

        if not recent_kn and not recent_dec and not recent_bp:
            lines.append("\n(no recent knowledge)")

        return "\n".join(lines)


@mcp.tool()
def bb_share_decision(session_id: str, decision: str, rationale: str = "") -> str:
    """记录架构/设计决策。永久保留。"""
    err = _require_uuid(session_id)
    if err: return err
    ts = _now()
    with _lock:
        _auto_cleanup(session_id)
        decs = _state.setdefault("decisions", [])
        decs.append({
            "id": f"dec-{len(decs)+1}", "session_id": session_id,
            "decision": decision, "rationale": rationale, "timestamp": ts,
        })
        _save_state()
    _append_event(f"{ts} | {session_id} | DECISION | {decision[:60]}")
    return f"DECISION RECORDED: {decision[:60]}"


@mcp.tool()
def bb_report_bug_pattern(session_id: str, pattern: str, root_cause: str, fix: str) -> str:
    """报告Bug模式/踩坑经验。永久保留。"""
    err = _require_uuid(session_id)
    if err: return err
    ts = _now()
    with _lock:
        _auto_cleanup(session_id)
        bps = _state.setdefault("bug_patterns", [])
        bps.append({
            "id": f"bug-{len(bps)+1}", "session_id": session_id,
            "pattern": pattern, "root_cause": root_cause, "fix": fix, "timestamp": ts,
        })
        _save_state()
    _append_event(f"{ts} | {session_id} | BUG_PATTERN | {pattern[:60]}")
    return f"BUG PATTERN RECORDED: {pattern[:60]}"


# ─── 启动 ───

_load_state()

_decay_thread = threading.Thread(target=_decay_loop, daemon=True)
_decay_thread.start()

_cleanup_thread = threading.Thread(target=_stale_cleanup_loop, daemon=True)
_cleanup_thread.start()

if __name__ == "__main__":
    mcp.run(transport="stdio")