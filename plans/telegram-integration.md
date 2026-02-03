# Plan: clux Prompt Command + Telegram Integration

Two phases: (1) clux CLI gets a `prompt` command, (2) vita telegram learns to attach to clux sessions.

---

# Phase 1: clux CLI (in clux repo)

## 1.1 Single-Letter Aliases

Add click aliases: `clux a` = attach, `clux d` = delete, `clux l` = list, `clux n` = new, `clux s` = status.

**File:** `src/clux/cli.py`

Implementation: Hidden click commands that invoke the real ones with `ctx.invoke()`.

```python
@main.command("a", hidden=True)
@click.argument("name")
@click.option("--safe", is_flag=True)
@click.pass_context
def a_cmd(ctx, name, safe):
    ctx.invoke(attach_cmd, name=name, safe=safe)
```

---

## 1.2 `clux prompt` Command

**Usage:** `clux prompt <session> "message"` (alias: `clux p`)

### Behavior:
1. Look up session by name + directory (use `--dir` or cwd)
2. Validate session has `claude_session_id` (error if not - can't resume a session that was never used)
3. If tmux session exists for this session, kill it entirely
4. Build command: `claude --print --verbose --output-format stream-json --resume <session_id> -p "message"`
5. Execute with streaming subprocess
6. Parse NDJSON events, emit to stdout based on mode
7. Update `last_activity` in db
8. Capture final `session_id` from result event (defensive - shouldn't change)

### CLI Flags:
```
clux prompt <session> <message>
    --dir <path>       Look up session in specific directory (default: cwd)
    --json             Output raw stream-json (NDJSON) instead of parsed text
    --timeout <secs>   Subprocess timeout, default 900
    --safe             Use safe mode (no --dangerously-skip-permissions)
```

### Stream-JSON Format (NDJSON from claude):
```jsonl
{"type":"system","subtype":"init","session_id":"abc-123","..."}
{"type":"assistant","message":{"content":[{"type":"text","text":"Hello..."}]}}
{"type":"assistant","message":{"content":[{"type":"text","text":" world"}]}}
{"type":"result","session_id":"abc-123","cost_usd":0.05}
```

### Output Modes:

**Default (text streaming):**
- Parse each NDJSON line as it arrives
- For `assistant` events, extract text from `message.content[].text` and print immediately
- Result: live streaming text output to terminal

**`--json` mode:**
- Pass through raw NDJSON lines as-is
- Caller can parse events themselves
- Enables: progress indicators, cost tracking, session_id extraction

### Streaming Implementation (`src/clux/prompt.py`):

```python
import subprocess
import sys
import json
from dataclasses import dataclass
from .config import Config
from .db import SessionDB
from .tmux import session_exists, kill_session


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
    """
    db = SessionDB()
    config = Config.load()

    # 1. Look up session
    session = db.get_session(session_name, working_directory)
    if not session:
        raise ValueError(f"Session '{session_name}' not found in {working_directory}")

    if not session.claude_session_id:
        raise ValueError(f"Session '{session_name}' has no claude session ID - use interactively first")

    # 2. Kill tmux if running
    if session.tmux_session and session_exists(session.tmux_session):
        kill_session(session.tmux_session)

    # 3. Build command
    cmd = config.get_claude_command(safe=safe)
    cmd.extend([
        "--print",
        "--verbose",
        "--output-format", "stream-json",
        "--resume", session.claude_session_id,
        "-p", message,
    ])

    # 4. Execute with streaming
    text_parts = []
    result_session_id = None
    cost_usd = None

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=working_directory,
    )

    try:
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
```

### CLI Command (`cli.py`):

```python
@main.command("prompt")
@click.argument("name")
@click.argument("message")
@click.option("--dir", "directory", type=click.Path(exists=True), help="Working directory")
@click.option("--json", "json_mode", is_flag=True, help="Output raw stream-json")
@click.option("--timeout", default=900, help="Timeout in seconds")
@click.option("--safe", is_flag=True, help="Disable YOLO mode")
def prompt_cmd(name: str, message: str, directory: str | None, json_mode: bool, timeout: int, safe: bool) -> None:
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


# Alias
@main.command("p", hidden=True)
@click.argument("name")
@click.argument("message")
@click.option("--dir", "directory", type=click.Path(exists=True))
@click.option("--json", "json_mode", is_flag=True)
@click.option("--timeout", default=900)
@click.option("--safe", is_flag=True)
@click.pass_context
def p_cmd(ctx, **kwargs):
    ctx.invoke(prompt_cmd, **kwargs)
```

### Files:
- **Modify:** `src/clux/cli.py` - add `prompt` command + `p` alias + other single-letter aliases
- **Create:** `src/clux/prompt.py` - streaming execution logic

---

## 1.3 Session Listing for External Consumers

Add a way for telegram to get available sessions without parsing CLI output.

**Option A:** `clux list --json` outputs JSON array
**Option B:** Direct sqlite access (clux db is at `~/.local/share/clux/sessions.db`)

Recommendation: **Option A** - cleaner interface, db schema can change.

```python
@main.command("list")
@click.option("--json", "json_mode", is_flag=True, help="Output as JSON")
# ... existing options ...
def list_cmd(json_mode: bool, ...):
    if json_mode:
        sessions = [
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
        click.echo(json.dumps(sessions))
        return
    # ... existing display logic ...
```

---

# Phase 2: Vita Telegram Integration (in vita repo)

## 2.1 State Management

**File:** `scripts/telegram_clux.py`

```python
"""Clux session attachment for Telegram."""

import json
import subprocess
from pathlib import Path
from datetime import datetime

STATE_FILE = Path(__file__).parent.parent / "data" / "telegram" / "clux_state.json"

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"topic_attachments": {}}

def save_state(state: dict) -> None:
    """Atomic state save using temp file + rename."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.rename(STATE_FILE)

def get_attachment(topic_id: str) -> dict | None:
    """Get clux attachment for a topic, or None."""
    state = load_state()
    return state.get("topic_attachments", {}).get(topic_id)

def attach_topic(topic_id: str, session_name: str, session_dir: str, claude_session_id: str) -> None:
    """Attach a topic to a clux session."""
    state = load_state()
    state.setdefault("topic_attachments", {})[topic_id] = {
        "session_name": session_name,
        "session_dir": session_dir,
        "claude_session_id": claude_session_id,
        "attached_at": datetime.now().isoformat(),
    }
    save_state(state)

def detach_topic(topic_id: str) -> bool:
    """Detach a topic. Returns True if was attached."""
    state = load_state()
    if topic_id in state.get("topic_attachments", {}):
        del state["topic_attachments"][topic_id]
        save_state(state)
        return True
    return False

_sessions_cache: tuple[float, list[dict]] | None = None
_CACHE_TTL = 5.0  # seconds

def list_available_sessions(use_cache: bool = True) -> list[dict]:
    """Get resumable clux sessions via CLI.

    Caches results for 5 seconds to avoid subprocess spam.
    """
    global _sessions_cache
    import time

    if use_cache and _sessions_cache:
        cached_at, sessions = _sessions_cache
        if time.time() - cached_at < _CACHE_TTL:
            return sessions

    try:
        result = subprocess.run(
            ["clux", "list", "--json"],  # No --all, skip archived
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            sessions = json.loads(result.stdout)
            # Only resumable sessions (have a claude session ID)
            sessions = [s for s in sessions if s.get("claude_session_id")]
            _sessions_cache = (time.time(), sessions)
            return sessions
        return []
    except Exception:
        return []


# Simple lock to prevent concurrent prompts to same session
_active_prompts: set[str] = set()

def is_session_busy(session_name: str, session_dir: str) -> bool:
    """Check if a session is currently processing a prompt."""
    key = f"{session_dir}:{session_name}"
    return key in _active_prompts

def mark_session_busy(session_name: str, session_dir: str) -> None:
    key = f"{session_dir}:{session_name}"
    _active_prompts.add(key)

def mark_session_free(session_name: str, session_dir: str) -> None:
    key = f"{session_dir}:{session_name}"
    _active_prompts.discard(key)

async def run_clux_prompt(
    session_name: str,
    session_dir: str,
    message: str,
    timeout: int = 900,
) -> tuple[str, str | None, float | None, str | None]:
    """
    Run clux prompt and return (response_text, session_id, cost_usd, error).

    Uses --json mode for structured output parsing.
    Returns error string if failed, None if success.
    """
    import asyncio

    # Check busy state
    if is_session_busy(session_name, session_dir):
        return "", None, None, "Session is busy processing another request"

    mark_session_busy(session_name, session_dir)

    try:
        proc = await asyncio.create_subprocess_exec(
            "clux", "prompt", session_name, message,
            "--dir", session_dir,
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        text_parts = []
        session_id = None
        cost_usd = None

        try:
            # Read with timeout
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )

            for line in stdout.decode().split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    if event.get("type") == "assistant":
                        for block in event.get("message", {}).get("content", []):
                            if block.get("type") == "text":
                                text_parts.append(block.get("text", ""))
                    elif event.get("type") == "result":
                        session_id = event.get("session_id")
                        cost_usd = event.get("cost_usd") or event.get("total_cost_usd")
                except json.JSONDecodeError:
                    continue

            if proc.returncode != 0:
                return "".join(text_parts), session_id, cost_usd, stderr.decode().strip()

            return "".join(text_parts), session_id, cost_usd, None

        except asyncio.TimeoutError:
            proc.kill()
            return "".join(text_parts), session_id, cost_usd, "Timeout"

    finally:
        mark_session_free(session_name, session_dir)
```

---

## 2.2 Telegram Bot Integration

**File:** `scripts/telegram_bot.py`

### Interception Point (in handle_message, after auth + topic extraction):

```python
from scripts.telegram_clux import get_attachment, attach_topic, detach_topic, list_available_sessions, run_clux_prompt

# Early in handle_message(), after topic_id extraction:

clux_attachment = get_attachment(topic_id)

# Handle "clux" command
if text.strip().lower() == "clux":
    if clux_attachment:
        await show_clux_status(message, clux_attachment, topic_id)
    else:
        await show_clux_picker(message)
    return

# Handle "clux detach" shortcut
if text.strip().lower() == "clux detach" and clux_attachment:
    detach_topic(topic_id)
    await message.reply_text("Detached from clux session.")
    return

# Route to clux if attached (text messages only)
if clux_attachment and text:
    # Validate session still exists
    sessions = list_available_sessions()
    session_exists = any(
        s["name"] == clux_attachment["session_name"] and
        s["working_directory"] == clux_attachment["session_dir"]
        for s in sessions
    )
    if not session_exists:
        detach_topic(topic_id)
        await message.reply_text("Clux session no longer exists. Detached.")
        # Fall through to normal processing
    else:
        await handle_clux_message(message, text, clux_attachment, context.bot)
        return
# Note: photos/documents while attached fall through to normal vita handling

# ... normal claude processing continues ...
```

### Session Picker (inline keyboard):

```python
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

async def show_clux_picker(message):
    sessions = list_available_sessions()
    if not sessions:
        await message.reply_text("No resumable clux sessions found.")
        return

    buttons = []
    for s in sessions:
        short_dir = s["working_directory"].replace("/home/zack", "~")
        label = f"{s['name']} ({short_dir})"
        callback = f"clux:attach:{s['id']}"
        buttons.append([InlineKeyboardButton(label, callback_data=callback)])

    buttons.append([InlineKeyboardButton("Cancel", callback_data="clux:cancel")])

    await message.reply_text(
        "Select a clux session:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )

def escape_markdown(text: str) -> str:
    """Escape markdown special characters."""
    for char in ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']:
        text = text.replace(char, f'\\{char}')
    return text

async def show_clux_status(message, attachment, topic_id):
    name = escape_markdown(attachment["session_name"])
    short_dir = escape_markdown(attachment["session_dir"].replace("/home/zack", "~"))

    await message.reply_text(
        f"Attached to *{name}* ({short_dir})",
        parse_mode="MarkdownV2",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Detach", callback_data=f"clux:detach:{topic_id}")]
        ]),
    )
```

### Callback Handler:

```python
from telegram.ext import CallbackQueryHandler

async def handle_clux_callback(update, context):
    query = update.callback_query
    await query.answer()

    data = query.data
    topic_id = str(query.message.message_thread_id or "0")

    if data == "clux:cancel":
        await query.message.delete()
        return

    if data.startswith("clux:attach:"):
        session_id = data.split(":", 2)[2]
        # Look up session details
        sessions = list_available_sessions()
        session = next((s for s in sessions if s["id"] == session_id), None)
        if session:
            attach_topic(
                topic_id,
                session["name"],
                session["working_directory"],
                session["claude_session_id"],
            )
            name = escape_markdown(session["name"])
            short_dir = escape_markdown(session["working_directory"].replace("/home/zack", "~"))
            await query.message.edit_text(f"Attached to *{name}* ({short_dir})", parse_mode="MarkdownV2")
        else:
            await query.message.edit_text("Session not found.")
        return

    if data.startswith("clux:detach:"):
        tid = data.split(":", 2)[2]
        detach_topic(tid)
        await query.message.edit_text("Detached from clux session.")
        return

# Register in main():
app.add_handler(CallbackQueryHandler(handle_clux_callback, pattern=r"^clux:"))
```

### Message Handler (when attached):

```python
async def handle_clux_message(message, text, attachment, bot):
    """Process a message through clux instead of vita claude."""
    chat_id = message.chat_id
    thread_id = message.message_thread_id
    topic_id = str(thread_id or "0")
    session_name = attachment["session_name"]

    # Send placeholder with session indicator
    placeholder = await message.reply_text(
        f"[{session_name}] ...",
        message_thread_id=thread_id,
    )

    try:
        # Log inbound (for conversation continuity)
        log_inbound(text, message.message_id, topic_id=topic_id)

        # Run clux prompt
        response, session_id, cost_usd, error = await run_clux_prompt(
            session_name,
            attachment["session_dir"],
            text,
        )

        if error:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=placeholder.message_id,
                text=f"[{session_name}] Error: {error}",
                message_thread_id=thread_id,
            )
            return

        # Truncate for telegram
        if len(response) > 4096:
            response = response[:4090] + "\n..."

        # Update placeholder
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=placeholder.message_id,
            text=response or "(no response)",
            message_thread_id=thread_id,
        )

        # Log outbound with cost
        log_outbound(
            response,
            trigger="clux",
            topic_id=topic_id,
            extra={"clux_session": session_name, "cost_usd": cost_usd},
        )

        # Process stream capture tags (file-this, observation, etc.)
        from scripts.stream_capture.telegram_integration import process_response
        process_response(response)

    except Exception as e:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=placeholder.message_id,
            text=f"[{session_name}] Error: {e}",
            message_thread_id=thread_id,
        )
```

---

## 2.3 Files Summary

### Phase 1 (clux repo):
- **Modify:** `src/clux/cli.py` - aliases + prompt command + list --json
- **Create:** `src/clux/prompt.py` - streaming prompt execution

### Phase 2 (vita repo):
- **Create:** `scripts/telegram_clux.py` - state management + clux subprocess wrapper
- **Modify:** `scripts/telegram_bot.py` - interception + callback handler

---

## Decisions:

1. **Kill entire tmux session** when prompting (not just claude process)
2. **Only resumable sessions** shown in telegram picker (have claude_session_id, not archived)
3. **Conversation logging preserved** when using clux from telegram
4. **Stream capture tags processed** (observations, file-this, etc.)
5. **Sequential processing** - no forking when attached to clux; busy lock prevents concurrent requests
6. **Shell out to clux CLI** from telegram (no direct module import)
7. **--json flag** for structured NDJSON output, enables future streaming UX
8. **Cost tracking** - result event cost_usd captured and logged
9. **Error propagation** - stderr captured, exit code checked, errors surfaced to user
10. **Timeout** - 900s default on both sides, graceful handling
11. **Atomic state writes** - temp file + rename pattern for crash safety
12. **Session validation** - verify session still exists before each prompt (auto-detach if deleted)
13. **Markdown escaping** - escape special chars in session names for telegram formatting
14. **Text-only messages** - photos/documents while attached to clux are ignored (fall through to normal handling would be confusing)

---

## Implementation Order:

### Phase 1 (clux):
1. Single-letter aliases (a/d/l/n/s)
2. `list --json` output mode
3. `prompt.py` module with streaming
4. `prompt` command + `p` alias
5. Manual testing against real session

### Phase 2 (vita):
1. `telegram_clux.py` state module
2. `handle_message` interception
3. Inline keyboard picker + callback handler
4. `handle_clux_message` processor
5. End-to-end testing from telegram
