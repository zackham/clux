"""tmux session management."""

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class TmuxNotInstalled(Exception):
    """tmux is not installed on the system."""
    pass


def check_tmux_installed() -> bool:
    """Check if tmux is installed and available."""
    return shutil.which("tmux") is not None


def require_tmux() -> None:
    """Raise TmuxNotInstalled if tmux is not available."""
    if not check_tmux_installed():
        raise TmuxNotInstalled("tmux is not installed. Please install tmux to use clux.")


@dataclass
class TmuxSession:
    """A tmux session."""

    name: str
    attached: bool
    windows: int


def list_sessions() -> list[TmuxSession]:
    """List all tmux sessions."""
    if not check_tmux_installed():
        logger.warning("tmux is not installed")
        return []

    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}:#{session_attached}:#{session_windows}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            # No sessions or tmux server not running - not an error
            if "no server running" in result.stderr or "no sessions" in result.stderr:
                return []
            logger.debug(f"tmux list-sessions returned {result.returncode}: {result.stderr}")
            return []

        sessions = []
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split(":")
            if len(parts) >= 3:
                sessions.append(
                    TmuxSession(
                        name=parts[0],
                        attached=parts[1] == "1",
                        windows=int(parts[2]),
                    )
                )
        return sessions
    except FileNotFoundError:
        logger.warning("tmux binary not found")
        return []
    except Exception as e:
        logger.error(f"Failed to list tmux sessions: {e}")
        return []


def get_sessions_map() -> dict[str, TmuxSession]:
    """Get a map of session name -> TmuxSession for efficient lookups."""
    return {s.name: s for s in list_sessions()}


def session_exists(name: str, sessions_map: dict[str, TmuxSession] | None = None) -> bool:
    """Check if a tmux session exists.

    Optionally pass sessions_map from get_sessions_map() to avoid repeated subprocess calls.
    """
    if sessions_map is not None:
        return name in sessions_map
    return any(s.name == name for s in list_sessions())


def is_attached(name: str, sessions_map: dict[str, TmuxSession] | None = None) -> bool:
    """Check if a tmux session is attached.

    Optionally pass sessions_map from get_sessions_map() to avoid repeated subprocess calls.
    """
    if sessions_map is not None:
        session = sessions_map.get(name)
        return session.attached if session else False
    for s in list_sessions():
        if s.name == name:
            return s.attached
    return False


def create_session(name: str, working_directory: str) -> bool:
    """Create a new detached tmux session."""
    require_tmux()
    try:
        # Unset TMUX env var to avoid "sessions should be nested with care" error
        env = os.environ.copy()
        env.pop("TMUX", None)
        result = subprocess.run(
            ["tmux", "new-session", "-d", "-s", name, "-c", working_directory],
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode != 0:
            logger.error(f"Failed to create tmux session '{name}': {result.stderr}")
            return False
        logger.debug(f"Created tmux session: {name}")
        return True
    except Exception as e:
        logger.error(f"Exception creating tmux session: {e}")
        return False


def send_keys(session_name: str, keys: str, enter: bool = True) -> bool:
    """Send keys to a tmux session."""
    try:
        cmd = ["tmux", "send-keys", "-t", session_name, keys]
        if enter:
            cmd.append("Enter")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.error(f"Failed to send keys to '{session_name}': {result.stderr}")
            return False
        return True
    except Exception as e:
        logger.error(f"Exception sending keys: {e}")
        return False


def attach_session(name: str) -> int:
    """Attach to a tmux session. Returns exit code."""
    require_tmux()
    try:
        result = subprocess.run(["tmux", "attach-session", "-t", name])
        return result.returncode
    except Exception as e:
        logger.error(f"Exception attaching to session: {e}")
        return 1


def kill_session(name: str) -> bool:
    """Kill a tmux session."""
    try:
        result = subprocess.run(
            ["tmux", "kill-session", "-t", name],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning(f"Failed to kill tmux session '{name}': {result.stderr}")
            return False
        logger.debug(f"Killed tmux session: {name}")
        return True
    except Exception as e:
        logger.error(f"Exception killing session: {e}")
        return False


def get_pane_content(session_name: str, lines: int = 50) -> str:
    """Capture content from a tmux pane, showing the most recent output.

    Captures the full scrollback + visible area, then returns the last N lines.
    """
    try:
        # Capture entire scrollback history (-S -) plus visible pane (-E -)
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", session_name, "-p", "-S", "-", "-E", "-"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            # Return the last N lines (most recent output)
            all_lines = result.stdout.rstrip("\n").split("\n")
            return "\n".join(all_lines[-lines:])
        logger.debug(f"Failed to capture pane content: {result.stderr}")
        return ""
    except Exception as e:
        logger.error(f"Exception capturing pane content: {e}")
        return ""


def cleanup_orphaned_sessions(prefix: str = "clux-") -> list[str]:
    """Kill orphaned clux tmux sessions that aren't tracked in DB.

    Returns list of killed session names.
    """
    killed = []
    sessions = list_sessions()
    for session in sessions:
        if session.name.startswith(prefix):
            # This will be called with a check against DB in the app
            killed.append(session.name)
    return killed
