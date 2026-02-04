#!/usr/bin/env bash
set -euo pipefail

REPO="https://github.com/zackham/clux.git"

info()  { printf '\033[0;32m%s\033[0m %s\n' ">" "$1"; }
warn()  { printf '\033[1;33m%s\033[0m %s\n' ">" "$1"; }
error() { printf '\033[0;31m%s\033[0m %s\n' ">" "$1"; exit 1; }

# prerequisites
command -v tmux >/dev/null 2>&1 || warn "tmux not found - clux requires tmux"
command -v claude >/dev/null 2>&1 || warn "claude CLI not found - https://docs.anthropic.com/en/docs/claude-code"

# install
if command -v uv >/dev/null 2>&1; then
    info "installing with uv..."
    uv tool install --force "git+${REPO}"
elif command -v pipx >/dev/null 2>&1; then
    info "installing with pipx..."
    pipx install --force "git+${REPO}"
else
    error "uv or pipx required. install uv: curl -LsSf https://astral.sh/uv/install.sh | sh"
fi

# verify
if command -v clux >/dev/null 2>&1; then
    info "done! run 'clux' to get started."
else
    warn "installed but 'clux' not in PATH - restart your shell or add ~/.local/bin to PATH"
fi
