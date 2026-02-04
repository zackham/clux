"""SQLite database for session registry."""

import logging
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)


class DatabaseError(Exception):
    """Database operation failed."""
    pass


# Valid session name pattern: alphanumeric, hyphens, underscores, 1-50 chars
SESSION_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,49}$")


def validate_session_name(name: str) -> tuple[bool, str]:
    """Validate a session name.

    Returns (is_valid, error_message).
    """
    if not name:
        return False, "Session name cannot be empty"
    if len(name) > 50:
        return False, "Session name must be 50 characters or less"
    if not SESSION_NAME_PATTERN.match(name):
        return False, "Session name must start with alphanumeric and contain only letters, numbers, hyphens, and underscores"
    return True, ""


def make_tmux_name(session_name: str, working_directory: str) -> str:
    """Generate a unique tmux session name from session name + directory.

    Includes a short hash of the directory to avoid collisions when the same
    session name is used in different directories.
    """
    import hashlib
    dir_hash = hashlib.md5(working_directory.encode()).hexdigest()[:6]
    return f"clux-{session_name}-{dir_hash}"


def get_db_path() -> Path:
    """Get database path following XDG spec."""
    import os
    xdg_data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    db_dir = xdg_data / "clux"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "sessions.db"


@dataclass
class Session:
    """A clux session."""

    id: str
    name: str
    working_directory: str
    status: str
    created_at: str
    tmux_session: str | None = None
    claude_session_id: str | None = None
    last_activity: str | None = None
    archived_at: str | None = None

    @property
    def is_archived(self) -> bool:
        return self.status == "archived"

    @property
    def display_name(self) -> str:
        return self.name

    @property
    def age(self) -> str:
        """Human-readable age string."""
        ts = self.last_activity or self.created_at
        try:
            dt = datetime.fromisoformat(ts)
            # Handle both naive and aware datetimes
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            delta = now - dt
            if delta.days > 0:
                return f"{delta.days}d ago"
            hours = delta.seconds // 3600
            if hours > 0:
                return f"{hours}h ago"
            minutes = delta.seconds // 60
            return f"{minutes}m ago"
        except Exception:
            return "unknown"

    @property
    def session_key(self) -> str:
        """Stable unique key for this session."""
        return f"{self.working_directory}:{self.name}"


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    claude_session_id TEXT,
    working_directory TEXT NOT NULL,
    tmux_session TEXT,
    status TEXT DEFAULT 'idle',
    created_at TEXT NOT NULL,
    last_activity TEXT,
    archived_at TEXT,
    UNIQUE(name, working_directory)
);

CREATE INDEX IF NOT EXISTS idx_sessions_directory ON sessions(working_directory);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
"""


class SessionDB:
    """Session database operations."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or get_db_path()
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database schema."""
        try:
            with self._connection() as conn:
                conn.executescript(SCHEMA)
        except sqlite3.Error as e:
            logger.error(f"Failed to initialize database: {e}")
            raise DatabaseError(f"Failed to initialize database: {e}") from e

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Context manager for database connections with WAL mode."""
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def create_session(
        self,
        name: str,
        working_directory: str,
        tmux_session: str | None = None,
    ) -> Session:
        """Create a new session."""
        session_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        try:
            with self._connection() as conn:
                conn.execute(
                    """
                    INSERT INTO sessions (id, name, working_directory, tmux_session, status, created_at, last_activity)
                    VALUES (?, ?, ?, ?, 'idle', ?, ?)
                    """,
                    (session_id, name, working_directory, tmux_session, now, now),
                )
        except sqlite3.IntegrityError as e:
            logger.error(f"Session already exists: {name} in {working_directory}")
            raise DatabaseError(f"Session '{name}' already exists in this directory") from e
        except sqlite3.Error as e:
            logger.error(f"Failed to create session: {e}")
            raise DatabaseError(f"Failed to create session: {e}") from e

        return Session(
            id=session_id,
            name=name,
            working_directory=working_directory,
            tmux_session=tmux_session,
            status="idle",
            created_at=now,
            last_activity=now,
        )

    def get_session(self, name: str, working_directory: str) -> Session | None:
        """Get a session by name and directory."""
        try:
            with self._connection() as conn:
                row = conn.execute(
                    "SELECT * FROM sessions WHERE name = ? AND working_directory = ?",
                    (name, working_directory),
                ).fetchone()
                if row:
                    return self._row_to_session(row)
            return None
        except sqlite3.Error as e:
            logger.error(f"Failed to get session: {e}")
            raise DatabaseError(f"Failed to get session: {e}") from e

    def get_session_by_id(self, session_id: str) -> Session | None:
        """Get a session by ID."""
        try:
            with self._connection() as conn:
                row = conn.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if row:
                    return self._row_to_session(row)
            return None
        except sqlite3.Error as e:
            logger.error(f"Failed to get session by ID: {e}")
            raise DatabaseError(f"Failed to get session: {e}") from e

    def get_session_by_tmux_name(self, tmux_session: str) -> Session | None:
        """Get a session by its tmux session name."""
        try:
            with self._connection() as conn:
                row = conn.execute(
                    "SELECT * FROM sessions WHERE tmux_session = ?",
                    (tmux_session,),
                ).fetchone()
                if row:
                    return self._row_to_session(row)
            return None
        except sqlite3.Error as e:
            logger.error(f"Failed to get session by tmux name: {e}")
            raise DatabaseError(f"Failed to get session: {e}") from e

    def list_sessions(
        self,
        include_archived: bool = False,
        working_directory: str | None = None,
    ) -> list[Session]:
        """List all sessions."""
        try:
            with self._connection() as conn:
                query = "SELECT * FROM sessions"
                params: list = []
                conditions = []

                if not include_archived:
                    conditions.append("status != 'archived'")

                if working_directory:
                    conditions.append("working_directory = ?")
                    params.append(working_directory)

                if conditions:
                    query += " WHERE " + " AND ".join(conditions)

                query += " ORDER BY last_activity DESC"

                rows = conn.execute(query, params).fetchall()
                return [self._row_to_session(row) for row in rows]
        except sqlite3.Error as e:
            logger.error(f"Failed to list sessions: {e}")
            raise DatabaseError(f"Failed to list sessions: {e}") from e

    def update_status(self, session_id: str, status: str) -> None:
        """Update session status."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            with self._connection() as conn:
                if status == "archived":
                    conn.execute(
                        "UPDATE sessions SET status = ?, archived_at = ?, last_activity = ? WHERE id = ?",
                        (status, now, now, session_id),
                    )
                else:
                    conn.execute(
                        "UPDATE sessions SET status = ?, last_activity = ? WHERE id = ?",
                        (status, now, session_id),
                    )
        except sqlite3.Error as e:
            logger.error(f"Failed to update session status: {e}")
            raise DatabaseError(f"Failed to update session status: {e}") from e

    def update_claude_session_id(self, session_id: str, claude_session_id: str) -> None:
        """Update the Claude session ID."""
        try:
            with self._connection() as conn:
                conn.execute(
                    "UPDATE sessions SET claude_session_id = ? WHERE id = ?",
                    (claude_session_id, session_id),
                )
        except sqlite3.Error as e:
            logger.error(f"Failed to update Claude session ID: {e}")
            raise DatabaseError(f"Failed to update Claude session ID: {e}") from e

    def update_activity(self, session_id: str) -> None:
        """Touch last_activity timestamp."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            with self._connection() as conn:
                conn.execute(
                    "UPDATE sessions SET last_activity = ? WHERE id = ?",
                    (now, session_id),
                )
        except sqlite3.Error as e:
            logger.error(f"Failed to update activity: {e}")
            raise DatabaseError(f"Failed to update activity: {e}") from e

    def delete_session(self, session_id: str) -> None:
        """Delete a session permanently."""
        try:
            with self._connection() as conn:
                conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        except sqlite3.Error as e:
            logger.error(f"Failed to delete session: {e}")
            raise DatabaseError(f"Failed to delete session: {e}") from e

    def restore_session(self, session_id: str) -> None:
        """Restore an archived session."""
        try:
            with self._connection() as conn:
                conn.execute(
                    "UPDATE sessions SET status = 'idle', archived_at = NULL WHERE id = ?",
                    (session_id,),
                )
        except sqlite3.Error as e:
            logger.error(f"Failed to restore session: {e}")
            raise DatabaseError(f"Failed to restore session: {e}") from e

    def _row_to_session(self, row: sqlite3.Row) -> Session:
        """Convert a database row to a Session object."""
        return Session(
            id=row["id"],
            name=row["name"],
            claude_session_id=row["claude_session_id"],
            working_directory=row["working_directory"],
            tmux_session=row["tmux_session"],
            status=row["status"],
            created_at=row["created_at"],
            last_activity=row["last_activity"],
            archived_at=row["archived_at"],
        )
