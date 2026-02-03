"""Textual TUI application for clux."""

import subprocess
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, Label, Static, Tree
from textual.widgets.tree import TreeNode

from ..db import Session, SessionDB, validate_session_name
from .. import tmux
from ..config import Config


def get_cwd() -> str:
    return str(Path.cwd().resolve())


class TmuxPreview(Static):
    """Preview pane showing tmux session content."""

    content = reactive("")

    def __init__(self) -> None:
        super().__init__("", id="preview")

    def watch_content(self, content: str) -> None:
        self.update(content)


class ConfirmModal(ModalScreen[bool]):
    """Confirmation modal."""

    def __init__(self, message: str, title: str = "Confirm") -> None:
        super().__init__()
        self.message = message
        self.modal_title = title

    BINDINGS = [
        Binding("y", "confirm", "Yes"),
        Binding("n", "cancel", "No"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def compose(self) -> ComposeResult:
        yield Container(
            Label(self.modal_title, id="modal-title"),
            Label(self.message),
            Label("[dim]Press Y to confirm, N or Escape to cancel[/]"),
            id="confirm-modal",
        )

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class NewSessionModal(ModalScreen[str | None]):
    """Modal for creating a new session."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def compose(self) -> ComposeResult:
        yield Container(
            Label("New Session", id="modal-title"),
            Input(placeholder="Session name", id="session-name"),
            Label("[dim]Press Enter to create, Escape to cancel[/]"),
            id="new-session-modal",
        )

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        name = event.value.strip()
        if name:
            self.dismiss(name)
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class CluxApp(App):
    """Main clux TUI application."""

    show_archived = reactive(False)

    CSS = """
    #main-layout {
        layout: horizontal;
    }

    #sidebar {
        width: 40;
        border-right: solid $primary;
        padding: 0 1;
    }

    #session-tree {
        width: 100%;
        height: 100%;
    }

    #preview-container {
        width: 1fr;
        padding: 0 1;
    }

    #preview {
        width: 100%;
        height: 100%;
        background: $surface;
        overflow-y: auto;
    }

    #preview-header {
        dock: top;
        height: 1;
        background: $primary;
        color: $text;
        text-align: center;
        text-style: bold;
    }

    Tree {
        background: transparent;
    }

    Tree > .tree--cursor {
        background: $accent;
    }

    #new-session-modal {
        align: center middle;
        width: 50;
        height: 10;
        border: solid green;
        background: $surface;
        padding: 1 2;
    }

    #modal-title {
        text-align: center;
        text-style: bold;
        margin-bottom: 1;
    }

    #session-name {
        margin-bottom: 1;
    }

    #confirm-modal {
        align: center middle;
        width: 50;
        height: 8;
        border: solid red;
        background: $surface;
        padding: 1 2;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("n", "new_session", "New"),
        Binding("o", "attach", "Open"),
        Binding("a", "archive", "Archive"),
        Binding("d", "delete", "Delete"),
        Binding("r", "refresh", "Refresh"),
        Binding("s", "toggle_archived", "Show Archived"),
        Binding("u", "unarchive", "Unarchive"),
        Binding("tab", "next_directory", "Next Dir", show=False, priority=True),
        Binding("shift+tab", "prev_directory", "Prev Dir", show=False, priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.db = SessionDB()
        self.config = Config.load()
        self.cwd = get_cwd()
        self.sessions: list[Session] = []
        self.session_map: dict[str, Session] = {}  # "dir:name" -> Session
        self.dir_nodes: list[TreeNode] = []  # for tab navigation
        self.current_dir_idx: int = 0

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main-layout"):
            with Vertical(id="sidebar"):
                yield Tree("Sessions", id="session-tree")
            with Vertical(id="preview-container"):
                yield Static("Preview", id="preview-header")
                yield TmuxPreview()
        yield Footer()

    def on_mount(self) -> None:
        self.title = "clux"
        self.sub_title = "Claude Code Session Manager"
        self.cleanup_orphaned_tmux()
        self.refresh_sessions()
        self.query_one(Tree).focus()

    def cleanup_orphaned_tmux(self) -> None:
        """Kill tmux sessions that aren't tracked in DB."""
        tmux_sessions = tmux.get_sessions_map()
        db_sessions = self.db.list_sessions(include_archived=True)

        # Get set of tmux session names tracked in DB
        tracked_tmux = {s.tmux_session for s in db_sessions if s.tmux_session}

        # Kill orphaned clux-* sessions
        killed = []
        for name in tmux_sessions:
            if name.startswith("clux-") and name not in tracked_tmux:
                if tmux.kill_session(name):
                    killed.append(name)

        if killed:
            self.notify(f"Cleaned up {len(killed)} orphaned tmux session(s)")

    def refresh_sessions(self) -> None:
        """Refresh the session list."""
        self.sessions = self.db.list_sessions(include_archived=self.show_archived)

        # Fetch tmux sessions once for efficient status sync
        tmux_sessions = tmux.get_sessions_map()

        # Sync status with tmux
        for session in self.sessions:
            self.sync_session_status(session, tmux_sessions)

        # Build tree
        tree: Tree = self.query_one(Tree)
        tree.clear()
        self.session_map = {}
        self.dir_nodes = []

        if not self.sessions:
            msg = "[dim]No sessions. Press 'n' to create.[/]"
            if not self.show_archived:
                msg += " Press 's' to show archived."
            tree.root.add_leaf(msg)
            return

        # Group by directory
        by_dir: dict[str, list[Session]] = {}
        for session in self.sessions:
            if session.working_directory not in by_dir:
                by_dir[session.working_directory] = []
            by_dir[session.working_directory].append(session)

        # Sort: current dir first
        dirs = sorted(by_dir.keys(), key=lambda d: (d != self.cwd, d))

        # Build tree
        for directory in dirs:
            is_current = directory == self.cwd
            display = directory.replace(str(Path.home()), "~")
            if is_current:
                label = f"[green bold]{display}[/] [dim](current)[/]"
            else:
                label = f"[blue]{display}[/]"

            dir_node = tree.root.add(label, expand=True)
            self.dir_nodes.append(dir_node)

            for session in by_dir[directory]:
                status_icons = {
                    "active": "[green]●[/]",
                    "detached": "[yellow]○[/]",
                    "idle": "[white]○[/]",
                    "archived": "[dim]◌[/]",
                }
                icon = status_icons.get(session.status, "?")
                # Add resume indicator if session has Claude session ID
                resume = "[blue]↺[/] " if session.claude_session_id else ""
                session_label = f"{icon} {resume}{session.name} [dim]{session.age}[/]"
                # Use stable key based on session identity, stored in node.data
                session_key = f"{session.working_directory}:{session.name}"
                node = dir_node.add_leaf(session_label, data=session_key)
                self.session_map[session_key] = session

        # Expand root
        tree.root.expand()
        # Schedule selection of first session after tree renders
        self.call_later(self._select_first_session)

    def _select_first_session(self) -> None:
        """Select the first session node in the tree."""
        tree: Tree = self.query_one(Tree)
        for dir_node in self.dir_nodes:
            for child in dir_node.children:
                if child.data:  # Has session data = it's a session node
                    tree.select_node(child)
                    # Set current_dir_idx to match
                    for i, dn in enumerate(self.dir_nodes):
                        if child in dn.children:
                            self.current_dir_idx = i
                            break
                    # Update preview
                    self._update_preview(child.data)
                    return

    def sync_session_status(self, session: Session, tmux_sessions: dict | None = None) -> None:
        """Sync session status with tmux state."""
        if session.tmux_session:
            if tmux.session_exists(session.tmux_session, tmux_sessions):
                new_status = "detached" if not tmux.is_attached(session.tmux_session, tmux_sessions) else "active"
            else:
                new_status = "idle"

            if new_status != session.status and session.status != "archived":
                self.db.update_status(session.id, new_status)
                session.status = new_status

    def get_selected_session(self) -> Session | None:
        """Get the currently selected session."""
        tree: Tree = self.query_one(Tree)
        node = tree.cursor_node
        if node and node.data:
            # node.data contains the session_key
            return self.session_map.get(node.data)
        return None

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        """Handle node selection - update preview."""
        session_key = event.node.data if event.node else None
        self._update_preview(session_key)

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        """Update preview when selection changes."""
        session_key = event.node.data if event.node else None
        self._update_preview(session_key)

    def _update_preview(self, session_key: str | None) -> None:
        """Update the preview pane for a session."""
        session = self.session_map.get(session_key) if session_key else None
        preview = self.query_one(TmuxPreview)
        header = self.query_one("#preview-header", Static)

        if session:
            header.update(f" {session.name} [{session.status}]")
            if session.tmux_session and tmux.session_exists(session.tmux_session):
                # Capture tmux pane content
                content = tmux.get_pane_content(session.tmux_session, lines=100)
                if content.strip():
                    preview.content = content
                else:
                    preview.content = f"[dim]tmux session '{session.tmux_session}' is empty[/]"
            elif session.claude_session_id:
                preview.content = (
                    f"[dim]Session idle - has Claude session ID[/]\n\n"
                    f"[cyan]Claude ID:[/] {session.claude_session_id[:16]}...\n"
                    f"[cyan]Directory:[/] {session.working_directory}\n"
                    f"[cyan]Created:[/] {session.created_at}\n\n"
                    f"[dim]Press Enter to resume with --resume[/]"
                )
            else:
                preview.content = (
                    f"[dim]Session idle - no Claude session[/]\n\n"
                    f"[cyan]Directory:[/] {session.working_directory}\n"
                    f"[cyan]Created:[/] {session.created_at}\n\n"
                    f"[dim]Press Enter to start fresh[/]"
                )
        else:
            header.update(" Preview")
            preview.content = "[dim]Select a session to preview[/]"

    def on_key(self, event) -> None:
        """Handle key events - intercept Enter for attach."""
        if event.key == "enter":
            if len(self.screen_stack) > 1:
                return  # Let modal handle it
            event.prevent_default()
            event.stop()
            self.action_attach()

    def action_quit(self) -> None:
        self.exit()

    def action_refresh(self) -> None:
        self.refresh_sessions()
        self.notify("Refreshed")

    def action_next_directory(self) -> None:
        """Jump to first session in next directory."""
        if not self.dir_nodes:
            return
        # Find next directory that has sessions
        for _ in range(len(self.dir_nodes)):
            self.current_dir_idx = (self.current_dir_idx + 1) % len(self.dir_nodes)
            dir_node = self.dir_nodes[self.current_dir_idx]
            # Find first session node (child with data)
            for child in dir_node.children:
                if child.data:
                    tree: Tree = self.query_one(Tree)
                    tree.select_node(child)
                    return

    def action_prev_directory(self) -> None:
        """Jump to first session in previous directory."""
        if not self.dir_nodes:
            return
        # Find previous directory that has sessions
        for _ in range(len(self.dir_nodes)):
            self.current_dir_idx = (self.current_dir_idx - 1) % len(self.dir_nodes)
            dir_node = self.dir_nodes[self.current_dir_idx]
            # Find first session node (child with data)
            for child in dir_node.children:
                if child.data:
                    tree: Tree = self.query_one(Tree)
                    tree.select_node(child)
                    return

    def action_new_session(self) -> None:
        """Create a new session."""
        def on_result(name: str | None) -> None:
            if name:
                self.create_session(name)

        self.push_screen(NewSessionModal(), on_result)

    def create_session(self, name: str) -> None:
        """Create and attach to a new session."""
        # Validate name
        is_valid, error = validate_session_name(name)
        if not is_valid:
            self.notify(f"Invalid name: {error}", severity="error")
            return

        existing = self.db.get_session(name, self.cwd)
        if existing:
            self.notify(f"Session '{name}' already exists", severity="error")
            return

        tmux_name = f"clux-{name}"

        if tmux.session_exists(tmux_name):
            tmux.kill_session(tmux_name)

        session = self.db.create_session(name, self.cwd, tmux_name)

        if not tmux.create_session(tmux_name, self.cwd):
            self.notify("Failed to create tmux session", severity="error")
            self.db.delete_session(session.id)
            return

        claude_cmd = " ".join(self.config.get_claude_command())
        tmux.send_keys(tmux_name, claude_cmd)

        self.db.update_status(session.id, "active")
        self.exit(result=("attach", tmux_name))

    def action_attach(self) -> None:
        """Attach to selected session."""
        session = self.get_selected_session()
        if not session:
            self.notify("No session selected", severity="warning")
            return

        if session.tmux_session and tmux.session_exists(session.tmux_session):
            self.db.update_status(session.id, "active")
            self.exit(result=("attach", session.tmux_session))
        elif session.claude_session_id:
            tmux_name = session.tmux_session or f"clux-{session.name}"

            if not tmux.create_session(tmux_name, session.working_directory):
                self.notify("Failed to create tmux session", severity="error")
                return

            claude_cmd = self.config.get_claude_command()
            claude_cmd.extend(["--resume", session.claude_session_id])
            tmux.send_keys(tmux_name, " ".join(claude_cmd))

            self.db.update_status(session.id, "active")
            self.exit(result=("attach", tmux_name))
        else:
            tmux_name = session.tmux_session or f"clux-{session.name}"

            if not tmux.create_session(tmux_name, session.working_directory):
                self.notify("Failed to create tmux session", severity="error")
                return

            claude_cmd = " ".join(self.config.get_claude_command())
            tmux.send_keys(tmux_name, claude_cmd)

            self.db.update_status(session.id, "active")
            self.exit(result=("attach", tmux_name))

    def action_archive(self) -> None:
        """Archive selected session."""
        session = self.get_selected_session()
        if not session:
            self.notify("No session selected", severity="warning")
            return

        if session.tmux_session and tmux.session_exists(session.tmux_session):
            tmux.kill_session(session.tmux_session)

        self.db.update_status(session.id, "archived")
        self.notify(f"Archived: {session.name}")
        self.refresh_sessions()

    def action_delete(self) -> None:
        """Delete selected session."""
        session = self.get_selected_session()
        if not session:
            self.notify("No session selected", severity="warning")
            return

        # Show confirmation with callback
        def do_delete(confirmed: bool) -> None:
            if not confirmed:
                return
            if session.tmux_session and tmux.session_exists(session.tmux_session):
                tmux.kill_session(session.tmux_session)
            self.db.delete_session(session.id)
            self.notify(f"Deleted: {session.name}")
            self.refresh_sessions()

        self.push_screen(
            ConfirmModal(f"Delete session '{session.name}'?", "Delete Session"),
            do_delete
        )

    def action_toggle_archived(self) -> None:
        """Toggle showing archived sessions."""
        self.show_archived = not self.show_archived
        state = "showing" if self.show_archived else "hiding"
        self.notify(f"Now {state} archived sessions")
        self.refresh_sessions()

    def action_unarchive(self) -> None:
        """Unarchive/restore selected session."""
        session = self.get_selected_session()
        if not session:
            self.notify("No session selected", severity="warning")
            return

        if session.status != "archived":
            self.notify("Session is not archived", severity="warning")
            return

        self.db.restore_session(session.id)
        self.notify(f"Restored: {session.name}")
        self.refresh_sessions()


def run_tui() -> None:
    """Run the TUI and handle the result."""
    import os

    app = CluxApp()
    result = app.run()

    if result and isinstance(result, tuple):
        action, tmux_name = result
        if action == "attach":
            # If already inside tmux with an attached client, use switch-client
            # Otherwise use attach-session
            if os.environ.get("TMUX"):
                # Try switch-client first
                switch_result = subprocess.run(
                    ["tmux", "switch-client", "-t", tmux_name],
                    capture_output=True,
                )
                if switch_result.returncode != 0:
                    # No current client - can't switch or attach from inside tmux
                    # without a client. Print manual command.
                    print(f"Session ready: tmux attach -t {tmux_name}")
            else:
                subprocess.run(["tmux", "attach-session", "-t", tmux_name])
