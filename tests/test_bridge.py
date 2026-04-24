"""Unit tests for pure logic in bridge.py. No subprocess, no network."""

from __future__ import annotations

import json
from types import SimpleNamespace

import bridge


# --- Session I/O -------------------------------------------------------------

def test_load_session_missing_returns_empty(tmp_state):
    s = bridge.load_session(12345)
    assert s == {"claude_session_id": None, "history": []}


def test_session_roundtrip(tmp_state):
    data = {
        "claude_session_id": "sid-abc",
        "history": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ],
    }
    bridge.save_session(42, data)

    # File is actually written to the monkeypatched dir
    written = (tmp_state / "sessions" / "42.json").read_text()
    assert json.loads(written) == data

    assert bridge.load_session(42) == data


def test_load_session_corrupt_json_returns_default(tmp_state):
    (tmp_state / "sessions" / "7.json").write_text("{not json")
    assert bridge.load_session(7) == {"claude_session_id": None, "history": []}


def test_delete_session_removes_file(tmp_state):
    bridge.save_session(1, {"claude_session_id": None, "history": []})
    assert (tmp_state / "sessions" / "1.json").exists()
    bridge.delete_session(1)
    assert not (tmp_state / "sessions" / "1.json").exists()


def test_delete_session_noop_when_absent(tmp_state):
    bridge.delete_session(999)  # must not raise


# --- extract_result_and_session ----------------------------------------------

def test_extract_standard_shape():
    obj = {"result": "hello there", "session_id": "sid-1", "total_cost_usd": 0.01}
    assert bridge.extract_result_and_session(obj) == ("hello there", "sid-1")


def test_extract_alt_keys():
    obj = {"text": "alt", "sessionId": "sid-camel"}
    assert bridge.extract_result_and_session(obj) == ("alt", "sid-camel")


def test_extract_list_content_blocks():
    obj = {
        "result": [
            {"text": "part one "},
            {"content": "part two"},
            "part three",
        ],
        "session_id": "sid-x",
    }
    result, sid = bridge.extract_result_and_session(obj)
    assert result == "part one part twopart three"
    assert sid == "sid-x"


def test_extract_missing_everything_returns_empty_and_none():
    assert bridge.extract_result_and_session({}) == ("", None)


def test_extract_non_string_session_id_ignored():
    obj = {"result": "ok", "session_id": 12345}
    result, sid = bridge.extract_result_and_session(obj)
    assert result == "ok"
    assert sid is None


# --- _apply_archive_output ---------------------------------------------------

def test_apply_archive_single_file_and_index(tmp_state):
    raw = (
        "FILE: preferences.md\n"
        "CONTENT:\n"
        "User prefers concise replies.\n"
        "Uses uv, not pip.\n"
        "===END===\n"
        "INDEX:\n"
        "preferences.md — user collaboration preferences\n"
        "===END===\n"
    )
    wrote, wrote_index = bridge._apply_archive_output(raw)

    assert wrote == ["preferences.md"]
    assert wrote_index is True

    prefs = (tmp_state / "memory" / "preferences.md").read_text()
    assert "User prefers concise replies." in prefs
    assert "Uses uv, not pip." in prefs
    # Trailing newline normalised, no stray blanks
    assert prefs.endswith("\n")

    idx = (tmp_state / "memory" / "index.md").read_text()
    assert idx.strip() == "preferences.md — user collaboration preferences"


def test_apply_archive_multiple_files(tmp_state):
    raw = (
        "FILE: a.md\n"
        "CONTENT:\n"
        "alpha\n"
        "===END===\n"
        "FILE: b.md\n"
        "CONTENT:\n"
        "bravo line 1\n"
        "bravo line 2\n"
        "===END===\n"
        "INDEX:\n"
        "a.md — alpha\n"
        "b.md — bravo\n"
        "===END===\n"
    )
    wrote, wrote_index = bridge._apply_archive_output(raw)
    assert sorted(wrote) == ["a.md", "b.md"]
    assert wrote_index is True
    assert (tmp_state / "memory" / "a.md").read_text().strip() == "alpha"
    assert (tmp_state / "memory" / "b.md").read_text().splitlines() == [
        "bravo line 1",
        "bravo line 2",
    ]


def test_apply_archive_rejects_path_traversal(tmp_state):
    raw = (
        "FILE: ../../evil.md\n"
        "CONTENT:\n"
        "pwned\n"
        "===END===\n"
    )
    wrote, wrote_index = bridge._apply_archive_output(raw)
    # ../../evil.md collapses via Path().name to "evil.md" and is accepted,
    # but critically it is written inside MEMORY_DIR, not outside it.
    assert wrote == ["evil.md"]
    assert (tmp_state / "memory" / "evil.md").exists()
    # And nothing escaped the tmp dir:
    assert not (tmp_state.parent / "evil.md").exists()


def test_apply_archive_rejects_index_md_as_file(tmp_state):
    # index.md should only ever be written via the INDEX: block, not FILE:.
    raw = (
        "FILE: index.md\n"
        "CONTENT:\n"
        "junk\n"
        "===END===\n"
    )
    wrote, wrote_index = bridge._apply_archive_output(raw)
    assert wrote == []
    assert wrote_index is False
    assert not (tmp_state / "memory" / "index.md").exists()


def test_apply_archive_rejects_non_markdown_filename(tmp_state):
    raw = (
        "FILE: secrets.txt\n"
        "CONTENT:\n"
        "bad\n"
        "===END===\n"
    )
    wrote, wrote_index = bridge._apply_archive_output(raw)
    assert wrote == []
    assert not (tmp_state / "memory" / "secrets.txt").exists()


def test_apply_archive_unparseable_noop(tmp_state):
    wrote, wrote_index = bridge._apply_archive_output("just some prose, no markers")
    assert wrote == []
    assert wrote_index is False


def test_apply_archive_empty_string(tmp_state):
    wrote, wrote_index = bridge._apply_archive_output("")
    assert wrote == []
    assert wrote_index is False


# --- Allowlist ---------------------------------------------------------------

def _fake_update(chat_id):
    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id))


def test_allowed_true_for_allowlisted_chat(monkeypatch):
    monkeypatch.setattr(bridge, "ALLOWLIST", {111})
    assert bridge._allowed(_fake_update(111)) is True


def test_allowed_false_for_other_chat(monkeypatch):
    monkeypatch.setattr(bridge, "ALLOWLIST", {111})
    assert bridge._allowed(_fake_update(222)) is False


def test_allowed_false_when_no_chat():
    update = SimpleNamespace(effective_chat=None)
    assert bridge._allowed(update) is False
