"""Telegram ↔ Claude Code CLI bridge.

See DESIGN.md for the full picture. This module:
- Accepts messages from a single allowlisted chat.
- Dispatches each message to `claude -p`, using `--resume` to keep per-chat state.
- Enforces read-only tool use for the bot via `--disallowedTools`.
- On /new, asks Claude to distil the current conversation into memory/*.md files.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

REPO_ROOT = Path(__file__).resolve().parent
SESSIONS_DIR = REPO_ROOT / "sessions"
MEMORY_DIR = REPO_ROOT / "memory"
MEMORY_INDEX = MEMORY_DIR / "index.md"

# Tools the bot is never allowed to use. Restricts the bot only — interactive
# Claude Code sessions in this repo are unaffected (this flag is per-invocation).
DISALLOWED_TOOLS = "Write Edit NotebookEdit Bash WebFetch"

# MCP tool prefixes the bot is pre-approved to call without an interactive
# permission prompt. Keep in sync with .mcp.json.
ALLOWED_MCP_TOOLS = "mcp__email-assistant mcp__ticker-analyzer"

TELEGRAM_LIMIT = 4096

SESSIONS_DIR.mkdir(exist_ok=True)
MEMORY_DIR.mkdir(exist_ok=True)

# Populated from .env in main() so `import bridge` works without credentials.
ALLOWLIST: set[int] = set()


# --- Session file I/O ---------------------------------------------------------

def _session_path(chat_id: int) -> Path:
    return SESSIONS_DIR / f"{chat_id}.json"


def load_session(chat_id: int) -> dict:
    p = _session_path(chat_id)
    if not p.exists():
        return {"claude_session_id": None, "history": []}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {"claude_session_id": None, "history": []}


def save_session(chat_id: int, data: dict) -> None:
    _session_path(chat_id).write_text(json.dumps(data, indent=2))


def delete_session(chat_id: int) -> None:
    p = _session_path(chat_id)
    if p.exists():
        p.unlink()


# --- Claude subprocess helpers ------------------------------------------------

async def _run_claude_raw(
    prompt: str,
    *,
    resume: Optional[str] = None,
    output_json: bool = True,
    timeout: float = 180.0,
) -> str:
    cmd = [
        "claude",
        "-p",
        "--disallowedTools", DISALLOWED_TOOLS,
        "--allowedTools", ALLOWED_MCP_TOOLS,
    ]
    if output_json:
        cmd += ["--output-format", "json"]
    if resume:
        cmd += ["--resume", resume]
    cmd.append(prompt)

    # MCP_CONNECTION_NONBLOCKING=false forces the CLI to wait for MCP servers
    # to finish registering before its first API call. Without this, the bot
    # can't see mcp__* tools on a fresh session (servers come online after
    # the prompt has already been sent).
    env = {**os.environ, "MCP_CONNECTION_NONBLOCKING": "false"}

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(REPO_ROOT),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"claude timed out after {timeout:.0f}s")

    if proc.returncode != 0:
        err = stderr.decode(errors="replace").strip()[:500]
        raise RuntimeError(f"claude exited {proc.returncode}: {err}")

    return stdout.decode(errors="replace").strip()


async def run_claude_json(prompt: str, resume: Optional[str] = None) -> dict:
    raw = await _run_claude_raw(prompt, resume=resume, output_json=True)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Some CLI builds prepend warnings; try parsing the last non-empty line.
        for line in reversed(raw.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        raise RuntimeError(f"could not parse claude JSON output: {raw[:500]}")


async def run_claude_text(prompt: str, timeout: float = 300.0) -> str:
    return await _run_claude_raw(prompt, output_json=False, timeout=timeout)


def extract_result_and_session(obj: dict) -> tuple[str, Optional[str]]:
    """Pull assistant text + session id from claude's JSON, tolerating schema drift."""
    result = (
        obj.get("result")
        or obj.get("text")
        or obj.get("content")
        or obj.get("output")
        or ""
    )
    if isinstance(result, list):
        parts = []
        for item in result:
            if isinstance(item, dict):
                parts.append(item.get("text") or item.get("content") or "")
            else:
                parts.append(str(item))
        result = "".join(parts)
    session_id = (
        obj.get("session_id")
        or obj.get("sessionId")
        or obj.get("session")
    )
    return str(result), (session_id if isinstance(session_id, str) else None)


# --- Memory selection & archival ---------------------------------------------

async def select_memories(user_msg: str) -> str:
    """Ask Claude which memory files are relevant and concatenate them."""
    if not MEMORY_INDEX.exists():
        return ""
    index_text = MEMORY_INDEX.read_text().strip()
    if not index_text:
        return ""

    selection_prompt = (
        "You are a memory file selector. Given an index of long-term memory files "
        "(each line: `filename.md — one-line description`) and an incoming user "
        "message, output ONLY a newline-separated list of relevant filenames from "
        "the index. No prose, no bullets. If nothing is relevant, output exactly NONE.\n\n"
        f"--- INDEX ---\n{index_text}\n\n"
        f"--- USER MESSAGE ---\n{user_msg}\n"
    )
    try:
        raw = await run_claude_text(selection_prompt, timeout=60)
    except Exception:
        return ""
    if not raw or raw.strip().upper() == "NONE":
        return ""

    chunks: list[str] = []
    for line in raw.splitlines():
        name = line.strip().lstrip("-*").strip().strip("`").strip()
        if not name or name.upper() == "NONE":
            continue
        # Tolerate "foo.md — note" style outputs.
        name = name.split()[0]
        if not name.endswith(".md") or "/" in name or ".." in name:
            continue
        p = MEMORY_DIR / name
        if p.is_file() and p.resolve().parent == MEMORY_DIR.resolve():
            try:
                chunks.append(f"### memory/{name}\n{p.read_text()}")
            except OSError:
                continue
    return "\n\n".join(chunks)


async def archive_session(chat_id: int) -> str:
    session = load_session(chat_id)
    history = session.get("history") or []
    if not history:
        delete_session(chat_id)
        return "No conversation to archive. Session cleared."

    existing_memories: list[str] = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        if f.name == "index.md":
            continue
        try:
            existing_memories.append(f"### memory/{f.name}\n{f.read_text()}")
        except OSError:
            continue
    index_text = MEMORY_INDEX.read_text() if MEMORY_INDEX.exists() else ""
    existing_blob = "\n\n".join(existing_memories) if existing_memories else "(none)"
    convo_text = "\n\n".join(f"[{t.get('role','?')}] {t.get('content','')}" for t in history)

    prompt = (
        "You are archiving a conversation into long-term markdown memory files.\n"
        "Produce zero or more memory files plus an updated index.\n\n"
        "Output format — exactly this, nothing else:\n"
        "For each file to create or overwrite:\n"
        "FILE: <filename.md>\n"
        "CONTENT:\n"
        "<file body>\n"
        "===END===\n\n"
        "Then, once, the updated index:\n"
        "INDEX:\n"
        "<contents of memory/index.md — one line per file: `name.md — description`>\n"
        "===END===\n\n"
        "If nothing is worth archiving, output exactly the single token "
        "NOTHING_TO_ARCHIVE and nothing else.\n\n"
        f"--- EXISTING INDEX ---\n{index_text}\n\n"
        f"--- EXISTING MEMORIES ---\n{existing_blob}\n\n"
        f"--- CONVERSATION ---\n{convo_text}\n"
    )

    try:
        raw = await run_claude_text(prompt, timeout=300)
    except Exception as e:
        return f"Archival failed: {e}. Session kept."

    if "NOTHING_TO_ARCHIVE" in raw:
        delete_session(chat_id)
        return "Nothing worth archiving. Session cleared."

    wrote_files, wrote_index = _apply_archive_output(raw)
    delete_session(chat_id)
    if not wrote_files and not wrote_index:
        return "Archival produced no parseable output. Session cleared anyway."
    summary = f"Archived {len(wrote_files)} file(s)"
    if wrote_index:
        summary += " + index"
    return summary + ". Session cleared."


def _apply_archive_output(raw: str) -> tuple[list[str], bool]:
    """Parse FILE:/CONTENT:/===END=== blocks and the INDEX block. Writes files."""
    wrote_files: list[str] = []
    wrote_index = False
    lines = raw.splitlines()
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith("FILE:"):
            fname = stripped[len("FILE:"):].strip()
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith("CONTENT:"):
                j += 1
            if j >= len(lines):
                break
            body_start = j + 1
            k = body_start
            while k < len(lines) and lines[k].strip() != "===END===":
                k += 1
            body = "\n".join(lines[body_start:k])
            safe = Path(fname).name  # drop any path components
            if safe.endswith(".md") and safe and safe != "index.md":
                (MEMORY_DIR / safe).write_text(body.rstrip() + "\n")
                wrote_files.append(safe)
            i = k + 1
            continue
        if stripped.startswith("INDEX:"):
            j = i + 1
            while j < len(lines) and lines[j].strip() != "===END===":
                j += 1
            body = "\n".join(lines[i + 1:j])
            MEMORY_INDEX.write_text(body.rstrip() + "\n")
            wrote_index = True
            i = j + 1
            continue
        i += 1
    return wrote_files, wrote_index


# --- Main chat flow -----------------------------------------------------------

async def ask_claude(chat_id: int, user_msg: str) -> str:
    session = load_session(chat_id)
    sid = session.get("claude_session_id")

    memory_ctx = await select_memories(user_msg)
    if memory_ctx:
        prompt = (
            "You have access to the following long-term memories for this user:\n\n"
            f"{memory_ctx}\n\n"
            f"--- USER MESSAGE ---\n{user_msg}"
        )
    else:
        prompt = user_msg

    try:
        obj = await run_claude_json(prompt, resume=sid)
    except Exception:
        if not sid:
            raise
        # Session id may be stale/evicted — retry once fresh.
        obj = await run_claude_json(prompt, resume=None)

    result, new_sid = extract_result_and_session(obj)
    session["claude_session_id"] = new_sid or sid
    session["history"].append({"role": "user", "content": user_msg})
    session["history"].append({"role": "assistant", "content": result})
    save_session(chat_id, session)
    return result or "(empty reply)"


# --- Telegram handlers --------------------------------------------------------

HELP_TEXT = (
    "Commands:\n"
    "/new — archive the current conversation into long-term memory, then start fresh\n"
    "/help — show this message\n"
    "anything else — passed to Claude"
)


def _allowed(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.id in ALLOWLIST)


async def _send_chunks(update: Update, text: str) -> None:
    text = text or "(empty reply)"
    chat = update.effective_chat
    for i in range(0, len(text), TELEGRAM_LIMIT):
        await chat.send_message(text[i:i + TELEGRAM_LIMIT])


async def help_handler(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    await update.effective_chat.send_message(HELP_TEXT)


async def new_handler(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    await update.effective_chat.send_chat_action(ChatAction.TYPING)
    msg = await archive_session(update.effective_chat.id)
    await update.effective_chat.send_message(msg)


async def message_handler(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    if not update.message or not update.message.text:
        return
    chat = update.effective_chat
    await chat.send_chat_action(ChatAction.TYPING)
    try:
        reply = await ask_claude(chat.id, update.message.text)
    except Exception as e:
        reply = f"Error: {e}"
    await _send_chunks(update, reply)


def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    bot_token = os.environ["BOT_TOKEN"]
    my_chat_id = int(os.environ["MY_CHAT_ID"])
    ALLOWLIST.add(my_chat_id)

    app = Application.builder().token(bot_token).build()
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(CommandHandler("new", new_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
