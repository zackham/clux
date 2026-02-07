"""Claude session detection and management."""

from pathlib import Path


def get_claude_projects_dir() -> Path:
    """Get the Claude projects directory."""
    return Path.home() / ".claude" / "projects"


def path_to_project_dir_name(working_directory: str) -> str:
    """Convert a working directory path to Claude's project directory name.

    Claude uses a format like: -home-user-work-myproject
    which is the path with slashes and underscores replaced by dashes.
    """
    # Normalize the path
    path = Path(working_directory).resolve()
    # Convert to string and replace / and _ with -
    path_str = str(path)
    if path_str.startswith("/"):
        path_str = path_str[1:]
    return "-" + path_str.replace("/", "-").replace("_", "-")


def get_project_sessions_dir(working_directory: str) -> Path | None:
    """Get the Claude sessions directory for a working directory."""
    projects_dir = get_claude_projects_dir()
    project_name = path_to_project_dir_name(working_directory)
    project_dir = projects_dir / project_name

    if project_dir.exists():
        return project_dir
    return None
