# Blackboard MCP

Cross-session coordination server for [Claude Code](https://code.claude.com) — file locks, build locks, knowledge sharing, and a real-time dashboard.

## Why?

Claude Code runs each session independently. When you open multiple windows, they can't see each other — leading to file conflicts, build collisions, and duplicated work.

Blackboard acts as a **registry center**: every session registers itself, claims files, acquires build locks, and shares knowledge. Other sessions can detect conflicts and coordinate.

## Features

- **Session Registry** — Auto-register via SessionStart hook, heartbeat monitoring, stale cleanup
- **File Conflict Detection** — Claim files before editing, detect conflicts across sessions
- **Build Lock Coordination** — Only one session compiles at a time, with 10min timeout
- **Shared Knowledge Base** — Share decisions, bug patterns, and knowledge with confidence scoring and decay
- **Real-time Dashboard** — tkinter GUI with session list, sorting, health checks, and knowledge viewer
- **Crash Recovery** — 3-level state recovery (state.json → .bak → events.log replay)

## Quick Start

### 1. Install

```bash
# Clone
git clone https://github.com/RD2100/blackboard-mcp.git
cd blackboard-mcp
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
            "command": "bash \"$HOME\"/path/to/blackboard-mcp/scripts/bb-hook-session-start.sh"
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

Double-click `scripts/bb-monitor.bat` (Windows) or run:

```bash
python scripts/bb-monitor-gui.py /path/to/your/project
```

## MCP Tools

| Tool | Description |
|------|-------------|
| `bb_register` | Register/update session (with session_id, name, task) |
| `bb_deregister` | Unregister session |
| `bb_heartbeat` | Refresh session heartbeat |
| `bb_claim_file` | Claim file ownership |
| `bb_release_file` | Release file ownership |
| `bb_check_conflicts` | Check if files are claimed by others |
| `bb_acquire_build_lock` | Acquire build lock for a project |
| `bb_release_build_lock` | Release build lock |
| `bb_status` | View full system status with health checks |
| `bb_session_files` | List files claimed by a session |
| `bb_share_knowledge` | Share knowledge entry |
| `bb_search_knowledge` | Search knowledge base |
| `bb_validate_knowledge` | Validate/refute knowledge (affects confidence) |
| `bb_get_recent_knowledge` | Get recent knowledge entries |
| `bb_share_decision` | Record architecture/design decision |
| `bb_report_bug_pattern` | Report bug pattern / pitfall |
| `bb_event` | Log custom event |

## Architecture

```
Session A ──┐
            ├──▶ MCP Server (blackboard) ──▶ state.json (disk)
Session B ──┤         │
            │    ┌────┴────┐
Session C ──┘    │ Cleanup  │ (60s stale cleanup thread)
                 │ Decay    │ (knowledge confidence decay)
                 │ Reload   │ (detect hook-written state changes)
                 └─────────┘

Dashboard ──▶ reads state.json directly (2s refresh)
```

## Configuration

### Stale Timeout

Sessions with no heartbeat for 30 minutes are marked stale and cleaned up. Configurable in `server.py`:

```python
STALE_TIMEOUT_MINUTES = 30
```

### Build Lock Timeout

Build locks auto-release after 10 minutes. Configurable in `server.py`:

```python
BUILD_LOCK_TIMEOUT_MINUTES = 10
```

## Privacy

Blackboard stores all data locally in `.claude/blackboard/` within your project directory. No data is sent externally. The following files should NOT be committed to version control:

- `state.json` — runtime session state
- `state.json.bak` — backup
- `events.log` — event history

## License

MIT
