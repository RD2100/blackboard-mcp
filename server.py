#!/usr/bin/env python3
"""Blackboard MCP Server v4 — 少即是多

精简原则:
  - MCP是唯一的真相来源，无shell脚本直接写state.json
  - 每次工具调用自动清理stale session（>15min无心跳）
  - 每次工具调用自动刷新调用者心跳
  - 原子写入 + .bak备份 + events.log回放恢复

三层知识模型:
  - 临时知识(knowledge): 有可信度+衰减，30天降权/60天归档
  - 决策记录(decisions): 永久保留
  - Bug模式(bug_patterns): 永久保留
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("blackboard")

# ─── 配置 ───

STALE_TIMEOUT_MINUTES = 30  # 30min无心跳才标记stale（给MCP重启恢复窗口）
KNOWLEDGE_HALF_LIFE_DAYS = 30
KNOWLEDGE_ARCHIVE_DAYS = 60
SOLIDIFY_THRESHOLD = 0.8
DECAY_INTERVAL = 3600  # 每小时检查衰减

# ─── 状态管理 ───

_lock = threading.Lock()
_state = {}
_state_file = None
_events_file = None
_state_mtime = 0  # 磁盘state.json最后修改时间，用于检测hook写入


def _discover_state_file():
    """Discover state.json path: project-level > global"""
    for env_var in ["CLAUDE_PROJECT_DIR", "PWD"]:
        d = os.environ.get(env_var, "")
        if d and ".." not in d:  # prevent directory traversal
            p = os.path.join(d, ".claude", "blackboard", "state.json")
            if os.path.isfile(p):
                return os.path.dirname(p), p
    try:
        import subprocess
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            p = os.path.join(r.stdout.strip(), ".claude", "blackboard", "state.json")
            if os.path.isfile(p):
                return os.path.dirname(p), p
    except Exception:
        pass
    bb = os.path.join(os.path.expanduser("~"), ".claude", "blackboard")
    os.makedirs(bb, exist_ok=True)
    return bb, os.path.join(bb, "state.json")


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _is_uuid(s):
    """判断字符串是否是UUID格式"""
    try:
        import uuid as _uuid
        _uuid.UUID(s)
        return True
    except (ValueError, AttributeError):
        return False


def _empty_state():
    return {
        "version": 4,
        "last_updated": _now(),
        "sessions": {},
        "file_registry": {},
        "build_locks": {},
        "knowledge": {},
        "decisions": [],
        "bug_patterns": [],
    }


def _try_load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def _rebuild_from_events(events_path):
    """从events.log回放重建state（最后手段）"""
    state = _empty_state()
    if not os.path.isfile(events_path):
        return state
    try:
        with open(events_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = [p.strip() for p in line.split("|")]
                if len(parts) < 4:
                    continue
                ts, sid, event_type = parts[0], parts[1], parts[2]
                if event_type in ("SESSION_STARTED", "REGISTERED"):
                    if sid not in state["sessions"]:
                        state["sessions"][sid] = {
                            "name": sid, "started_at": ts, "heartbeat": ts,
                            "status": "active", "task": "", "claimed_files": [],
                        }
                elif event_type == "DEREGISTERED":
                    if sid in state["sessions"]:
                        state["sessions"][sid]["status"] = "ended"
        state["last_updated"] = _now()
    except Exception:
        pass
    return state


def _load_state():
    global _state, _state_file, _events_file
    bb_dir, sf = _discover_state_file()
    _state_file = sf
    _events_file = os.path.join(bb_dir, "events.log")
    bak = sf + ".bak"

    # 恢复链: state.json → .bak → events.log → 空
    _state = _try_load_json(_state_file)
    if _state is None and os.path.isfile(bak):
        _state = _try_load_json(bak)
    if _state is None:
        _state = _rebuild_from_events(_events_file)
    if _state is None:
        _state = _empty_state()

    # 确保v4字段
    for key in ("sessions", "file_registry", "build_locks", "knowledge"):
        _state.setdefault(key, {})
    for key in ("decisions", "bug_patterns"):
        _state.setdefault(key, [])

    # MCP重启恢复：把ended session恢复为active（给Claude一个心跳刷新的窗口）
    # 如果session在MCP重启前是active的，重启后应该保留，等心跳超时再清理
    recovered = 0
    for sid, s in _state.get("sessions", {}).items():
        if s.get("status") == "ended":
            # 检查心跳是否在2小时内（可能是MCP重启导致的，不是真的结束）
            hb_str = s.get("heartbeat", "")
            try:
                hb = datetime.fromisoformat(hb_str.replace("Z", "+00:00"))
                if (datetime.now(timezone.utc) - hb).total_seconds() < 7200:  # 2小时内
                    s["status"] = "active"
                    recovered += 1
            except (ValueError, KeyError):
                pass
    if recovered:
        _save_state()


def _save_state():
    """原子写入 + .bak备份"""
    global _state_mtime
    _state["last_updated"] = _now()
    dn = os.path.dirname(_state_file)
    # 先备份当前文件
    if os.path.isfile(_state_file):
        try:
            shutil.copy2(_state_file, _state_file + ".bak")
        except OSError:
            pass
    # 原子写入
    fd, tmp = tempfile.mkstemp(dir=dn, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(_state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, _state_file)
        _state_mtime = os.path.getmtime(_state_file)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _append_event(msg):
    try:
        with open(_events_file, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def _auto_cleanup(caller_sid=None):
    """每次工具调用时: 从磁盘reload(如果hook更新了) + 清理stale + 刷新心跳"""
    global _state, _state_mtime
    # 检测磁盘state.json是否比内存新（SessionStart hook可能写入了）
    if _state_file and os.path.isfile(_state_file):
        try:
            disk_mtime = os.path.getmtime(_state_file)
            if disk_mtime > _state_mtime:
                new_state = _try_load_json(_state_file)
                if new_state is not None:
                    _state = new_state
                    _state_mtime = disk_mtime
                    for key in ("sessions", "file_registry", "build_locks", "knowledge"):
                        _state.setdefault(key, {})
                    for key in ("decisions", "bug_patterns"):
                        _state.setdefault(key, [])
        except OSError:
            pass

    now = datetime.now(timezone.utc)
    stale = []
    for sid, s in _state.get("sessions", {}).items():
        if s.get("status") != "active":
            continue
        hb_str = s.get("heartbeat", "")
        try:
            hb = datetime.fromisoformat(hb_str.replace("Z", "+00:00"))
            if (now - hb).total_seconds() > STALE_TIMEOUT_MINUTES * 60:
                stale.append(sid)
        except (ValueError, KeyError):
            stale.append(sid)

    for sid in stale:
        s = _state["sessions"][sid]
        s["status"] = "stale"
        for fp in list(s.get("claimed_files", [])):
            if _state.get("file_registry", {}).get(fp) == sid:
                del _state["file_registry"][fp]
        # 释放编译锁
        for proj in list(_state.get("build_locks", {})):
            lock = _state["build_locks"][proj]
            owner = lock if isinstance(lock, str) else lock.get("session_id", "")
            if owner == sid:
                del _state["build_locks"][proj]

    # Release expired build locks (>10min held, regardless of session status)
    for proj in list(_state.get("build_locks", {})):
        lock = _state["build_locks"][proj]
        if isinstance(lock, dict) and lock.get("acquired_at"):
            try:
                at = datetime.fromisoformat(lock["acquired_at"].replace("Z", "+00:00"))
                if (now - at).total_seconds() > 600:
                    del _state["build_locks"][proj]
            except (ValueError, KeyError):
                del _state["build_locks"][proj]

    # 刷新调用者心跳
    if caller_sid and caller_sid in _state.get("sessions", {}):
        _state["sessions"][caller_sid]["heartbeat"] = _now()
        _state["sessions"][caller_sid]["status"] = "active"


def _rotate_events(max_lines=500, keep_lines=200):
    """events.log自动rotation"""
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


# ─── 知识衰减后台线程 ───

def _stale_cleanup_loop():
    """后台线程：每60秒清理stale session + 从磁盘reload"""
    while True:
        time.sleep(60)
        try:
            with _lock:
                _auto_cleanup()
                # 清理完stale后保存
                stale_count = sum(1 for s in _state.get("sessions", {}).values() if s.get("status") == "stale")
                if stale_count:
                    for sid in [k for k, v in _state["sessions"].items() if v.get("status") == "stale"]:
                        _state["sessions"][sid]["status"] = "ended"
                    _save_state()
        except Exception:
            pass


def _decay_loop():
    while True:
        time.sleep(DECAY_INTERVAL)
        try:
            with _lock:
                now = datetime.now(timezone.utc)
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
def bb_register(name: str, task: str = "", session_id: str = "") -> str:
    """注册或更新session信息。新session启动时调用。
    session_id: 由SessionStart hook注入的UUID，用于查找已有session。必须使用hook注入的值，不要用自定义名字。
    name: 显示名称（可选，用于覆盖UUID显示）。
    task: 当前任务描述，必须写清楚你在做什么。"""
    with _lock:
        _auto_cleanup(session_id or name)
        _state.setdefault("sessions", {})
        ts = _now()

        # 以session_id为主键查找已有session
        sid = session_id or name
        if sid in _state["sessions"]:
            # 更新已有session（hook已注册，Claude补充task/name）
            existing = _state["sessions"][sid]
            existing["heartbeat"] = ts
            existing["status"] = "active"
            if task:
                existing["task"] = task
            if name and name != sid:
                existing["display_name"] = name
            _save_state()
            _rotate_events()
            _append_event(f"{ts} | {sid} | REGISTERED | task={task}")
            return f"Updated: {sid} (task={existing.get('task','')})"

        # 新session（name作为主键，兼容无session_id的情况）
        if name in _state["sessions"]:
            _state["sessions"][name].update({"name": name, "task": task, "heartbeat": ts, "status": "active"})
        else:
            _state["sessions"][name] = {
                "name": name, "started_at": ts, "heartbeat": ts,
                "status": "active", "task": task, "claimed_files": [],
            }
        _save_state()
        _rotate_events()
    _append_event(f"{ts} | {name} | REGISTERED | task={task}")
    return f"Registered: {name}"


@mcp.tool()
def bb_deregister(session_id: str) -> str:
    """注销session，释放所有文件占用和编译锁。"""
    released = []
    with _lock:
        _auto_cleanup(session_id)
        if session_id in _state.get("sessions", {}):
            for fp in _state["sessions"][session_id].get("claimed_files", []):
                if _state.get("file_registry", {}).get(fp) == session_id:
                    _state["file_registry"].pop(fp, None)
                    released.append(fp)
            for proj in list(_state.get("build_locks", {})):
                lock = _state["build_locks"][proj]
                owner = lock if isinstance(lock, str) else lock.get("session_id", "")
                if owner == session_id:
                    del _state["build_locks"][proj]
            _state["sessions"][session_id]["status"] = "ended"
            _save_state()
    _append_event(f"{_now()} | {session_id} | DEREGISTERED | released={len(released)} files")
    return f"Deregistered: {session_id}, released {len(released)} files"


@mcp.tool()
def bb_heartbeat(session_id: str) -> str:
    """刷新session心跳。15分钟无心跳标记stale。"""
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
    """声明文件占用。冲突返回CONFLICT，已占用返回OWN，成功返回CLAIMED。"""
    file_path = file_path.lstrip("./")
    with _lock:
        _auto_cleanup(session_id)
        fr = _state.setdefault("file_registry", {})
        if file_path in fr:
            owner = fr[file_path]
            if owner == session_id:
                return f"OWN: {file_path}"
            owner_s = _state.get("sessions", {}).get(owner, {})
            if owner_s.get("status") == "active":
                return f"CONFLICT: {file_path} claimed by '{owner_s.get('name', owner)}'"
        fr[file_path] = session_id
        sess = _state.get("sessions", {}).get(session_id, {})
        claimed = sess.setdefault("claimed_files", [])
        if file_path not in claimed:
            claimed.append(file_path)
        _save_state()
    return f"CLAIMED: {file_path}"


@mcp.tool()
def bb_release_file(session_id: str, file_path: str) -> str:
    """释放文件占用。"""
    file_path = file_path.lstrip("./")
    with _lock:
        _auto_cleanup(session_id)
        fr = _state.get("file_registry", {})
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
    """查询文件占用状态。file_paths逗号分隔，空则查全部。"""
    with _lock:
        fr = _state.get("file_registry", {})
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
                name = _state.get("sessions", {}).get(owner, {}).get("name", owner[:16])
                lines.append(f"  {fp} -> {name}")
            else:
                lines.append(f"  {fp} -> FREE")
        return "\n".join(lines)


@mcp.tool()
def bb_acquire_build_lock(session_id: str, project_dir: str = "") -> str:
    """获取编译锁。同一项目只允许1个session编译。返回ACQUIRED或CONFLICT。"""
    with _lock:
        _auto_cleanup(session_id)
        locks = _state.setdefault("build_locks", {})
        existing = locks.get(project_dir)
        if existing:
            owner = existing if isinstance(existing, str) else existing.get("session_id", "")
            if owner != session_id:
                owner_s = _state.get("sessions", {}).get(owner, {})
                if owner_s.get("status") == "active":
                    # 检查锁是否超时(>10min)
                    acquired_at = existing.get("acquired_at", "") if isinstance(existing, dict) else ""
                    if acquired_at:
                        try:
                            at = datetime.fromisoformat(acquired_at.replace("Z", "+00:00"))
                            if (datetime.now(timezone.utc) - at).total_seconds() > 600:
                                # 锁超时，自动释放
                                pass
                            else:
                                return f"CONFLICT: Build lock held by '{owner_s.get('name', owner)}'. Wait or call bb_release_build_lock."
                        except (ValueError, KeyError):
                            pass
                    else:
                        return f"CONFLICT: Build lock held by '{owner_s.get('name', owner)}'. Wait or call bb_release_build_lock."
        locks[project_dir] = {"session_id": session_id, "acquired_at": _now()}
        _save_state()
    _append_event(f"{_now()} | {session_id} | BUILD_LOCK_ACQUIRED | project={project_dir}")
    return f"ACQUIRED: Build lock for {project_dir}"


@mcp.tool()
def bb_release_build_lock(session_id: str, project_dir: str = "") -> str:
    """释放编译锁。编译完成后必须调用。"""
    with _lock:
        _auto_cleanup(session_id)
        locks = _state.get("build_locks", {})
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
    """查看Blackboard完整状态：sessions、文件占用、编译锁、知识统计、健康检查。"""
    with _lock:
        _auto_cleanup()
        lines = ["=== Blackboard Status ==="]

        # Sessions
        active = sum(1 for s in _state["sessions"].values() if s.get("status") == "active")
        stale = sum(1 for s in _state["sessions"].values() if s.get("status") == "stale")
        lines.append(f"\nSessions: {len(_state['sessions'])} total, {active} active, {stale} stale")
        for sid, s in _state["sessions"].items():
            status = s.get("status", "?")
            name = s.get("name", sid[:16])
            task = s.get("task", "")
            files = len(s.get("claimed_files", []))
            lines.append(f"  [{status:>7}] {name} | files={files} | {task}")

        # File registry
        lines.append(f"\nFile claims: {len(_state['file_registry'])}")
        for fp, owner in list(_state["file_registry"].items())[:20]:
            name = _state["sessions"].get(owner, {}).get("name", owner[:16])
            lines.append(f"  {fp} -> {name}")

        # Build locks
        if _state["build_locks"]:
            lines.append(f"\nBuild locks: {len(_state['build_locks'])}")
            for proj, lock in _state["build_locks"].items():
                owner = lock if isinstance(lock, str) else lock.get("session_id", "")
                name = _state["sessions"].get(owner, {}).get("name", owner[:16])
                lines.append(f"  {proj} -> {name}")

        # Knowledge
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
        checks.append(("state.json", "OK" if _state else "CORRUPT"))
        checks.append(("events.log", "OK" if os.path.isfile(_events_file or "") else "MISSING"))
        dead_locks = 0
        for proj, lock in _state.get("build_locks", {}).items():
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
    """记录事件。类型: SESSION_STARTED/ENDED, DISCOVERY, WARNING, ERROR, MILESTONE, DECISION, BLOCKED, UNBLOCKED, CUSTOM。"""
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
    with _lock:
        _auto_cleanup(session_id)
        session = _state.get("sessions", {}).get(session_id)
        if not session:
            return f"Session {session_id} not found"
        claimed = session.get("claimed_files", [])
        if not claimed:
            return f"No files claimed by {session.get('name', session_id)}"
        return f"Files claimed by {session.get('name', session_id)}:\n" + "\n".join(f"  {f}" for f in claimed)


# ─── 知识管理 ───

@mcp.tool()
def bb_share_knowledge(session_id: str, fingerprint: str, category: str, text: str) -> str:
    """共享知识。fingerprint用于去重。category: bug_fix/architecture/performance/api/config/pattern/other"""
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
    now = datetime.now(timezone.utc)
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

        recent_dec = [d for d in _state.get("decisions", []) if True]
        if recent_dec:
            lines.append(f"\nDecisions ({len(recent_dec)}):")
            for d in recent_dec:
                lines.append(f"  - {d.get('decision', '')[:80]}")
                if d.get("rationale"):
                    lines.append(f"    Why: {d['rationale'][:60]}")

        recent_bp = [b for b in _state.get("bug_patterns", []) if True]
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

# 初始化_state_mtime
if _state_file and os.path.isfile(_state_file):
    _state_mtime = os.path.getmtime(_state_file)

_decay_thread = threading.Thread(target=_decay_loop, daemon=True)
_decay_thread.start()

_cleanup_thread = threading.Thread(target=_stale_cleanup_loop, daemon=True)
_cleanup_thread.start()

if __name__ == "__main__":
    mcp.run(transport="stdio")
