# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 crossVault GmbH
"""The `a4sx` command, end to end against the local stub server (ported from the standalone
script's test suite: login base precedence, headless login, push scrub report, list, hints, window
sizes, resume, permission-bypass handling, error messages)."""
import argparse
import hashlib
import io
import json
import os
from datetime import timezone

import pytest

from a4sx import cli, config, sdk

from conftest import make_transcript
from stub_server import VALID_TOKEN

FAKE = "not-a-real-token-0000"
GH = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3"
SECRETS = (FAKE, GH, GH[4:])
ROWS = [
    {"sessionId": "sid-1", "type": "user", "message": {"content": f'curl -H "Authorization: Bearer {FAKE}" x'}},
    {"sessionId": "sid-1", "type": "assistant", "message": {"content": "no secrets on this line"}},
    {"sessionId": "sid-1", "type": "user", "message": {"content": f"Bearer {FAKE} and {GH}"}},
    {"sessionId": "sid-1", "type": "assistant", "message": {"content": "clean"}},
]


@pytest.fixture
def env(stub, monkeypatch):
    monkeypatch.setenv("A4SX_BASE", stub.base_url)
    monkeypatch.setattr(sdk.time, "sleep", lambda s: None)
    return stub


@pytest.fixture
def signed_in(env, monkeypatch):
    monkeypatch.setenv("A4SX_SESSION", VALID_TOKEN)
    monkeypatch.setenv("A4SX_SESSION_BASE", env.base_url)
    return env


def _file(tmp_path, rows=ROWS):
    f = tmp_path / "t.jsonl"
    f.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return f


# --- login -----------------------------------------------------------------------------------------


def test_login_uses_a4sx_base_and_says_so(env, monkeypatch, capsys):
    monkeypatch.setenv("A4SX_NO_BROWSER", "1")
    assert cli.main(["login"]) == 0
    out = capsys.readouterr().out
    assert f"signing in to {env.base_url}" in out
    assert "No browser can be opened from here (A4SX_NO_BROWSER is set)" in out
    assert "/auth/cli/d/ABCD-0001" in out and "ABCD-0001" in out
    assert VALID_TOKEN not in out
    assert config.load_token(env.base_url) == VALID_TOKEN
    assert config.load_token() is None                        # bound to the stub, not the public server


def test_base_flag_beats_the_env(env, monkeypatch, capsys):
    monkeypatch.setenv("A4SX_BASE", "https://elsewhere.example.org")
    monkeypatch.setenv("A4SX_NO_BROWSER", "1")
    assert cli.main(["login", "--base", env.base_url]) == 0
    assert all(r.path for r in env.requests)            # every call went to the stub


def test_default_is_the_public_server(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(sdk.Client, "login", lambda self, *a, **k: seen.append(self.base_url))
    assert cli.main(["login", "--no-browser"]) == 0
    assert seen == ["https://session-exchange.com"]
    assert "signing in to https://session-exchange.com" in capsys.readouterr().out


def test_login_parser_leaves_base_unset():
    a = cli.build_parser().parse_args(["login"])
    assert a.base is None and a.token is None


def test_login_token_from_stdin(env, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(VALID_TOKEN + "\n"))
    assert cli.main(["login", "--token", "-"]) == 0
    assert config.load_token(env.base_url) == VALID_TOKEN and VALID_TOKEN not in capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", io.StringIO("a4sx-test-" + "wrongwrong1\n"))
    with pytest.raises(SystemExit) as e:
        cli.main(["login", "--token"])
    assert "isn't a valid session (HTTP 401)" in str(e.value)


def test_browser_path_opens_the_login_url(env, monkeypatch, capsys):
    monkeypatch.setattr(cli, "no_browser_reason", lambda force=False: None)
    opened = []
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
    assert cli.main(["login"]) == 0
    out = capsys.readouterr().out
    assert opened and opened[0] in out and "Open this URL in your browser" in out


def test_browser_failure_falls_back_to_the_short_link(env, monkeypatch, capsys):
    monkeypatch.setattr(cli, "no_browser_reason", lambda force=False: None)
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", lambda url: False)
    monkeypatch.setattr(cli, "_qr_lines", lambda text: ["QR-LINE"])
    assert cli.main(["login"]) == 0
    out = capsys.readouterr().out
    assert "the browser could not be opened" in out and "QR-LINE" in out and "/auth/cli/d/" in out


@pytest.mark.parametrize("var,val,reason", [("SSH_CONNECTION", "1 2 3 4", "SSH"), ("A4SX_NO_BROWSER", "1", "A4SX")])
def test_headless_detection(monkeypatch, var, val, reason):
    monkeypatch.setenv(var, val)
    assert reason in cli.no_browser_reason()
    assert cli.no_browser_reason(force=True) == "--no-browser was given"


def test_logout(env, capsys):
    config.save_token(VALID_TOKEN, env.base_url)
    config.save_token("other-token-123", "https://other.example.org")
    assert cli.main(["logout"]) == 0
    assert "signed out." in capsys.readouterr().out
    assert config.load_token(env.base_url) is None
    assert config.load_token("https://other.example.org") == "other-token-123"   # other servers untouched
    assert env.calls("POST", "/auth/logout")[0].auth == f"Bearer {VALID_TOKEN}"


# --- push ------------------------------------------------------------------------------------------


def test_push_reports_client_scrub_and_shas(signed_in, tmp_path, capsys):
    f = _file(tmp_path)
    assert cli.main(["push", str(f), "--label", "lbl"]) == 0
    cap = capsys.readouterr()
    sent = signed_in.calls("POST", "/library/push")[0].body
    local, uploaded = hashlib.sha256(f.read_bytes()).hexdigest(), hashlib.sha256(sent).hexdigest()
    assert local != uploaded
    assert "a4sx push: client scrub: 2 lines changed (bearer: 2, github_token: 1) — lines 1, 3" in cap.err
    assert f"local sha256    {local}" in cap.err
    assert f"uploaded sha256 {uploaded}: DIFFERS from the local file (the client scrub rewrote it)" in cap.err
    body = json.loads(cap.out)
    cs = body["client_scrub"]
    assert cs["lines_changed"] == 2 and cs["lines"] == [1, 3] and cs["identical"] is False
    assert cs["local_sha256"] == local and cs["uploaded_sha256"] == uploaded
    assert body["redactions"] == 0 and body["status"] == "staged (private)"
    assert ("server scrub: 0 redactions (counted on the uploaded bytes; the 2 client-side line change(s) "
            "above are NOT included)") in cap.err


def test_push_clean_file_is_identical(signed_in, tmp_path, capsys):
    f = _file(tmp_path, rows=[ROWS[1], ROWS[3]])
    assert cli.main(["push", str(f)]) == 0
    cap = capsys.readouterr()
    assert signed_in.calls("POST", "/library/push")[0].body == f.read_bytes()
    assert "client scrub: 0 lines changed" in cap.err and "(identical to the local file)" in cap.err


def test_no_scrub_uploads_the_local_bytes(signed_in, tmp_path, capsys):
    f = _file(tmp_path)
    assert cli.main(["push", str(f), "--no-scrub"]) == 0
    cap = capsys.readouterr()
    assert signed_in.calls("POST", "/library/push")[0].body == f.read_bytes()
    assert "client scrub: skipped (--no-scrub)" in cap.err


def test_dry_run_makes_no_network_call_and_needs_no_login(env, tmp_path, capsys):
    f = _file(tmp_path)
    assert cli.main(["push", str(f), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert env.requests == []
    assert ": bearer  [masked" in out and ": github_token  [masked" in out and "line 3 col" in out
    assert "lines 1, 3" in out and "(DIFFERS from the local file)" in out and "dry run: nothing uploaded" in out


def test_keep_pii_sends_secrets_only_and_warns(signed_in, tmp_path, capsys):
    f = tmp_path / "pii.jsonl"
    f.write_text(json.dumps({"type": "user", "sessionId": "s", "cwd": "/home/alice/app",
                             "message": {"content": "mail ann@corp.example.net"}}) + "\n")
    assert cli.main(["push", str(f), "--keep-pii"]) == 0
    sent = signed_in.calls("POST", "/library/push")[0]
    assert sent.query["client_scrub"] == "secrets-only" and b"/home/alice/app" in sent.body
    assert "WARNING: --keep-pii" in capsys.readouterr().err
    assert cli.main(["push", str(f)]) == 0                       # default: personal data is removed
    sent = signed_in.calls("POST", "/library/push")[1]
    assert sent.query["client_scrub"] == "full" and b"alice" not in sent.body and b"ann@" not in sent.body


def test_keep_pii_and_no_scrub_conflict(signed_in, tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["push", str(_file(tmp_path)), "--keep-pii", "--no-scrub"])


@pytest.mark.parametrize("dry", [False, True])
def test_fail_on_scrub_exits_3_and_uploads_nothing(signed_in, tmp_path, dry):
    args = ["push", str(_file(tmp_path)), "--fail-on-scrub"] + (["--dry-run"] if dry else [])
    assert cli.main(args) == cli.FAIL_ON_SCRUB_EXIT == 3
    assert signed_in.calls("POST", "/library/push") == []


def test_fail_on_scrub_passes_a_clean_file(signed_in, tmp_path):
    assert cli.main(["push", str(_file(tmp_path, rows=[ROWS[1]])), "--fail-on-scrub"]) == 0


def test_fail_on_scrub_and_no_scrub_conflict(tmp_path):
    with pytest.raises(SystemExit) as e:
        cli.main(["push", str(_file(tmp_path)), "--fail-on-scrub", "--no-scrub"])
    assert "mutually exclusive" in str(e.value)


@pytest.mark.parametrize("extra", [[], ["--dry-run"], ["--fail-on-scrub"]])
def test_no_secret_values_in_any_output(signed_in, tmp_path, capsys, extra):
    cli.main(["push", str(_file(tmp_path))] + extra)
    cap = capsys.readouterr()
    for s in SECRETS + (VALID_TOKEN,):
        assert s not in cap.out and s not in cap.err


def test_push_latest(signed_in, tmp_path, capsys):
    proj = tmp_path / "claude" / "projects" / "-w"
    proj.mkdir(parents=True)
    (proj / "s.jsonl").write_bytes(make_transcript())
    assert cli.main(["push", "--latest"]) == 0
    assert "latest session:" in capsys.readouterr().err


def test_push_without_login(env, tmp_path, capsys):
    assert cli.main(["push", str(_file(tmp_path))]) == 1
    assert "not signed in" in capsys.readouterr().err


def test_expired_session_says_to_log_in_again(env, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("A4SX_SESSION", "a4sx-test-" + "expired00001")
    monkeypatch.setenv("A4SX_SESSION_BASE", env.base_url)
    assert cli.main(["list"]) == 1
    err = capsys.readouterr().err
    assert "HTTP 401 auth_invalid" in err and "run `a4sx login`" in err


# --- list ------------------------------------------------------------------------------------------


def test_fmt_ts_is_compact_and_local():
    assert cli.fmt_ts("2026-09-29T21:50:12Z", timezone.utc) == "2026-09-29 21:50"
    assert cli.fmt_ts(None) == "-" and cli.fmt_ts("garbage") == "-"


@pytest.mark.parametrize("key,expected", [("updated", ["b", "a", "c"]), ("created", ["a", "b", "c"]),
                                          ("name", ["a", "b", "c"])])
def test_sort_rows(key, expected):
    rows = [{"id": "a", "created_at": "2026-09-03T00:00:00Z", "updated_at": "2026-09-04T00:00:00Z"},
            {"id": "b", "created_at": "2026-09-02T00:00:00Z", "updated_at": "2026-09-05T00:00:00Z"},
            {"id": "c"}]
    assert [r["id"] for r in cli.sort_rows(rows, key)] == expected


def test_list_tree_with_times_and_versions(signed_in, capsys):
    signed_in.add_session("c-1", b"", label="base", title="Base", created_at="2026-09-20T10:00:00Z",
                          updated_at="2026-09-29T21:50:00Z", versions=4, tokens={"current": 182000})
    signed_in.add_session("c-2", b"", label="spin-off", title="Child", parent={"cid": "c-1"},
                          created_at="2026-09-27T08:00:00Z", updated_at="2026-09-27T08:00:00Z", versions=1)
    signed_in.add_session("c-3", b"", label="orphan", parent={"cid": "gone"})
    assert cli.main(["list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split()[:6] == ["#", "name", "created", "updated", "vers", "ctx"]
    assert "base" in lines[1] and " 4 " in lines[1] and "182k" in lines[1]
    assert "└─ spin-off" in lines[2]
    assert "(parent deleted)" in lines[3]


def test_list_empty_and_json(signed_in, capsys):
    assert cli.main(["list"]) == 0
    assert "your library is empty" in capsys.readouterr().out
    signed_in.add_session("c-1", b"", label="x")
    assert cli.main(["list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == "c-1"


def test_unknown_sort_is_rejected(capsys):
    with pytest.raises(SystemExit):
        cli.main(["list", "--sort", "size"])


# --- search / acquire / fetch / delete -------------------------------------------------------------


def test_search_public(env, capsys):
    env.listings = [{"id": "L1", "tenant": "acme", "title": "Pytest flakes", "price_cents": 0},
                    {"id": "L2", "title": "Paid pytest", "price_cents": 1250, "currency": "eur"}]
    assert cli.main(["search", "pytest"]) == 0
    out = capsys.readouterr().out
    assert "L1  --workspace acme" in out and "[free]" in out and "[12.50 EUR]" in out
    assert env.requests[0].auth is None


def test_acquire_messages(signed_in, capsys):
    signed_in.listings = [{"id": "free"}, {"id": "paid", "price_cents": 100}]
    assert cli.main(["acquire", "free"]) == 0
    cap = capsys.readouterr()
    assert json.loads(cap.out)["collection"] == "acq-free" and "a4sx resume acq-free" in cap.err
    with pytest.raises(SystemExit) as e:
        cli.main(["acquire", "paid"])
    assert "paid listing" in str(e.value)


def test_fetch_writes_the_transcript(signed_in, tmp_path, capsys):
    signed_in.add_session("c-1", make_transcript("sid-f"), label="w")
    out = tmp_path / "out.jsonl"
    assert cli.main(["fetch", "w", "-o", str(out)]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["internal_sid"] == "sid-f" and d["path"] == str(out) and out.read_bytes() == make_transcript("sid-f")


def test_delete(signed_in, capsys):
    signed_in.add_session("c-1", b"", label="w")
    assert cli.main(["delete", "w"]) == 0
    assert "erased within 24 hours" in capsys.readouterr().err
    assert cli.main(["delete", "w"]) == 1


# --- resume ----------------------------------------------------------------------------------------


def test_resume_no_launch(signed_in, tmp_path, capsys):
    signed_in.add_session("c-1", make_transcript("base"), label="w")
    work = tmp_path / "work"
    work.mkdir()
    assert cli.main(["resume", "w", "--cwd", str(work), "--no-launch", "--", "--model", "x"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert os.path.isfile(d["path"]) and d["cwd"] == str(work)
    assert d["command"] == ["claude", "--resume", d["session_id"], "--model", "x"]


def test_resume_execs_claude_in_the_work_dir(signed_in, tmp_path, monkeypatch, capsys):
    signed_in.add_session("c-1", make_transcript("base"), label="w")
    ran = []
    monkeypatch.setattr(cli, "_exec_claude", lambda argv, cwd: ran.append((argv, cwd)) or 0)
    assert cli.main(["resume", "w", "--cwd", str(tmp_path)]) == 0
    assert ran[0][0][:2] == ["claude", "--resume"] and ran[0][1] == str(tmp_path)


def test_resume_of_another_authors_session_drops_permission_bypass(signed_in, tmp_path, monkeypatch, capsys):
    signed_in.listings = [{"id": "L1"}]
    signed_in.transcripts["L1"] = make_transcript("s")
    ran = []
    monkeypatch.setattr(cli, "_exec_claude", lambda argv, cwd: ran.append(argv) or 0)
    assert cli.main(["resume", "L1", "--cwd", str(tmp_path), "--", "--dangerously-skip-permissions", "-p"]) == 0
    err = capsys.readouterr().err
    assert "--dangerously-skip-permissions" not in ran[0] and "-p" in ran[0]
    assert "untrusted input" in err and "dropped: --dangerously-skip-permissions" in err
    assert cli.main(["resume", "L1", "--cwd", str(tmp_path), "--trust-base", "--",
                     "--dangerously-skip-permissions"]) == 0
    assert "--dangerously-skip-permissions" in ran[1]


def test_exec_claude_without_claude_on_path(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert cli._exec_claude(["claude", "--resume", "x"], str(tmp_path)) == 0
    assert "is not on PATH" in capsys.readouterr().err


@pytest.mark.parametrize("args,kept,dropped", [
    (["--dangerously-skip-permissions"], [], ["--dangerously-skip-permissions"]),
    (["--allow-dangerously-skip-permissions=1", "-p"], ["-p"], ["--allow-dangerously-skip-permissions=1"]),
    (["--permission-mode", "bypassPermissions"], [], ["--permission-mode", "bypassPermissions"]),
    (["--permission-mode=BYPASSPERMISSIONS"], [], ["--permission-mode=BYPASSPERMISSIONS"]),
    (["--permission-mode bypassPermissions"], [], ["--permission-mode", "bypassPermissions"]),
    (["--permission-mode", "plan"], ["--permission-mode", "plan"], []),
])
def test_strip_permission_bypass(args, kept, dropped):
    assert cli.strip_permission_bypass(args) == (kept, dropped)


def test_launch_compat(signed_in, tmp_path, monkeypatch):
    signed_in.add_session("c-1", make_transcript("base"), label="w")
    ran = []
    monkeypatch.setattr(cli, "_exec_claude", lambda argv, cwd: ran.append(argv) or 0)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["launch", "--a4sx-session", "w"]) == 0
    assert ran[0][:2] == ["claude", "--resume"]
    assert cli.main(["launch", "--", "hello"]) == 0
    assert ran[1] == ["claude", "hello"]


# --- tokens / squash / craft -----------------------------------------------------------------------


@pytest.mark.parametrize("given,expected", [("200000", 200000), ("200k", 200000), ("1m", 1000000),
                                            ("200_000", 200000), ("1.5M", 1500000)])
def test_window_accepts_the_forms_people_type(given, expected):
    assert cli.window_arg(given) == expected


def test_window_model_name_says_what_to_type():
    with pytest.raises(argparse.ArgumentTypeError) as e:
        cli.window_arg("opus4-8")
    assert "NUMBER OF TOKENS" in str(e.value) and "200k" in str(e.value)
    for bad in ("0", "-5k"):
        with pytest.raises(argparse.ArgumentTypeError):
            cli.window_arg(bad)


def test_tokens_on_a_local_file(tmp_path, capsys):
    f = tmp_path / "s.jsonl"
    f.write_bytes(make_transcript())
    assert cli.main(["tokens", str(f), "--window", "1k"]) == 0
    out = capsys.readouterr().out
    assert "current context :     1,100  (110% of 1k)" in out and "squash" in out


def test_squash_recipe(signed_in, capsys):
    signed_in.add_session("c-1", make_transcript(), label="long", tokens={"current": 150000})
    assert cli.main(["squash", "long"]) == 0
    cap = capsys.readouterr()
    assert "a4sx resume long" in cap.out and "--into c-1 --compact" in cap.out and "~150k (~75% of 200k)" in cap.err


def test_craft_mixes_files_and_saved_sessions(signed_in, tmp_path, capsys):
    signed_in.add_session("c-1", make_transcript("a"), label="saved")
    f = tmp_path / "b.jsonl"
    f.write_bytes(make_transcript("b"))
    assert cli.main(["craft", "saved", str(f), "--label", "combo"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["records"] == 4 and d["crafted_from"] == ["saved", str(f)]


# --- hints / misc ----------------------------------------------------------------------------------


class _TTY(io.StringIO):
    def isatty(self):
        return True


def test_hints_only_on_a_terminal(signed_in, tmp_path, monkeypatch, capsys):
    f = _file(tmp_path, rows=[ROWS[1]])
    monkeypatch.setattr("sys.stderr", _TTY())
    cli.main(["push", str(f), "--label", "lbl"])
    assert "next: resume it any time with `a4sx resume lbl`" in __import__("sys").stderr.getvalue()
    monkeypatch.setenv("A4SX_HINTS", "0")
    monkeypatch.setattr("sys.stderr", _TTY())
    cli.main(["push", str(f), "--label", "lbl"])
    assert "next:" not in __import__("sys").stderr.getvalue()


def test_version(env, capsys):
    assert cli.main(["version"]) == 0
    assert capsys.readouterr().out.strip().endswith("(server 9.9.9)")
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert cli.main(["version", "--offline"]) == 0


def test_completion_and_help(capsys):
    assert cli.main(["completion", "bash"]) == 0
    out = capsys.readouterr().out
    assert "complete -F _a4sx_complete a4sx" in out and "resume" in out
    assert cli.main([]) == 2


def test_python_dash_m(tmp_path):
    import subprocess
    import sys

    r = subprocess.run([sys.executable, "-m", "a4sx", "--version"], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.startswith("a4sx ")
