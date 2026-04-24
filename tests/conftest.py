"""Shared fixtures. Each test gets fresh tmp dirs for sessions/memory + a clean allowlist."""

from __future__ import annotations

import pytest

import bridge


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    """Redirect SESSIONS_DIR, MEMORY_DIR, MEMORY_INDEX into a per-test tmp tree."""
    sessions = tmp_path / "sessions"
    memory = tmp_path / "memory"
    sessions.mkdir()
    memory.mkdir()
    monkeypatch.setattr(bridge, "SESSIONS_DIR", sessions)
    monkeypatch.setattr(bridge, "MEMORY_DIR", memory)
    monkeypatch.setattr(bridge, "MEMORY_INDEX", memory / "index.md")
    return tmp_path


@pytest.fixture(autouse=True)
def clean_allowlist(monkeypatch):
    monkeypatch.setattr(bridge, "ALLOWLIST", set())
