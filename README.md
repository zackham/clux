# clux

tmux-native session management for Claude Code.

## the problem

Claude Code sessions are ephemeral by default. you interact, close the terminal, context is gone. Claude has `--resume` to bring sessions back, but you need the session ID, and you need to track which session belongs to which project. this gets tedious.

clux treats Claude sessions as first-class persistent entities - like tmux treats terminal sessions. create a session, work on it, detach, come back later. the session survives terminal death because clux captures the Claude session ID and stores it.

## mental model

```
clux new api
  ↓
  tmux session created (clux-api)
  ↓
  claude launched inside
  ↓
  you work, then detach (ctrl-b d)
  ↓
  clux captures claude session ID → stored in db
  ↓
later:
  clux attach api
  ↓
  if tmux still alive → attach directly
  if tmux dead but ID exists → spawn new tmux + claude --resume <id>
  if neither → fresh claude session
```

sessions are scoped to directories. you can have "api" in both `~/work/vita` and `~/work/rwgps` without collision.

## install

```bash
cd ~/work/clux
uv sync
ln -sf ~/work/clux/.venv/bin/clux ~/.local/bin/clux
```

requires: python 3.12+, tmux, claude cli

## usage

### interactive

```bash
clux                      # TUI dashboard (default)
clux new api              # create session, launch claude, attach
clux attach api           # resume session (smart - handles tmux death)
clux list                 # show active/detached/idle sessions
clux list --all           # include archived
clux status               # sessions in current directory only
clux archive api          # hide without deleting
clux restore api          # unhide
clux delete api           # permanent removal
```

single-letter aliases for speed:

```bash
clux n api                # new
clux a api                # attach
clux l                    # list
clux d api                # delete
clux s                    # status
```

### non-interactive

send prompts to sessions without attaching:

```bash
clux prompt api "analyze the error in src/main.rs"
clux p api "what files did you modify?" --json
```

useful for automation, piping output, or integration with other tools. `--json` gives you raw NDJSON if you need to parse it yourself.

## TUI

the default command opens a terminal UI:

```
┌─────────────────────────────────────────────────────────────────────┐
│  ~/work/vita                                                        │
│  ├── ● api (active)                                                │
│  ├── ○ refactor (detached)                                         │
│  └── ○ tests (idle) ↺                                              │
│  ~/work/rwgps                                                       │
│  └── ○ mobile (detached)                                           │
├─────────────────────────────────────────────────────────────────────┤
│  [tmux pane preview]                                                │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

status indicators:
- `●` green = active (tmux attached)
- `○` yellow = detached (tmux alive, not attached)
- `○` white = idle (tmux dead, but claude session ID preserved)
- `◌` dim = archived
- `↺` blue = resumable (has claude session ID)

keybindings:

| key | action |
|-----|--------|
| `n` | new session |
| `enter` / `o` | attach |
| `a` | archive |
| `u` | unarchive |
| `d` | delete (with confirmation) |
| `r` | refresh |
| `s` | toggle show archived |
| `tab` | navigate directories |
| `q` | quit |

## config

`~/.config/clux/config.toml`:

```toml
yolo_mode = true          # adds --dangerously-skip-permissions to claude
claude_command = "claude" # custom claude binary path
```

yolo mode is on by default. pass `--safe` to any command to disable for that invocation:

```bash
clux new api --safe       # launches claude without skip-permissions flag
```

## data storage

- **registry:** `~/.local/share/clux/sessions.db` (SQLite with WAL mode)
- **claude sessions:** `~/.claude/projects/` (Claude's native storage)

clux doesn't store conversation content - it just tracks the mapping between your named sessions and Claude's internal session IDs.

## how session capture works

Claude stores sessions at paths like:

```
~/.claude/projects/-home-user-work-myproject/
  ├── a1b2c3d4-e5f6-...  (directory)
  ├── a1b2c3d4-e5f6-....jsonl  (transcript)
```

when you create a session, clux records the launch time. when you detach, it scans this directory for UUIDs modified after launch time. the most recent one is your session ID. this gets stored so `clux attach` can use `claude --resume`.

## session lifecycle

| status | meaning | what attach does |
|--------|---------|------------------|
| active | tmux alive, attached | switch to it |
| detached | tmux alive, not attached | attach to tmux |
| idle | tmux dead, claude ID exists | spawn tmux + `claude --resume` |
| archived | hidden from default views | restore first, then attach |

## integration

`clux list --json` outputs session data for external tools:

```json
[
  {
    "name": "api",
    "directory": "/home/user/work/myproject",
    "status": "idle",
    "claude_session_id": "a1b2c3d4-...",
    "last_activity": "2026-02-01T14:30:00"
  }
]
```

`clux prompt` returns exit code 0 on success, streams text to stdout, and captures cost info from Claude's NDJSON output.

## why tmux

could have used screen, or managed subprocesses directly. tmux won because:

1. already ubiquitous in dev workflows
2. solid session persistence out of the box
3. easy to capture pane content for TUI preview
4. users can attach manually if clux breaks (`tmux attach -t clux-api`)

the `clux-` prefix keeps clux sessions visually separate from your regular tmux sessions.

## limitations

- one session per (name, directory) pair. rename if you need multiple.
- session ID capture depends on Claude's file structure. if Claude changes how it stores sessions, capture breaks.
- non-interactive prompt kills any existing tmux process in the session (by design - prevents interleaving).
- no Windows support. tmux dependency makes this Linux/macOS only.

## license

MIT
