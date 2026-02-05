"""CLI interface for clux."""

import json
import os
import sys
import time
from pathlib import Path

import click

from . import claude
from . import tmux
from .config import Config
from .db import Session, SessionDB, validate_session_name, make_tmux_name


def get_cwd() -> str:
    """Get current working directory as string."""
    return str(Path.cwd().resolve())


def format_session_line(session: Session) -> str:
    """Format a session for display."""
    status_icon = {
        "active": click.style("●", fg="green"),
        "detached": click.style("○", fg="yellow"),
        "idle": click.style("○", fg="white"),
        "archived": click.style("◌", fg="bright_black"),
    }.get(session.status, "?")

    name = click.style(session.name, bold=True)
    age = click.style(session.age, fg="bright_black")
    status = click.style(session.status, fg="cyan")

    # Show resume indicator if session has a Claude session ID
    resume_indicator = ""
    if session.claude_session_id:
        resume_indicator = click.style(" ↺", fg="blue")

    return f"  {status_icon} {name:<20} {age:<12} {status}{resume_indicator}"


def sync_session_status(db: SessionDB, session: Session) -> Session:
    """Sync session status with actual tmux state."""
    if session.tmux_session:
        if tmux.session_exists(session.tmux_session):
            if tmux.is_attached(session.tmux_session):
                new_status = "active"
            else:
                new_status = "detached"
        else:
            new_status = "idle"

        if new_status != session.status and session.status != "archived":
            db.update_status(session.id, new_status)
            session.status = new_status

    # Detect Claude session ID if missing
    if not session.claude_session_id:
        _try_capture_claude_id(db, session)

    return session


def _try_capture_claude_id(db: SessionDB, session: Session) -> None:
    """Try to detect and store a Claude session ID from disk."""
    from datetime import datetime, timezone

    try:
        created_dt = datetime.fromisoformat(session.created_at)
        if created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=timezone.utc)
        created_ts = created_dt.timestamp()
    except Exception:
        return

    claude_id = claude.find_session_after_time(session.working_directory, created_ts)
    if claude_id:
        db.update_claude_session_id(session.id, claude_id)
        session.claude_session_id = claude_id


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx: click.Context) -> None:
    """clux - Claude Code session manager."""
    if ctx.invoked_subcommand is None:
        # Default: launch TUI
        from .tui import CluxApp
        from .tui.app import run_tui
        run_tui()


@main.command("new")
@click.argument("name")
@click.option("--safe", is_flag=True, help="Disable YOLO mode for this session")
def new_cmd(name: str, safe: bool) -> None:
    """Create a new Claude session."""
    # Validate session name
    is_valid, error = validate_session_name(name)
    if not is_valid:
        click.echo(f"Invalid session name: {error}", err=True)
        sys.exit(1)

    db = SessionDB()
    config = Config.load()
    cwd = get_cwd()

    # Check if session already exists
    existing = db.get_session(name, cwd)
    if existing:
        click.echo(f"Session '{name}' already exists in this directory.", err=True)
        click.echo(f"Use 'clux attach {name}' to resume it.", err=True)
        sys.exit(1)

    # Create tmux session name (includes dir hash for uniqueness)
    tmux_name = make_tmux_name(name, cwd)

    # Check if tmux session already exists (orphaned)
    if tmux.session_exists(tmux_name):
        click.echo(f"tmux session '{tmux_name}' already exists. Killing it.", err=True)
        tmux.kill_session(tmux_name)

    # Create database record
    session = db.create_session(name, cwd, tmux_name)
    click.echo(f"Created session: {name}")

    # Create tmux session
    if not tmux.create_session(tmux_name, cwd):
        click.echo("Failed to create tmux session", err=True)
        db.delete_session(session.id)
        sys.exit(1)

    # Record time before launching Claude (for session detection)
    launch_time = time.time()

    # Send claude command
    claude_cmd = " ".join(config.get_claude_command(safe=safe))
    tmux.send_keys(tmux_name, claude_cmd)

    # Update status and attach
    db.update_status(session.id, "active")
    click.echo(f"Attaching to tmux session: {tmux_name}")
    exit_code = tmux.attach_session(tmux_name)

    # After detach, try to capture Claude session ID
    claude_session_id = claude.find_session_after_time(cwd, launch_time)
    if claude_session_id:
        db.update_claude_session_id(session.id, claude_session_id)
        click.echo(f"Captured Claude session: {claude_session_id[:8]}...")

    # Update status based on tmux state
    session = db.get_session_by_id(session.id)
    if session:
        sync_session_status(db, session)


@main.command("list")
@click.option("--all", "include_all", is_flag=True, help="Include archived sessions")
@click.option("--here", is_flag=True, help="Only show sessions for current directory")
@click.option("--json", "json_mode", is_flag=True, help="Output as JSON")
def list_cmd(include_all: bool = False, here: bool = False, json_mode: bool = False) -> None:
    """List all sessions."""
    db = SessionDB()
    cwd = get_cwd() if here else None
    sessions = db.list_sessions(include_archived=include_all, working_directory=cwd)

    # Sync all session statuses
    sessions = [sync_session_status(db, s) for s in sessions]

    # JSON output mode
    if json_mode:
        output = [
            {
                "id": s.id,
                "name": s.name,
                "working_directory": s.working_directory,
                "status": s.status,
                "claude_session_id": s.claude_session_id,
                "last_activity": s.last_activity,
            }
            for s in sessions
        ]
        click.echo(json.dumps(output))
        return

    if not sessions:
        click.echo("No sessions found.")
        if not include_all:
            click.echo("Use --all to include archived sessions.")
        return

    # Group by directory
    by_dir: dict[str, list[Session]] = {}
    for session in sessions:
        if session.working_directory not in by_dir:
            by_dir[session.working_directory] = []
        by_dir[session.working_directory].append(session)

    # Sort directories: current first, then alphabetically
    current = get_cwd()
    dirs = sorted(by_dir.keys(), key=lambda d: (d != current, d))

    for directory in dirs:
        dir_sessions = by_dir[directory]
        # Shorten path for display
        display_dir = directory.replace(str(Path.home()), "~")
        is_current = directory == current

        if is_current:
            click.echo(click.style(f"\n{display_dir} (current)", fg="green", bold=True))
        else:
            click.echo(click.style(f"\n{display_dir}", fg="blue"))

        for session in dir_sessions:
            click.echo(format_session_line(session))


@main.command("attach")
@click.argument("name")
@click.option("--safe", is_flag=True, help="Use safe mode if creating new session")
def attach_cmd(name: str, safe: bool) -> None:
    """Attach to an existing session."""
    db = SessionDB()
    config = Config.load()
    cwd = get_cwd()

    session = db.get_session(name, cwd)
    if not session:
        click.echo(f"Session '{name}' not found in current directory.", err=True)
        click.echo("Use 'clux list --all' to see all sessions.", err=True)
        sys.exit(1)

    if session.status == "archived":
        click.echo(f"Session '{name}' is archived. Use 'clux restore {name}' first.", err=True)
        sys.exit(1)

    # Sync status
    session = sync_session_status(db, session)
    launch_time = time.time()

    if session.tmux_session and tmux.session_exists(session.tmux_session):
        # Attach to existing tmux session
        db.update_status(session.id, "active")
        click.echo(f"Attaching to: {session.tmux_session}")
        tmux.attach_session(session.tmux_session)
    elif session.claude_session_id:
        # Resume with --resume
        click.echo(f"Resuming Claude session: {session.claude_session_id[:8]}...")
        tmux_name = session.tmux_session or make_tmux_name(name, session.working_directory)

        if not tmux.create_session(tmux_name, session.working_directory):
            click.echo("Failed to create tmux session", err=True)
            sys.exit(1)

        claude_cmd = config.get_claude_command(safe=safe)
        claude_cmd.extend(["--resume", session.claude_session_id])
        tmux.send_keys(tmux_name, " ".join(claude_cmd))

        db.update_status(session.id, "active")
        tmux.attach_session(tmux_name)
    else:
        # No way to resume, start fresh
        click.echo(f"No Claude session to resume. Starting fresh.")
        tmux_name = session.tmux_session or make_tmux_name(name, session.working_directory)

        if not tmux.create_session(tmux_name, session.working_directory):
            click.echo("Failed to create tmux session", err=True)
            sys.exit(1)

        claude_cmd = " ".join(config.get_claude_command(safe=safe))
        tmux.send_keys(tmux_name, claude_cmd)

        db.update_status(session.id, "active")
        tmux.attach_session(tmux_name)

    # After detach, try to capture/update Claude session ID
    session = db.get_session_by_id(session.id)
    if session:
        if not session.claude_session_id:
            claude_session_id = claude.find_session_after_time(cwd, launch_time)
            if claude_session_id:
                db.update_claude_session_id(session.id, claude_session_id)
        sync_session_status(db, session)


@main.command("archive")
@click.argument("name")
def archive_cmd(name: str) -> None:
    """Archive a session."""
    db = SessionDB()
    cwd = get_cwd()

    session = db.get_session(name, cwd)
    if not session:
        click.echo(f"Session '{name}' not found.", err=True)
        sys.exit(1)

    # Kill tmux session if running
    if session.tmux_session and tmux.session_exists(session.tmux_session):
        tmux.kill_session(session.tmux_session)

    db.update_status(session.id, "archived")
    click.echo(f"Archived: {name}")


@main.command("restore")
@click.argument("name")
def restore_cmd(name: str) -> None:
    """Restore an archived session."""
    db = SessionDB()
    cwd = get_cwd()

    session = db.get_session(name, cwd)
    if not session:
        click.echo(f"Session '{name}' not found.", err=True)
        sys.exit(1)

    if session.status != "archived":
        click.echo(f"Session '{name}' is not archived.", err=True)
        sys.exit(1)

    db.restore_session(session.id)
    click.echo(f"Restored: {name}")


@main.command("kill")
@click.argument("name")
def kill_cmd(name: str) -> None:
    """Kill a session's tmux process (keeps session resumable)."""
    db = SessionDB()
    cwd = get_cwd()

    session = db.get_session(name, cwd)
    if not session:
        click.echo(f"Session '{name}' not found.", err=True)
        sys.exit(1)

    if not session.tmux_session or not tmux.session_exists(session.tmux_session):
        click.echo(f"Session '{name}' has no running tmux process.", err=True)
        sys.exit(1)

    # Capture Claude session ID before killing if missing
    if not session.claude_session_id:
        _try_capture_claude_id(db, session)
        if session.claude_session_id:
            click.echo(f"Captured Claude session: {session.claude_session_id[:8]}...")

    tmux.kill_session(session.tmux_session)
    db.update_status(session.id, "idle")
    click.echo(f"Killed: {name}")


@main.command("close")
@click.argument("name", required=False)
@click.option("--tmux-name", help="Look up session by tmux session name (used by menu)")
def close_cmd(name: str | None, tmux_name: str | None) -> None:
    """Archive a session and kill its tmux (one-step cleanup).

    Can be called by name (from current directory) or by --tmux-name
    (used by the prefix+X tmux menu).
    """
    db = SessionDB()

    if tmux_name:
        # Look up by tmux session name (from menu keybinding)
        if not tmux_name.startswith("clux-"):
            click.echo("Not a clux session.", err=True)
            sys.exit(1)
        session = db.get_session_by_tmux_name(tmux_name)
    elif name:
        cwd = get_cwd()
        session = db.get_session(name, cwd)
    else:
        click.echo("Provide a session name or --tmux-name.", err=True)
        sys.exit(1)

    if not session:
        click.echo("Session not found.", err=True)
        sys.exit(1)

    # Capture Claude session ID before killing
    if not session.claude_session_id:
        _try_capture_claude_id(db, session)

    # Kill tmux session if running
    if session.tmux_session and tmux.session_exists(session.tmux_session):
        tmux.kill_session(session.tmux_session)

    # Archive
    db.update_status(session.id, "archived")
    click.echo(f"Archived and closed: {session.name}")


@main.command("new-here")
@click.argument("name")
@click.option("--tmux-name", required=True, help="Current tmux session name (used by menu)")
@click.option("--safe", is_flag=True, help="Disable YOLO mode for this session")
def new_here_cmd(name: str, tmux_name: str, safe: bool) -> None:
    """Create a new session in the same directory as the current session."""
    db = SessionDB()
    config = Config.load()

    if not tmux_name.startswith("clux-"):
        tmux.display_message("Not a clux session")
        return

    session = db.get_session_by_tmux_name(tmux_name)
    if not session:
        tmux.display_message("Session not found")
        return

    working_dir = session.working_directory

    # Validate name
    is_valid, error = validate_session_name(name)
    if not is_valid:
        tmux.display_message(f"Invalid name: {error}")
        return

    # Check if session already exists
    existing = db.get_session(name, working_dir)
    if existing:
        tmux.display_message(f"Session '{name}' already exists")
        return

    # Create new session
    new_tmux_name = make_tmux_name(name, working_dir)

    if tmux.session_exists(new_tmux_name):
        tmux.kill_session(new_tmux_name)

    new_session = db.create_session(name, working_dir, new_tmux_name)

    if not tmux.create_session(new_tmux_name, working_dir):
        tmux.display_message("Failed to create tmux session")
        db.delete_session(new_session.id)
        return

    claude_cmd = " ".join(config.get_claude_command(safe=safe))
    tmux.send_keys(new_tmux_name, claude_cmd)

    db.update_status(new_session.id, "active")

    # Switch to the new session
    if tmux.switch_client(new_tmux_name):
        tmux.display_message(f"New session: {name}")
    else:
        tmux.display_message(f"Created but failed to switch: {name}")


@main.command("next")
@click.option("--tmux-name", help="Current tmux session name (used by menu)")
def next_cmd(tmux_name: str | None) -> None:
    """Switch to the next session in the same project."""
    db = SessionDB()
    config = Config.load()

    # Find current session
    if tmux_name:
        if not tmux_name.startswith("clux-"):
            tmux.display_message("Not a clux session")
            return
        session = db.get_session_by_tmux_name(tmux_name)
    else:
        # Try to detect from environment
        tmux.display_message("Use --tmux-name or run from the clux menu")
        return

    if not session:
        tmux.display_message("Session not found")
        return

    # Get all non-archived sessions in same directory
    sessions = db.list_sessions(working_directory=session.working_directory)

    if len(sessions) <= 1:
        tmux.display_message("No other sessions in this project")
        return

    # Find current session index
    current_idx = None
    for i, s in enumerate(sessions):
        if s.id == session.id:
            current_idx = i
            break

    if current_idx is None:
        tmux.display_message("Current session not found in list")
        return

    # Get next session (wrap around)
    next_idx = (current_idx + 1) % len(sessions)
    next_session = sessions[next_idx]

    # Ensure next session has a running tmux
    next_tmux = next_session.tmux_session or make_tmux_name(
        next_session.name, next_session.working_directory
    )

    if not tmux.session_exists(next_tmux):
        # Create tmux session and launch Claude
        if not tmux.create_session(next_tmux, next_session.working_directory):
            tmux.display_message("Failed to create tmux session")
            return

        if next_session.claude_session_id:
            claude_cmd = config.get_claude_command()
            claude_cmd.extend(["--resume", next_session.claude_session_id])
            tmux.send_keys(next_tmux, " ".join(claude_cmd))
        else:
            claude_cmd = " ".join(config.get_claude_command())
            tmux.send_keys(next_tmux, claude_cmd)

        db.update_status(next_session.id, "active")

    # Switch to the next session
    if tmux.switch_client(next_tmux):
        tmux.display_message(f"Switched to: {next_session.name}")
    else:
        tmux.display_message(f"Failed to switch to: {next_session.name}")


@main.command("delete")
@click.argument("name")
@click.option("--force", is_flag=True, help="Delete without confirmation")
def delete_cmd(name: str, force: bool) -> None:
    """Permanently delete a session."""
    db = SessionDB()
    cwd = get_cwd()

    session = db.get_session(name, cwd)
    if not session:
        click.echo(f"Session '{name}' not found.", err=True)
        sys.exit(1)

    if not force:
        if not click.confirm(f"Permanently delete session '{name}'?"):
            return

    # Kill tmux session if running
    if session.tmux_session and tmux.session_exists(session.tmux_session):
        tmux.kill_session(session.tmux_session)

    db.delete_session(session.id)
    click.echo(f"Deleted: {name}")


@main.command("status")
def status_cmd() -> None:
    """Show status of current directory sessions."""
    db = SessionDB()
    cwd = get_cwd()

    sessions = db.list_sessions(working_directory=cwd)
    if not sessions:
        click.echo("No sessions in current directory.")
        return

    # Check if we're inside a tmux session that matches one of ours
    current_tmux = os.environ.get("TMUX_PANE", "")
    # This is a simplified check - could be more robust

    for session in sessions:
        session = sync_session_status(db, session)
        click.echo(format_session_line(session))


# =============================================================================
# Prompt command (non-interactive)
# =============================================================================


@main.command("prompt")
@click.argument("name")
@click.argument("message")
@click.option("--dir", "directory", type=click.Path(exists=True), help="Working directory")
@click.option("--json", "json_mode", is_flag=True, help="Output raw stream-json")
@click.option("--timeout", default=900, help="Timeout in seconds")
@click.option("--safe", is_flag=True, help="Disable YOLO mode")
def prompt_cmd(
    name: str, message: str, directory: str | None, json_mode: bool, timeout: int, safe: bool
) -> None:
    """Send a prompt to a session (non-interactive)."""
    from .prompt import run_prompt

    cwd = directory or get_cwd()
    try:
        result = run_prompt(name, message, cwd, json_mode=json_mode, safe=safe, timeout=timeout)
        if result.error:
            click.echo(f"Error: {result.error}", err=True)
        sys.exit(result.exit_code)
    except ValueError as e:
        click.echo(str(e), err=True)
        sys.exit(1)


# =============================================================================
# Single-letter aliases
# =============================================================================


@main.command("n", hidden=True)
@click.argument("name")
@click.option("--safe", is_flag=True)
@click.pass_context
def n_cmd(ctx: click.Context, name: str, safe: bool) -> None:
    """Alias for 'new'."""
    ctx.invoke(new_cmd, name=name, safe=safe)


@main.command("a", hidden=True)
@click.argument("name")
@click.option("--safe", is_flag=True)
@click.pass_context
def a_cmd(ctx: click.Context, name: str, safe: bool) -> None:
    """Alias for 'attach'."""
    ctx.invoke(attach_cmd, name=name, safe=safe)


@main.command("l", hidden=True)
@click.option("--all", "include_all", is_flag=True)
@click.option("--here", is_flag=True)
@click.option("--json", "json_mode", is_flag=True)
@click.pass_context
def l_cmd(ctx: click.Context, include_all: bool, here: bool, json_mode: bool) -> None:
    """Alias for 'list'."""
    ctx.invoke(list_cmd, include_all=include_all, here=here, json_mode=json_mode)


@main.command("k", hidden=True)
@click.argument("name")
@click.pass_context
def k_cmd(ctx: click.Context, name: str) -> None:
    """Alias for 'kill'."""
    ctx.invoke(kill_cmd, name=name)


@main.command("d", hidden=True)
@click.argument("name")
@click.option("--force", is_flag=True)
@click.pass_context
def d_cmd(ctx: click.Context, name: str, force: bool) -> None:
    """Alias for 'delete'."""
    ctx.invoke(delete_cmd, name=name, force=force)


@main.command("s", hidden=True)
@click.pass_context
def s_cmd(ctx: click.Context) -> None:
    """Alias for 'status'."""
    ctx.invoke(status_cmd)


@main.command("x", hidden=True)
@click.argument("name", required=False)
@click.option("--tmux-name")
@click.pass_context
def x_cmd(ctx: click.Context, name: str | None, tmux_name: str | None) -> None:
    """Alias for 'close'."""
    ctx.invoke(close_cmd, name=name, tmux_name=tmux_name)


@main.command("p", hidden=True)
@click.argument("name")
@click.argument("message")
@click.option("--dir", "directory", type=click.Path(exists=True))
@click.option("--json", "json_mode", is_flag=True)
@click.option("--timeout", default=900)
@click.option("--safe", is_flag=True)
@click.pass_context
def p_cmd(
    ctx: click.Context,
    name: str,
    message: str,
    directory: str | None,
    json_mode: bool,
    timeout: int,
    safe: bool,
) -> None:
    """Alias for 'prompt'."""
    ctx.invoke(
        prompt_cmd,
        name=name,
        message=message,
        directory=directory,
        json_mode=json_mode,
        timeout=timeout,
        safe=safe,
    )


if __name__ == "__main__":
    main()
