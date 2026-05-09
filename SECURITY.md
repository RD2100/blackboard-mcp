# Security Policy

## Reporting Vulnerabilities

If you discover a security vulnerability, please report it privately:

- Open a GitHub Issue with the **security** label
- Or email the maintainer

**Do not** publicly disclose vulnerabilities before a fix is available.

## Known Security Considerations

### 1. Local-only Data
All Blackboard data (state.json, events.log) is stored locally in `.claude/blackboard/` within your project directory. **No data is transmitted externally.** However, this data is readable by any process on your machine. If you work in a shared environment, consider file permissions.

### 2. SessionStart Hook
The hook script (`bb-hook-session-start.sh`) receives input from Claude Code and writes to `state.json`. The script validates session_id format (rejects shell metacharacters) and sanitizes paths (rejects `..` directory traversal). All variables are passed as arguments to Python, never interpolated into code strings.

### 3. MCP Tool Inputs
The MCP server validates inputs:
- File paths: stripped of `./` prefix, checked against file_registry
- Session IDs: used as dictionary keys (no code execution)
- Build lock paths: validated against existing locks
- Knowledge fingerprints: used as dictionary keys (no code execution)

### 4. No Authentication
Blackboard does not implement authentication. Any Claude Code session with the MCP server configured can register, claim files, and share knowledge. This is intentional — Blackboard is designed for single-user, multi-session coordination on a local machine.

If you need multi-user coordination, you should:
- Run Blackboard behind an authenticated MCP proxy
- Or add authentication middleware to `server.py`

### 5. State File Integrity
- Atomic writes via `tempfile.mkstemp` + `os.replace`
- 3-level recovery chain: state.json → .bak → events.log replay
- `.bak` backup created before every write

## Best Practices

- Add `.claude/blackboard/` to your project's `.gitignore` (already in Blackboard's `.gitignore`)
- Do not commit `state.json`, `state.json.bak`, or `events.log` to version control
- In shared environments, set appropriate file permissions on `.claude/blackboard/`