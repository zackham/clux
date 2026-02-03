"""Claude session detection and management."""

import os
import re
from pathlib import Path


def get_claude_projects_dir() -> Path:
    """Get the Claude projects directory."""
    return Path.home() / ".claude" / "projects"


def path_to_project_dir_name(working_directory: str) -> str:
    """Convert a working directory path to Claude's project directory name.

    Claude uses a format like: -home-user-work-myproject
    which is the path with slashes replaced by dashes.
    """
    # Normalize the path
    path = Path(working_directory).resolve()
    # Convert to string and replace / with -
    # Remove leading slash, replace remaining slashes
    path_str = str(path)
    if path_str.startswith("/"):
        path_str = path_str[1:]
    return "-" + path_str.replace("/", "-")


def get_project_sessions_dir(working_directory: str) -> Path | None:
    """Get the Claude sessions directory for a working directory."""
    projects_dir = get_claude_projects_dir()
    project_name = path_to_project_dir_name(working_directory)
    project_dir = projects_dir / project_name

    if project_dir.exists():
        return project_dir
    return None


def find_latest_session_id(working_directory: str) -> str | None:
    """Find the most recent Claude session ID for a working directory.

    Returns the session ID (UUID) or None if no sessions found.
    """
    project_dir = get_project_sessions_dir(working_directory)
    if not project_dir:
        return None

    # UUID pattern - match both directories and .jsonl files
    uuid_pattern = re.compile(
        r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(\.jsonl)?$",
        re.IGNORECASE,
    )

    # Find all UUID sessions (directories or .jsonl files) and their modification times
    sessions = []
    for entry in project_dir.iterdir():
        match = uuid_pattern.match(entry.name)
        if match:
            try:
                mtime = entry.stat().st_mtime
                sessions.append((mtime, match.group(1)))  # Extract UUID without .jsonl
            except OSError:
                continue

    if not sessions:
        return None

    # Return the most recently modified session
    sessions.sort(reverse=True)
    return sessions[0][1]


def find_session_after_time(working_directory: str, after_timestamp: float) -> str | None:
    """Find the most recent session created after a given timestamp.

    Returns the newest session created after the timestamp, or None.
    """
    project_dir = get_project_sessions_dir(working_directory)
    if not project_dir:
        return None

    # UUID pattern - match both directories and .jsonl files
    uuid_pattern = re.compile(
        r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(\.jsonl)?$",
        re.IGNORECASE,
    )

    # Find all sessions created after the timestamp
    candidates = []
    for entry in project_dir.iterdir():
        match = uuid_pattern.match(entry.name)
        if match:
            try:
                mtime = entry.stat().st_mtime
                if mtime > after_timestamp:
                    candidates.append((mtime, match.group(1)))  # Extract UUID without .jsonl
            except OSError:
                continue

    if not candidates:
        return None

    # Return the most recent one
    candidates.sort(reverse=True)
    return candidates[0][1]


def list_sessions(working_directory: str, limit: int = 10) -> list[tuple[str, float]]:
    """List recent Claude sessions for a working directory.

    Returns list of (session_id, mtime) tuples, most recent first.
    """
    project_dir = get_project_sessions_dir(working_directory)
    if not project_dir:
        return []

    # UUID pattern - match both directories and .jsonl files
    uuid_pattern = re.compile(
        r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(\.jsonl)?$",
        re.IGNORECASE,
    )

    sessions = []
    for entry in project_dir.iterdir():
        match = uuid_pattern.match(entry.name)
        if match:
            try:
                mtime = entry.stat().st_mtime
                sessions.append((match.group(1), mtime))  # Extract UUID without .jsonl
            except OSError:
                continue

    sessions.sort(key=lambda x: x[1], reverse=True)
    return sessions[:limit]
