# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 crossVault GmbH
import json
import os

import pytest

from a4sx import transcript as t

from conftest import make_transcript


def test_project_dir_name_matches_claude_code():
    want = "-home-me-work-my-repo--claude-worktrees-x"
    assert t.project_dir_name("/home/me/work/my-repo/.claude/worktrees/x") == want
    assert t.project_dir_name("/a/b.c_d e") == "-a-b-c-d-e"


def test_projects_dir_honours_claude_config_dir(monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/srv/x")
    assert t.claude_projects_dir() == os.path.join("/srv/x", "projects")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert t.claude_projects_dir().endswith(os.path.join(".claude", "projects"))
    assert t.claude_projects_dir("/explicit") == os.path.join("/explicit", "projects")


def test_latest_local_session(tmp_path):
    assert t.latest_local_session() is None
    proj = tmp_path / "claude" / "projects" / "-p"
    proj.mkdir(parents=True)
    old, new = proj / "a.jsonl", proj / "b.jsonl"
    old.write_bytes(b"{}\n")
    new.write_bytes(b"{}\n")
    os.utime(old, (1, 1))
    assert t.latest_local_session() == str(new)


def test_summarize_and_ids():
    data = make_transcript("sid-1")
    s = t.summarize(data)
    assert s["session_id"] == "sid-1" and s["cwd"] == "/work/app"
    assert (s["records"], s["user_turns"], s["assistant_turns"]) == (2, 1, 1)
    assert s["first_user"] == "fix the flaky test"
    assert t.session_id_of(b"\n\nnot json\n" + data) == "sid-1"
    assert t.recorded_cwd_of(data) == "/work/app"


def test_rekey_changes_only_session_id():
    data = make_transcript("old") + b"garbage line\n\n"
    out = [json.loads(x) for x in t.rekey(data, "new").decode().splitlines()]
    assert [r["sessionId"] for r in out] == ["new", "new"]
    assert [r["uuid"] for r in out] == ["u1", "u2"] and out[1]["parentUuid"] == "u1"


def test_resume_locally_writes_where_claude_code_looks(tmp_path):
    data = make_transcript("base-sid")
    cwd = tmp_path / "my.app"
    cwd.mkdir()
    local = t.resume_locally(data, cwd=str(cwd))
    assert local.base_session_id == "base-sid" and local.session_id != "base-sid"
    expected_dir = tmp_path / "claude" / "projects" / t.project_dir_name(str(cwd))
    assert local.path == str(expected_dir / f"{local.session_id}.jsonl")
    rows = [json.loads(x) for x in open(local.path)]
    assert {r["sessionId"] for r in rows} == {local.session_id}
    assert local.resume_command == ["claude", "--resume", local.session_id]
    assert not [n for n in os.listdir(expected_dir) if n.endswith(".part")]


def test_resume_locally_never_overwrites_and_validates(tmp_path):
    data = make_transcript()
    t.resume_locally(data, cwd=str(tmp_path), session_id="fixed-id")
    with pytest.raises(FileExistsError):
        t.resume_locally(data, cwd=str(tmp_path), session_id="fixed-id")
    with pytest.raises(ValueError):
        t.resume_locally(data, cwd=str(tmp_path), session_id="../escape")
    with pytest.raises(ValueError):
        t.resume_locally(b'{"type":"user"}\n', cwd=str(tmp_path))


def test_token_stats():
    s = t.token_stats(make_transcript())
    assert s["has_usage"] and s["current"] == 1100 and s["peak"] == 1100 and s["output"] == 50 and s["turns"] == 1
    s = t.token_stats(b'{"type":"user","message":{"content":"' + b"x" * 400 + b'"}}\n')
    assert not s["has_usage"] and s["est_tokens"] == s["bytes"] // 4


def _rows(*recs):
    return "".join(json.dumps(r) + "\n" for r in recs).encode()


def test_trim_to_last_compact():
    data = _rows(
        {"uuid": "a", "parentUuid": None, "sessionId": "s"},
        {"uuid": "b", "parentUuid": "a", "sessionId": "s", "type": "system", "subtype": "compact_boundary"},
        {"uuid": "c", "parentUuid": "b", "sessionId": "s", "isCompactSummary": True},
        {"uuid": "d", "parentUuid": "c", "sessionId": "s"},
    )
    out, n = t.trim_to_last_compact(data)
    rows = [json.loads(x) for x in out.decode().splitlines()]
    assert n == 3 and [r["uuid"] for r in rows] == ["b", "c", "d"] and rows[0]["parentUuid"] is None
    assert t.has_compaction(data)
    assert t.trim_to_last_compact(make_transcript()) is None
    assert not t.has_compaction(make_transcript())


def _use(i):
    return {"type": "tool_use", "id": i, "name": "x", "input": {}}


def _res(i):
    return {"type": "tool_result", "tool_use_id": i, "content": "ok"}


def test_craft_guard_reconciles_by_order_and_occurrence():
    recs = [
        {"message": {"content": [_res("early")]}},                    # result before its use
        {"message": {"model": "m1", "content": [_use("early"), _use("a")]}},
        {"message": {"content": [_res("a")]}},
        {"message": {"model": "m2", "content": [_use("a")]}},         # duplicate id
        {"message": {"content": [_res("a")]}},                         # result of the duplicate
        {"subtype": "compact_boundary"},
        {"message": {"content": [_use("never")]}},                     # never closed
    ]
    out, rep = t.craft_guard(recs)
    assert rep["boundary"] == 1 and rep["dup_use"] == 1 and rep["orphan_results"] == 2
    assert rep["models"] == ["m1", "m2"]
    uses = [b["id"] for r in out for b in (r.get("message") or {}).get("content", []) if b["type"] == "tool_use"]
    results = [b["tool_use_id"] for r in out for b in (r.get("message") or {}).get("content", [])
               if b["type"] == "tool_result"]
    assert uses == ["a"] and results == ["a"]


def test_stitch_rethreads_under_one_session():
    blob, sid, rep = t.stitch([make_transcript("one"), make_transcript("two")])
    rows = [json.loads(x) for x in blob.decode().splitlines()]
    assert rep["records"] == 4 and {r["sessionId"] for r in rows} == {sid}
    assert rows[0]["parentUuid"] is None
    assert all(rows[i]["parentUuid"] == rows[i - 1]["uuid"] for i in range(1, 4))
    assert len({r["uuid"] for r in rows}) == 4
    with pytest.raises(ValueError):
        t.stitch([b"nothing here\n"])
