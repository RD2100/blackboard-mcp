# Blackboard MCP

<p align="center">
  <strong>Cross-session coordination server for <a href="https://code.claude.com">Claude Code</a></strong><br>
  File locks · Build locks · Knowledge sharing · Real-time dashboard
</p>

<p align="center">
  <a href="https://github.com/RD2100/blackboard-mcp/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License"></a>
  <img src="https://img.shields.io/badge/python-3.10+-green.svg" alt="Python">
  <img src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg" alt="Platform">
</p>

---

## The Problem

You open 3 Claude Code windows to work on different tasks. Suddenly:

- **File conflict** — Two sessions edit the same file simultaneously, one overwrites the other
- **Build collision** — Two sessions run `./gradlew` at the same time, Gradle daemons pile up, OOM
- **Duplicated work** — One session fixes a bug that another session already fixed yesterday
- **No visibility** — You can't see what other sessions are doing

**Blackboard solves this.** It acts as a registry center where every session registers, claims files, acquires build locks, and shares knowledge.

## Features

| Feature | Description |
|---------|-------------|
| **Session Registry** | Auto-register via SessionStart hook, heartbeat monitoring, stale cleanup |
| **File Conflict Detection** | Claim files before editing, detect conflicts across sessions |
| **Build Lock Coordination** | Only one session compiles at a time, with 10min timeout |
| **Shared Knowledge Base** | Share decisions, bug patterns, and knowledge with confidence scoring + decay |
| **Real-time Dashboard** | tkinter GUI with session list, sorting, health checks, and knowledge viewer |
| **Crash Recovery** | 3-level state recovery: state.json → .bak → events.log replay |
| **MCP Restart Resilient** | Sessions survive MCP server restarts, no data loss |

## Quick Start

### 1. Clone & Install

```bash
git clone https://github.com/RD2100/blackboard-mcp.git
cd blackboard-mcp
pip install mcp  # only dependency
```

### 2. Register with Claude Code

```bash
claude mcp add -s user blackboard -- python "/path/to/blackboard-mcp/server.py"
```

### 3. Add SessionStart Hook

Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash \"/path/to/blackboard-mcp/scripts/bb-hook-session-start.sh\""
          }
        ]
      }
    ]
  }
}
```

### 4. Add Rules (Recommended)

Copy `rules/blackboard-protocol.md` to your project's `.claude/rules/` or `~/.claude/rules/`.

### 5. Open Dashboard (Optional)

**Windows:** Double-click `scripts/bb-monitor.bat`

**macOS/Linux:**
```bash
python scripts/bb-monitor-gui.py /path/to/your/project
```

## MCP Tools

### Session Management

| Tool | Description |
|------|-------------|
| `bb_register(name, task, session_id)` | Register/update session |
| `bb_deregister(session_id)` | Unregister session, release all files |
| `bb_heartbeat(session_id)` | Refresh session heartbeat |
| `bb_status()` | View full system status with health checks |
| `bb_session_files(session_id)` | List files claimed by a session |

### File Coordination

| Tool | Description |
|------|-------------|
| `bb_claim_file(session_id, file_path)` | Claim file ownership (returns CONFLICT/CLAIMED/OWN) |
| `bb_release_file(session_id, file_path)` | Release file ownership |
| `bb_check_conflicts(file_paths)` | Check if files are claimed by others |

### Build Coordination

| Tool | Description |
|------|-------------|
| `bb_acquire_build_lock(session_id, project_dir)` | Acquire build lock (returns ACQUIRED/CONFLICT) |
| `bb_release_build_lock(session_id, project_dir)` | Release build lock |

### Knowledge Management

| Tool | Description |
|------|-------------|
| `bb_share_knowledge(session_id, fingerprint, category, text)` | Share knowledge entry (deduped by fingerprint) |
| `bb_search_knowledge(query, category?)` | Search knowledge base |
| `bb_validate_knowledge(session_id, fingerprint, verdict)` | Validate/refute knowledge (affects confidence) |
| `bb_get_recent_knowledge(hours?)` | Get recent knowledge, decisions, and bug patterns |
| `bb_share_decision(session_id, decision, rationale?)` | Record architecture/design decision |
| `bb_report_bug_pattern(session_id, pattern, root_cause, fix)` | Report bug pattern / pitfall |
| `bb_event(session_id, event_type, details?)` | Log custom event |

## Architecture

```
Session A ──┐
            ├──▶ MCP Server (blackboard) ──▶ state.json (disk)
Session B ──┤         │
            │    ┌────┴────┐
Session C ──┘    │ Cleanup  │  60s stale cleanup thread
                 │ Decay    │  hourly knowledge confidence decay
                 │ Reload   │  detect hook-written state changes
                 └─────────┘

Dashboard ──▶ reads state.json directly (2s refresh)
```

### Knowledge Lifecycle

```
Share (conf=0.5) → Validate confirmed (+0.2) → ... → conf ≥ 0.8 → ★ HIGH CONFIDENCE
                  → Validate refuted  (-0.5) → ... → conf ≤ 0.2 → archived
                  → 30 days no reference → confidence decays (half-life)
                  → 60 days no reference → archived
```

## Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `STALE_TIMEOUT_MINUTES` | 30 | Minutes before inactive session is marked stale |
| `BUILD_LOCK_TIMEOUT_MINUTES` | 10 | Minutes before build lock auto-releases |
| `KNOWLEDGE_HALF_LIFE_DAYS` | 30 | Days before knowledge confidence decays by half |
| `KNOWLEDGE_ARCHIVE_DAYS` | 60 | Days before knowledge is archived |

## Privacy

All data is stored locally in `.claude/blackboard/` within your project directory. **No data is sent externally.** The following files should NOT be committed to version control:

- `state.json` / `state.json.bak` — runtime session state
- `events.log` — event history

## Comparison with Alternatives

| Feature | Blackboard | [ccsession](https://github.com/TimEvans/ccsession) | [claude-presence](https://github.com/garniergeorges/claude-presence) |
|---------|-----------|---------|------------------|
| Session registry | ✅ | ✅ | ✅ |
| File conflict detection | ✅ | ❌ | ❌ |
| Build lock coordination | ✅ | ❌ | ❌ |
| Shared knowledge base | ✅ | ❌ | ❌ |
| Confidence scoring + decay | ✅ | ❌ | ❌ |
| Real-time dashboard | ✅ | ❌ | ❌ |
| Crash recovery | ✅ | ❌ | ❌ |
| MCP restart resilient | ✅ | ❌ | ❌ |

## Related

- [anthropics/claude-code#24798](https://github.com/anthropics/claude-code/issues/24798) — Inter-session communication for multi-Claude workflows
- [anthropics/claude-code#47997](https://github.com/anthropics/claude-code/issues/47997) — Multi-session coordination feature request
- [Claude Code Agent Teams](https://code.claude.com/docs/en/agent-teams) — Official in-process multi-agent (single session)

## License

[MIT](LICENSE)
