# clux

fast session switching for Claude Code, with an API for external integrations.

![clux TUI](assets/screenshot.png)

## why

you're working on multiple projects. each has its own Claude session with accumulated context. switching between them means finding session IDs, remembering which belongs where, manually resuming. clux eliminates that friction.

`clux` in one terminal. arrow to the project you need. enter. you're there with full context restored.

for automation, `clux list --json` and `clux prompt` let you build integrations - telegram bots, editor plugins, CI pipelines - anything that needs to talk to a Claude session programmatically.

## install

```bash
git clone https://github.com/zackham/clux.git
cd clux
uv sync
ln -sf $(pwd)/.venv/bin/clux ~/.local/bin/clux
```

requires: python 3.12+, tmux, claude cli

## quick start

```bash
clux                      # TUI - browse and switch sessions
clux new api              # create session in current directory
clux attach api           # resume session (survives terminal death)
clux close api            # archive + kill tmux in one step
clux next                 # switch to next session in same project
```

TUI keybindings: `n` new, `enter` attach, `a` archive, `d` delete, `q` quit

### tmux menu

inside any clux session, press `ctrl-b j` to open the clux menu:

- **Archive & Close** — archive the session and kill tmux in one step
- **Next Session** — switch to the next session in the same project
- **Open clux** — open the TUI as a popup overlay

the keybinding is injected automatically when sessions are created. no tmux config needed. `j` is unbound in default tmux.

## API for integrations

### list sessions

```bash
clux list --json
```

```json
[
  {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "name": "api",
    "working_directory": "/home/user/work/myproject",
    "status": "idle",
    "claude_session_id": "a1b2c3d4-...",
    "last_activity": "2026-02-01T14:30:00"
  }
]
```

### send prompts programmatically

```bash
clux prompt api "what files did you modify?"
clux prompt api "run the tests" --json    # raw NDJSON output
clux prompt api "deploy" --dir ~/work/api  # specify directory
```

returns exit code 0 on success, streams response to stdout. `--json` gives raw NDJSON for parsing cost, session ID, tool use events.

### example: telegram bot integration

```python
import subprocess
import json

def get_sessions():
    result = subprocess.run(["clux", "list", "--json"], capture_output=True, text=True)
    return json.loads(result.stdout) if result.returncode == 0 else []

def send_to_session(name, directory, message):
    result = subprocess.run(
        ["clux", "prompt", name, message, "--dir", directory],
        capture_output=True, text=True, timeout=900
    )
    return result.stdout if result.returncode == 0 else f"Error: {result.stderr}"

# telegram handler
sessions = get_sessions()
# show inline keyboard picker...
# on selection, route messages through send_to_session()
```

## how it works

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
```

sessions are scoped to directories. "api" in `~/work/foo` and "api" in `~/work/bar` are separate.

## session states

| status | meaning |
|--------|---------|
| `active` | tmux running, attached |
| `detached` | tmux running, not attached |
| `idle` | tmux dead, but claude session ID preserved |
| `archived` | hidden from default views |

## config

`~/.config/clux/config.toml`:

```toml
yolo_mode = true          # --dangerously-skip-permissions by default
claude_command = "claude" # custom binary path
```

## data

- **registry:** `~/.local/share/clux/sessions.db`
- **claude sessions:** `~/.claude/projects/`

clux tracks the mapping between named sessions and Claude's internal session IDs. no conversation content stored.

## license

MIT
