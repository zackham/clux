"""Non-interactive prompt execution for clux sessions."""

import json
import subprocess
import sys
from dataclasses import dataclass

from .config import Config
from .db import SessionDB
from .tmux import kill_session, session_exists


@dataclass
class PromptResult:
    """Result from a prompt execution."""

    text: str
    session_id: str | None
    cost_usd: float | None
    exit_code: int
    error: str | None = None


def run_prompt(
    session_name: str,
    message: str,
    working_directory: str,
    json_mode: bool = False,
    safe: bool = False,
    timeout: int = 900,
) -> PromptResult:
    """
    Run claude in print mode against a session.

    Returns PromptResult with text, session_id, cost, and exit status.
    Streams output to stdout as it arrives.

    Args:
        session_name: Name of the clux session
        message: The prompt message to send
        working_directory: Directory the session belongs to
        json_mode: If True, output raw NDJSON instead of parsed text
        safe: If True, don't use --dangerously-skip-permissions
        timeout: Subprocess timeout in seconds

    Returns:
        PromptResult with response text, session ID, cost, and exit status
    """
    db = SessionDB()
    config = Config.load()

    # 1. Look up session
    session = db.get_session(session_name, working_directory)
    if not session:
        raise ValueError(f"Session '{session_name}' not found in {working_directory}")

    if not session.claude_session_id:
        raise ValueError(
            f"Session '{session_name}' has no claude session ID - use interactively first"
        )

    # 2. Kill tmux if running
    if session.tmux_session and session_exists(session.tmux_session):
        kill_session(session.tmux_session)

    # 3. Build command
    cmd = config.get_claude_command(safe=safe)
    cmd.extend(
        [
            "--print",
            "--verbose",
            "--output-format",
            "stream-json",
            "--resume",
            session.claude_session_id,
            "-p",
            message,
        ]
    )

    # 4. Execute with streaming
    text_parts: list[str] = []
    result_session_id: str | None = None
    cost_usd: float | None = None

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=working_directory,
    )

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue

            if json_mode:
                # Pass through raw NDJSON
                print(line, flush=True)

            try:
                event = json.loads(line)
                event_type = event.get("type")

                if event_type == "assistant":
                    msg = event.get("message", {})
                    for block in msg.get("content", []):
                        if block.get("type") == "text":
                            text = block.get("text", "")
                            text_parts.append(text)
                            if not json_mode:
                                print(text, end="", flush=True)
                        # Silently skip tool_use blocks (handled by claude internally)

                elif event_type == "result":
                    result_session_id = event.get("session_id")
                    cost_usd = event.get("cost_usd") or event.get("total_cost_usd")

                elif event_type == "system" and event.get("subtype") == "init":
                    if not result_session_id:
                        result_session_id = event.get("session_id")

                # Silently skip: tool_use, tool_result, other event types

            except json.JSONDecodeError:
                continue

        proc.wait(timeout=timeout)

        if not json_mode:
            print()  # Final newline

    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return PromptResult(
            text="".join(text_parts),
            session_id=result_session_id,
            cost_usd=cost_usd,
            exit_code=124,  # Standard timeout exit code
            error="Timeout expired",
        )

    # Capture stderr if failed
    assert proc.stderr is not None
    stderr = proc.stderr.read() if proc.returncode != 0 else None

    # 5. Update db (even on failure, activity happened)
    db.update_activity(session.id)
    if result_session_id and result_session_id != session.claude_session_id:
        db.update_claude_session_id(session.id, result_session_id)

    return PromptResult(
        text="".join(text_parts),
        session_id=result_session_id,
        cost_usd=cost_usd,
        exit_code=proc.returncode,
        error=stderr.strip() if stderr else None,
    )
