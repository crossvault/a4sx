# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 crossVault GmbH
"""Security properties from the pre-release review: tokens are bound to their server, every upload
is pre-scrubbed, sign-in links must be on the server's origin, resumed sessions are private."""
import json
import os
import stat

import pytest

from a4sx import cli, config, sdk
from a4sx.transcript import project_dir_name, resume_locally

from conftest import make_transcript
from stub_server import VALID_TOKEN

GH = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3"


def _authorized(stub):
    return [r for r in stub.requests if r.auth]


# --- H1: a token only ever goes to the server that issued it ---------------------------------------


def test_a_token_from_login_is_never_sent_to_another_base(stub, attacker, monkeypatch, capsys):
    monkeypatch.setenv("A4SX_NO_BROWSER", "1")
    assert cli.main(["login", "--base", stub.base_url]) == 0
    capsys.readouterr()
    # an explicit --base, the global --base, and an injected $A4SX_BASE all point at the attacker
    assert cli.main(["list", "--base", attacker.base_url]) == 1
    assert cli.main(["--base", attacker.base_url, "whoami"]) == 1
    monkeypatch.setenv("A4SX_BASE", attacker.base_url)
    assert cli.main(["whoami"]) == 1
    assert _authorized(attacker) == []
    err = capsys.readouterr().err
    assert f"not signed in to {attacker.base_url}" in err and f"signed in to {stub.base_url}" in err
    assert VALID_TOKEN not in err
    # the real server still works
    monkeypatch.delenv("A4SX_BASE")
    assert cli.main(["list"]) == 0
    assert _authorized(stub)


def test_sdk_client_for_another_origin_has_no_token(stub, attacker):
    sdk.Client(stub.base_url).login_with_token(VALID_TOKEN)
    other = sdk.Client(attacker.base_url)
    assert other.token is None
    with pytest.raises(sdk.NotLoggedIn):
        other.list_sessions()
    with pytest.raises(sdk.NotLoggedIn):
        other.push(make_transcript())
    assert _authorized(attacker) == []
    # same host, different port or scheme = different origin
    assert config.load_token(stub.base_url.replace("127.0.0.1", "localhost")) is None


def test_env_token_is_not_sent_to_an_injected_base(stub, attacker, monkeypatch, capsys):
    monkeypatch.setenv("A4SX_SESSION", VALID_TOKEN)            # meant for the public server
    monkeypatch.setenv("A4SX_BASE", attacker.base_url)
    assert cli.main(["list"]) == 1
    assert _authorized(attacker) == []


def test_each_server_needs_its_own_login(stub, attacker):
    sdk.Client(stub.base_url).login_with_token(VALID_TOKEN)
    attacker.tokens.add("a4sx-test-" + "attackerside1")
    sdk.Client(attacker.base_url).login_with_token("a4sx-test-" + "attackerside1")
    assert config.load_token(stub.base_url) == VALID_TOKEN
    assert config.load_token(attacker.base_url) == "a4sx-test-attackerside1"
    assert config.signed_in_origins() == sorted([stub.base_url, attacker.base_url])
    sdk.Client(stub.base_url).list_sessions()
    assert [r.auth for r in stub.requests if r.path == "/library/sessions"] == [f"Bearer {VALID_TOKEN}"]


def test_the_standalone_scripts_session_file_is_bound_to_its_server(stub):
    d = config.config_dir()
    os.makedirs(d)
    with open(os.path.join(d, "session"), "w") as f:
        f.write("legacy-token-123")
    assert config.load_token() == "legacy-token-123"             # no amdx.json: the public server
    assert config.load_token(stub.base_url) is None
    with open(os.path.join(d, "amdx.json"), "w") as f:
        json.dump({"base": stub.base_url}, f)
    assert config.load_token(stub.base_url) == "legacy-token-123"
    assert config.load_token() is None


# --- H2: every upload path pre-scrubs --------------------------------------------------------------


def _secret_file(tmp_path):
    f = tmp_path / "local.jsonl"
    f.write_bytes(make_transcript("s", extra=[{"type": "user", "sessionId": "s", "uuid": "u3",
                                               "message": {"content": f"token {GH}"}}]))
    return f


@pytest.fixture
def signed_in(stub, monkeypatch):
    monkeypatch.setenv("A4SX_BASE", stub.base_url)
    monkeypatch.setenv("A4SX_SESSION", VALID_TOKEN)
    monkeypatch.setenv("A4SX_SESSION_BASE", stub.base_url)
    return stub


def test_craft_scrubs_local_files(signed_in, tmp_path, capsys):
    assert cli.main(["craft", str(_secret_file(tmp_path)), "--label", "c"]) == 0
    sent = signed_in.calls("POST", "/library/push")[0].body.decode()
    assert GH not in sent and GH[4:] not in sent
    cap = capsys.readouterr()
    assert "a4sx craft: client scrub: 1 line changed (secret.github_token: 1)" in cap.err
    assert json.loads(cap.out)["client_scrub"]["rules"] == {"secret.github_token": 1}


def test_craft_no_scrub_is_explicit_and_loud(signed_in, tmp_path, capsys):
    assert cli.main(["craft", str(_secret_file(tmp_path)), "--no-scrub"]) == 0
    assert GH in signed_in.calls("POST", "/library/push")[0].body.decode()
    err = capsys.readouterr().err
    assert "WARNING: --no-scrub" in err and "client scrub: skipped" in err


def test_sdk_craft_scrubs_by_default(signed_in, tmp_path):
    res, _, _ = sdk.Client().craft([_secret_file(tmp_path).read_bytes()])
    assert GH not in signed_in.calls("POST", "/library/push")[0].body.decode()
    assert res.scrub.enabled and res.scrub.lines


# --- M1: sign-in links must be on the server's own origin ------------------------------------------


@pytest.mark.parametrize("key,url", [
    ("login_url", "https://session-exchange.com/auth/cli/choose?code=attacker"),
    ("short_url", "https://evil.example.org/d/ABCD"),
    ("verification_url", "javascript:alert(1)"),
    ("login_url", "file:///etc/passwd"),
])
def test_sign_in_links_on_another_origin_are_refused(stub, key, url, monkeypatch, capsys):
    good = {"code": "c1", "login_url": f"{stub.base_url}/auth/cli/choose?code=c1", "user_code": "ABCD-0001",
            "short_url": f"{stub.base_url}/d/ABCD", "verification_url": f"{stub.base_url}/auth/cli/device"}
    stub.queue("POST", "/auth/cli/start", 200, {**good, key: url})
    with pytest.raises(sdk.A4sxError, match="refusing"):
        sdk.Client(stub.base_url).start_device_login()
    stub.queue("POST", "/auth/cli/start", 200, {**good, key: url})
    monkeypatch.setenv("A4SX_NO_BROWSER", "1")
    assert cli.main(["login", "--base", stub.base_url]) == 1
    out = capsys.readouterr()
    assert url not in out.out and "refusing" in out.err


def test_pairing_expiry_is_clamped():
    d = sdk.DeviceLogin.from_response({"code": "c", "login_url": "https://x.example/l", "expires_in": 10 ** 9})
    assert d.expires_in == sdk.MAX_PAIRING_S


# --- M3 / L1: resumed and fetched transcripts are private ------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
def test_resumed_session_is_0600_in_new_0700_dirs(tmp_path):
    local = resume_locally(make_transcript(), cwd=str(tmp_path))
    assert stat.S_IMODE(os.stat(local.path).st_mode) == 0o600
    projects = tmp_path / "claude" / "projects"
    for d in (tmp_path / "claude", projects, projects / project_dir_name(str(tmp_path))):
        assert stat.S_IMODE(os.stat(d).st_mode) == 0o700


@pytest.mark.skipif(os.name != "posix", reason="symlinks")
def test_resume_does_not_write_through_a_planted_symlink(tmp_path):
    proj = tmp_path / "claude" / "projects" / project_dir_name(str(tmp_path))
    proj.mkdir(parents=True)
    target = tmp_path / "victim"
    target.write_text("keep")
    (proj / ".fixed.part").symlink_to(target)
    with pytest.raises(OSError):
        resume_locally(make_transcript(), cwd=str(tmp_path), session_id="fixed")
    assert target.read_text() == "keep"


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
def test_fetched_file_is_tightened_even_if_it_existed(tmp_path):
    out = tmp_path / "out.jsonl"
    out.write_text("old")
    os.chmod(out, 0o644)
    sdk.FetchedSession("c", "m", make_transcript()).save(str(out))
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600


# --- the test suite itself never launches a real program -------------------------------------------


def test_exec_is_blocked_in_tests():
    with pytest.raises(AssertionError, match="exec"):
        os.execvp("claude", ["claude"])


def test_push_no_scrub_is_loud(signed_in, tmp_path, capsys):
    assert cli.main(["push", str(_secret_file(tmp_path)), "--no-scrub"]) == 0
    assert GH in signed_in.calls("POST", "/library/push")[0].body.decode()
    err = capsys.readouterr().err
    assert "WARNING: --no-scrub" in err and "client scrub: skipped" in err
    assert GH not in err


def test_push_with_scrub_has_no_warning(signed_in, tmp_path, capsys):
    assert cli.main(["push", str(_secret_file(tmp_path))]) == 0
    assert "WARNING" not in capsys.readouterr().err
