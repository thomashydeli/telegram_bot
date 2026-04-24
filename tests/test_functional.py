"""End-to-end checks against the real `claude` CLI + MCP servers.

Not run by default. Opt in with:   uv run pytest -m functional

These tests spawn MCP subprocesses, make live API calls, and may touch the
network (yfinance via ticker-analyzer). They cost a few cents per run.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess

import pytest

import bridge


pytestmark = [
    pytest.mark.functional,
    pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed"),
]


def test_mcp_list_shows_both_servers_connected():
    """`claude mcp list` spawns each stdio server for a health check. Cheap and offline."""
    proc = subprocess.run(
        ["claude", "mcp", "list"],
        cwd=str(bridge.REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"claude mcp list failed: {combined}"
    assert "email-assistant" in combined
    assert "ticker-analyzer" in combined
    # Both should report healthy. The exact glyph ("✓") can be locale-sensitive,
    # so match on the word too.
    assert combined.count("Connected") >= 2, combined


def test_bridge_run_claude_json_returns_session_id():
    """Smoke test of bridge's subprocess plumbing: no MCP tool needed."""
    obj = asyncio.run(bridge.run_claude_json("Reply with only the two letters OK. No punctuation."))
    result, sid = bridge.extract_result_and_session(obj)
    assert sid, f"expected a session_id, got {sid!r}"
    assert "OK" in result.upper(), f"unexpected reply: {result!r}"


def test_bridge_can_invoke_ticker_analyzer_mcp_tool():
    """Full path: bridge -> claude -p -> ticker-analyzer MCP -> yfinance.

    Verifies that MCP_CONNECTION_NONBLOCKING=false + --allowedTools is enough
    for the bot to actually call an MCP tool without a permission prompt.
    """
    prompt = (
        "Use the ticker-analyzer MCP server's get_stock_data tool with "
        "ticker='AAPL' and period='5d'. After the tool returns, reply with "
        "just the most recent closing price as a plain number. No prose, no "
        "currency symbol."
    )
    obj = asyncio.run(bridge.run_claude_json(prompt))
    result, sid = bridge.extract_result_and_session(obj)

    assert sid, "expected a session_id"
    assert not obj.get("permission_denials"), (
        f"MCP call was denied: {obj.get('permission_denials')}"
    )
    # Claude should have actually called a tool, not replied from priors.
    assert obj.get("num_turns", 0) >= 2, (
        f"expected a multi-turn response (tool call + summary), got {obj.get('num_turns')}: {result!r}"
    )
    # The reply should look like a stock price — at least one digit.
    assert any(c.isdigit() for c in result), f"no numeric price in reply: {result!r}"
