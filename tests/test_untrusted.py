# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 crossVault GmbH
"""The untrusted-content marker (an acquired session can carry planted instructions) survives
resume -> push, craft and push --into, so resuming anything derived from it keeps Claude Code's
permission prompts on."""
import os

import pytest

from a4sx import cli, config, sdk

from conftest import make_transcript
from stub_server import VALID_TOKEN

BYPASS = "--dangerously-skip-permissions"


@pytest.fixture
def env(stub, monkeypatch):
    monkeypatch.setenv("A4SX_BASE", stub.base_url)
    monkeypatch.setenv("A4SX_SESSION", VALID_TOKEN)
    monkeypatch.setenv("A4SX_SESSION_BASE", stub.base_url)
    stub.listings = [{"id": "L1", "title": "someone else's session"}]
    stub.transcripts["L1"] = make_transcript("seller-sid")
    stub.add_session("mine", make_transcript("my-sid"), label="mine")
    return stub


def _resume_flags(name, tmp_path, monkeypatch):
    """The argv `a4sx resume NAME -- --dangerously-skip-permissions` would launch."""
    ran = []
    monkeypatch.setattr(cli, "_exec_claude", lambda argv, cwd: ran.append(argv) or 0)
    work = tmp_path / f"w-{len(os.listdir(tmp_path))}"
    work.mkdir()
    assert cli.main(["resume", name, "--cwd", str(work), "--", BYPASS]) == 0
    return ran[0]


def _another_machine():
    """Drop the local registry: what remains is only what the server knows."""
    try:
        os.unlink(os.path.join(config.config_dir(), "untrusted.json"))
    except FileNotFoundError:
        pass


def test_own_sessions_stay_trusted(env, tmp_path, monkeypatch, capsys):
    assert BYPASS in _resume_flags("mine", tmp_path, monkeypatch)
    assert cli.main(["craft", "mine", "--label", "own-craft"]) == 0
    assert BYPASS in _resume_flags("own-craft", tmp_path, monkeypatch)


def test_resume_then_push_keeps_the_marker(env, tmp_path, monkeypatch):
    c = sdk.Client()
    r = c.resume("L1", cwd=str(tmp_path))
    assert r.fetched.untrusted and sdk.read_lineage(r.local.session_id)["untrusted"] is True
    res = c.push_file(r.local.path, label="continued")
    assert res.untrusted and res.parent.startswith("acq-L1")
    _another_machine()
    assert c.is_untrusted("continued")                    # via the server-side parent edge
    assert BYPASS not in _resume_flags("continued", tmp_path, monkeypatch)


def test_craft_from_an_acquired_session_keeps_the_marker(env, tmp_path, monkeypatch, capsys):
    sdk.Client().acquire("L1")
    assert cli.main(["craft", "mine", "acq-L1", "--label", "mixed"]) == 0
    assert "marked untrusted" in capsys.readouterr().err
    push = env.calls("POST", "/library/push")[-1]
    assert push.query["parent"] == "acq-L1"               # server-visible edge to the acquired source
    assert BYPASS not in _resume_flags("mixed", tmp_path, monkeypatch)
    _another_machine()
    assert sdk.Client().is_untrusted("mixed")
    assert BYPASS not in _resume_flags("mixed", tmp_path, monkeypatch)


def test_craft_with_explicit_parent_still_marks_locally(env, tmp_path, monkeypatch):
    sdk.Client().acquire("L1")
    assert cli.main(["craft", "acq-L1", "--label", "m2", "--parent", "mine"]) == 0
    assert env.calls("POST", "/library/push")[-1].query["parent"] == "mine"
    assert BYPASS not in _resume_flags("m2", tmp_path, monkeypatch)


def test_craft_from_a_local_file_resumed_from_an_acquired_session(env, tmp_path, monkeypatch):
    r = sdk.Client().resume("L1", cwd=str(tmp_path))
    assert cli.main(["craft", r.local.path, "--label", "from-file"]) == 0
    assert env.calls("POST", "/library/push")[-1].query["parent"] == "acq-L1"
    assert BYPASS not in _resume_flags("from-file", tmp_path, monkeypatch)


def test_push_into_a_trusted_entry_marks_it(env, tmp_path, monkeypatch):
    c = sdk.Client()
    r = c.resume("L1", cwd=str(tmp_path))
    assert c.push_file(r.local.path, into="mine").untrusted
    assert BYPASS not in _resume_flags("mine", tmp_path, monkeypatch)


def test_trust_base_is_the_explicit_override(env, tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(cli, "_exec_claude", lambda argv, cwd: ran.append(argv) or 0)
    assert cli.main(["resume", "L1", "--cwd", str(tmp_path), "--trust-base", "--", BYPASS]) == 0
    assert BYPASS in ran[0]
