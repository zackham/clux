# clux

Claude Code session manager with tmux integration.

## Install

```bash
cd ~/work/clux
uv sync
ln -sf ~/work/clux/.venv/bin/clux ~/.local/bin/clux
```

## Usage

```bash
clux                    # TUI dashboard
clux new <name>         # create session in cwd, launch claude
clux list [--all]       # list sessions (--all includes archived)
clux attach <name>      # attach to session (resumes with --resume if idle)
clux archive <name>     # archive session
clux restore <name>     # restore archived session
clux delete <name>      # permanently delete
clux status             # show sessions in cwd
```

## TUI Keybindings

| Key | Action |
|-----|--------|
| `n` | New session |
| `Enter` | Attach to selected |
| `a` | Archive selected |
| `r` | Restore selected |
| `d` | Delete selected |
| `R` | Refresh list |
| `q` | Quit |

## Config

`~/.config/clux/config.toml`:

```toml
yolo_mode = true          # --dangerously-skip-permissions by default
claude_command = "claude"
```

Use `--safe` flag to disable YOLO mode for a single session.

## Data

- Registry: `~/.local/share/clux/sessions.db` (SQLite WAL)
- Sessions are unique per directory (can have "api-refactor" in both vita and rwgps)

## How it works

1. `clux new foo` creates a tmux session `clux-foo`, launches claude inside
2. After you interact with claude and detach, clux captures the Claude session ID
3. If tmux dies but session ID exists, `clux attach foo` uses `claude --resume <id>`
4. Archive hides sessions; delete removes permanently
