# telegram_bot

A personal Telegram bot that bridges to the [Claude Code](https://docs.claude.com/en/docs/claude-code/overview) CLI. Messages sent in Telegram are forwarded to `claude -p`, which can call local MCP servers (email triage, market data, …) and reply back in the chat. Each chat keeps its own Claude session, and long-term context can be archived into a simple markdown memory store on demand.

## Features

- **Chat-as-session** — each Telegram chat resumes a dedicated Claude session, so follow-up messages keep full context without re-sending history.
- **Pluggable tools** — MCP servers listed in `.mcp.json` are auto-loaded. Add a server, restart, and Claude can use it.
- **Long-term memory** — `/new` archives the current conversation into `memory/*.md`; relevant entries are prepended on future messages.
- **Single-user allowlist** — messages from any chat other than `MY_CHAT_ID` are silently dropped.
- **Read-only sandbox** — the bot invokes Claude with `--disallowedTools "Write Edit NotebookEdit Bash WebFetch"`. Interactive Claude Code sessions run from this repo are unaffected.

## Prerequisites

- [uv](https://docs.astral.sh/uv/)
- The `claude` CLI on your `PATH`
- A bot token from [@BotFather](https://t.me/BotFather) and your numeric chat id from [@userinfobot](https://t.me/userinfobot)

## Setup

```bash
# 1. Clone with submodules (MCP servers live as submodules)
git clone --recurse-submodules <repo-url>
cd telegram_bot

# 2. Install dependencies
uv sync

# 3. Fill in secrets
cp .env.example .env
$EDITOR .env               # set BOT_TOKEN and MY_CHAT_ID

# 4. Point MCP config at your clone
cp .mcp.json.example .mcp.json
$EDITOR .mcp.json          # replace the placeholder paths with absolute paths
                           # to mcp-servers/<name> on your machine

# 5. Run
uv run bridge.py
```

Already cloned without `--recurse-submodules`? Run `git submodule update --init --recursive`.

## Commands in chat

| Command | Effect |
|---|---|
| `/help` | Show available commands |
| `/new`  | Archive the current conversation into long-term memory and start fresh |
| anything else | Forwarded to Claude |

## Tests

```bash
uv run pytest                 # unit tests only (fast, no subprocess)
uv run pytest -m functional   # end-to-end against the real claude CLI + MCP
```

Functional tests spawn the Claude CLI and MCP servers and make real API calls, so they cost a few cents and take ~15s.

## Adding an MCP server

1. `git submodule add <repo-url> mcp-servers/<name>`
2. Add a new entry under `mcpServers` in `.mcp.json` with the server's `command` and `args`.
3. Add `mcp__<name>` to `ALLOWED_MCP_TOOLS` in `bridge.py` so the bot is pre-approved to call its tools.
4. Restart the bridge.
