# clux

Session manager for [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Named sessions, fast switching, full context restored — even after terminal death.

![clux demo](assets/demo.gif)

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/zackham/clux/master/install.sh | bash
```

Or directly: `uv tool install git+https://github.com/zackham/clux.git`

Requires: Python 3.12+, tmux, [Claude CLI](https://docs.anthropic.com/en/docs/claude-code)

## Usage

```bash
clux                      # open TUI
clux new api              # create session in current directory
clux attach api           # resume (survives terminal death)
clux close api            # archive + kill in one step
```

Sessions are scoped to directories — "api" in `~/work/foo` and "api" in `~/work/bar` are separate. Context is preserved across detach, terminal close, and reboot via `claude --resume`.

### Tmux menu (`Ctrl-b j`)

From inside any clux session:

- **New Session** — create a new session in the same project
- **Archive & Close** — archive the session and kill tmux
- **Next Session** — switch to the next session in the same project
- **Open clux** — launch the TUI as a popup overlay

Injected automatically. No tmux config needed. `j` is unbound in default tmux.

### TUI keybindings

`n` new (in selected directory) · `enter`/`o` open · `a` archive · `d` delete · `k` kill · `s` show archived · `tab`/`shift+tab` jump directories · `q` quit

## Programmatic API

```bash
clux list --json                          # all sessions as JSON
clux prompt api "run the tests"           # send prompt, stream response
clux prompt api "deploy" --dir ~/work/api # target a specific directory
clux prompt api "status" --json           # raw NDJSON (cost, tool use, session ID)
```

`clux prompt` streams NDJSON events (text, tool use, cost) in real-time to stdout — built for scripting, bots, and CI.

## Config

`~/.config/clux/config.toml`:

```toml
yolo_mode = true          # --dangerously-skip-permissions by default
claude_command = "claude" # custom binary path
```

## License

MIT
