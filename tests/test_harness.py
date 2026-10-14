# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 crossVault GmbH
"""Sessions recorded with another harness (Codex CLI, Grok Build, ...): they save, list and fetch like
Claude Code ones, but only Claude Code sessions resume, and a marketplace listing is checked BEFORE it
is acquired, failing closed when the check can't be made."""
import json
import os

import pytest

from a4sx import cli, sdk

from conftest import make_transcript
from stub_server import VALID_TOKEN

CODEX = b'{"type":"session_meta","payload":{"id":"x"}}\n'


@pytest.fixture
def authed(client):
    client._token = VALID_TOKEN
    return client


@pytest.mark.parametrize("value,want", [
    ("codex-cli", "codex-cli"), ({"id": "Grok-Build", "version": "1"}, "grok-build"),
    (None, "unknown"), ("", "unknown"), ({"version": "1"}, "unknown"),
])
def test_harness_id(value, want):
    assert sdk.harness_id(value) == want


def test_harness_name():
    assert sdk.harness_name({"id": "codex-cli"}) == "Codex CLI"
    assert sdk.harness_name("something-new") == "something-new"


def test_a_codex_session_in_the_library_is_fetched_but_not_resumed(authed, stub, tmp_path):
    stub.add_session("c-codex", CODEX, label="cx", harness={"id": "codex-cli", "version": "0.40"})
    stub.native["c-codex"] = "native/codex-cli.jsonl"
    got = authed.fetch("cx")
    assert got.transcript == CODEX and got.harness == "codex-cli"
    with pytest.raises(sdk.OtherHarness) as e:
        authed.resume("cx", cwd=str(tmp_path))
    assert e.value.harness == "codex-cli" and "Codex CLI" in str(e.value)
    assert not os.path.exists(os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects"))


def test_a_native_layout_alone_identifies_the_harness(authed, stub, tmp_path):
    stub.add_session("c-grok", b"{}\n", label="g")   # no harness label from the server
    stub.native["c-grok"] = "native/grok-build.jsonl"
    with pytest.raises(sdk.OtherHarness) as e:
        authed.resume("g", cwd=str(tmp_path))
    assert e.value.harness == "grok-build"


def test_unknown_harness_keeps_resuming(authed, stub, tmp_path):
    stub.add_session("c-old", make_transcript("old"), label="old")
    assert authed.resume("old", cwd=str(tmp_path)).fetched.harness == "unknown"


def test_a_listing_of_another_harness_is_refused_before_the_acquire(authed, stub, tmp_path):
    stub.listings.append({"id": "L-cx", "title": "codex run", "harness": {"id": "codex-cli"}})
    with pytest.raises(sdk.OtherHarness):
        authed.resume("L-cx", cwd=str(tmp_path))
    assert not [r for r in stub.requests if r.path.startswith("/library/acquire/")]


def test_a_hidden_listing_is_checked_through_the_collection(authed, stub, tmp_path):
    # the public listing answers 404 (not listed); the caller may still read the collection
    stub.queue("GET", "/memory/discover/item", 404, {"error": {"code": "collection_not_found"}})
    stub.listings.append({"id": "L-g", "title": "grok", "harness": "grok-build"})
    with pytest.raises(sdk.OtherHarness):
        authed.resume("L-g", cwd=str(tmp_path))
    assert stub.calls("GET", "/memory/collections/L-g")
    assert not [r for r in stub.requests if r.path.startswith("/library/acquire/")]


@pytest.mark.parametrize("status", [500, 503])
def test_the_listing_check_fails_closed(authed, stub, tmp_path, status):
    stub.listings.append({"id": "L-cc", "title": "claude run", "harness": "claude-code"})
    stub.queue("GET", "/memory/discover/item", status, {})
    with pytest.raises(sdk.ApiError):
        authed.resume("L-cc", cwd=str(tmp_path))
    assert not [r for r in stub.requests if r.path.startswith("/library/acquire/")]


def test_a_claude_listing_is_acquired_and_resumed(authed, stub, tmp_path):
    stub.listings.append({"id": "L-ok", "title": "claude run", "harness": {"id": "claude-code"}})
    stub.transcripts["L-ok"] = make_transcript("seller")
    r = authed.resume("L-ok", cwd=str(tmp_path))
    assert r.fetched.acquired_now and r.fetched.untrusted


def test_plain_fetch_does_no_listing_check(authed, stub):
    stub.listings.append({"id": "L-cx", "title": "codex run", "harness": {"id": "codex-cli"}})
    stub.transcripts["L-cx"] = CODEX
    assert authed.fetch("L-cx").acquired_now
    assert not stub.calls("GET", "/memory/discover/item")


def test_push_reports_the_recognised_harness(authed, stub):
    stub.push_harness = "codex-cli"
    assert authed.push(CODEX).harness == "codex-cli"


# --- Grok Build session folders ---------------------------------------------------------------------


def _grok_folder(tmp_path):
    d = tmp_path / "grok" / "sess-1"
    d.mkdir(parents=True)
    (d / "updates.jsonl").write_bytes(b'{"timestamp":1,"method":"m","params":{}}\n')
    (d / "chat_history.jsonl").write_bytes(b"not uploaded\n")
    return d


def test_a_grok_folder_pushes_its_updates_log(tmp_path):
    d = _grok_folder(tmp_path)
    assert sdk.push_path(str(d)) == str(d / "updates.jsonl")


def test_a_grok_sidecar_is_refused(tmp_path):
    d = _grok_folder(tmp_path)
    with pytest.raises(sdk.A4sxError, match="only updates.jsonl"):
        sdk.push_path(str(d / "chat_history.jsonl"))


def test_a_folder_without_a_log_is_refused(tmp_path):
    with pytest.raises(sdk.A4sxError, match="has none"):
        sdk.push_path(str(tmp_path))


@pytest.mark.skipif(os.name != "posix", reason="symlinks")
def test_a_symlinked_log_is_refused(tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    (d / "chat_history.jsonl").write_bytes(b"x\n")
    os.symlink(d / "chat_history.jsonl", d / "updates.jsonl")
    with pytest.raises(sdk.A4sxError, match="symlink"):
        sdk.push_path(str(d))


def test_other_files_pass_through(tmp_path):
    p = tmp_path / "rollout-1.jsonl"
    p.write_bytes(CODEX)
    assert sdk.push_path(str(p)) == str(p)


def test_cli_push_of_a_grok_folder(stub, tmp_path, capsys):
    from a4sx import config

    config.save_token(VALID_TOKEN, stub.base_url)
    stub.push_harness = "grok-build"
    d = _grok_folder(tmp_path)
    assert cli.main(["push", str(d), "--base", stub.base_url]) == 0
    sent = stub.calls("POST", "/library/push")[0]
    assert sent.body == (d / "updates.jsonl").read_bytes()
    assert json.loads(capsys.readouterr().out)["harness"]["id"] == "grok-build"


def test_cli_resume_of_another_harness_exits_1(stub, tmp_path, capsys):
    from a4sx import config

    config.save_token(VALID_TOKEN, stub.base_url)
    stub.add_session("c-codex", CODEX, label="cx", harness="codex-cli")
    stub.native["c-codex"] = "native/codex-cli.jsonl"
    assert cli.main(["resume", "cx", "--no-launch", "--cwd", str(tmp_path), "--base", stub.base_url]) == 1
    assert "only Claude Code sessions can be resumed" in capsys.readouterr().err


def test_search_filters_by_harness(authed, stub):
    stub.add_session("c-1", b"", label="cc", harness="claude-code")
    stub.add_session("c-2", b"", label="cx", harness={"id": "codex-cli", "version": "0.40"})
    stub.add_session("c-3", b"", label="old")
    got = authed.search(scope="library", harness=["codex-cli", "unknown"])
    assert [r["id"] for r in got["items"]] == ["c-2", "c-3"]
    authed.search("x", harness=["grok-build", "codex-cli"])
    assert stub.calls("GET", "/memory/discover")[-1].query["harness"] == "codex-cli,grok-build"
    authed.search("x")
    assert "harness" not in stub.calls("GET", "/memory/discover")[-1].query
