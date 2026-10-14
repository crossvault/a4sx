# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 crossVault GmbH
"""SDK sign-in against the local stub server: device login, restarts, retries, token handling."""
import json
import os
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from a4sx import config, sdk

from stub_server import VALID_TOKEN


def test_device_login_stores_token_0600_and_remembers_the_server(client, stub):
    seen = []
    tok = client.login(on_pairing=lambda login, restarted: seen.append((login.user_code, restarted)))
    assert tok == VALID_TOKEN and seen == [("ABCD-0001", False)]
    assert config.load_token(stub.base_url) == VALID_TOKEN
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(config.credentials_file()).st_mode) == 0o600
    assert config.resolve_base_url() == stub.base_url
    assert [r.path for r in stub.requests] == ["/auth/cli/start", "/auth/cli/poll", "/accounts/me",
                                               "/library/ensure"]
    # start and poll carry no credential; the account check and ensure carry the new session
    assert stub.requests[0].auth is None and stub.requests[1].auth is None
    assert stub.requests[2].auth == stub.requests[3].auth == f"Bearer {VALID_TOKEN}"
    assert client.new_account_hint == ""


def test_an_account_pending_email_verification_gets_no_library(client, stub):
    stub.email_verification_pending = True
    with pytest.raises(sdk.EmailVerificationPending):
        client.login()
    assert not stub.calls("POST", "/library/ensure")
    assert config.load_token(stub.base_url) == VALID_TOKEN   # stored: the next login finishes the setup


def test_a_new_account_hint_is_kept(client, stub):
    stub.new_account_hint = "You can link another sign-in provider in Account settings."
    client.login()
    assert client.new_account_hint == stub.new_account_hint


def test_provider_is_passed(client, stub):
    login = client.start_device_login("github")
    assert json.loads(stub.requests[0].body) == {"provider": "github"}
    assert login.user_code == "ABCD-0001" and login.expires_in == 600


def test_expired_pairing_restarts_in_place(client, stub):
    stub.auto_approve = False
    stub.expires_in = 1
    t = [0.0]
    client._clock = lambda: t[0]

    def sleep(s):
        if len(stub.pairings) == 1:
            t[0] += s                              # the first pairing runs out of time
        else:
            stub.pairings["code2"]["approved"] = True   # the restarted one is approved

    client._sleep = sleep
    seen = []
    tok = client.device_login(on_pairing=lambda login, restarted: seen.append(restarted))
    assert tok == VALID_TOKEN and seen == [False, True]
    assert json.loads(stub.calls("POST", "/auth/cli/start")[1].body) == {"replaces": "code1"}


def test_a_404_poll_also_restarts(client, stub):
    stub.auto_approve = False
    stub.queue("GET", "/auth/cli/poll", 404, {"error": {"code": "not_found", "message": "gone"}})
    calls = []

    def sleep(s):
        calls.append(s)
        stub.pairings.get("code2", {})["approved"] = True

    client._sleep = sleep
    assert client.device_login() == VALID_TOKEN
    assert len(stub.calls("POST", "/auth/cli/start")) == 2


def test_gives_up_after_the_restart_budget(client, stub):
    stub.auto_approve = False
    stub.expires_in = 1
    t = [0.0]
    client._clock = lambda: t[0]
    client._sleep = lambda s: t.__setitem__(0, t[0] + s)
    with pytest.raises(sdk.LoginTimedOut):
        client.device_login(max_restarts=2)
    assert len(stub.calls("POST", "/auth/cli/start")) == 3


def test_start_rides_out_a_deploy_and_a_rate_limit(client, stub):
    stub.queue("POST", "/auth/cli/start", 503, {"error": {"code": "warming", "message": "",
                                                          "details": {"retry_after_s": 3}}})
    stub.queue("POST", "/auth/cli/start", 429, {"error": {"code": "rate_limited", "message": "",
                                                          "details": {"retry_after_s": 999}}})
    waits, told = [], []
    client._sleep = waits.append
    client.start_device_login(on_wait=lambda st, w: told.append((st, w)))
    assert waits == [3, 30] and told == [(503, 3), (429, 30)]     # capped at 30 s


def test_start_gives_up_with_an_api_error(client, stub):
    for _ in range(4):
        stub.queue("POST", "/auth/cli/start", 503, {})
    with pytest.raises(sdk.ApiError) as e:
        client.start_device_login()
    assert e.value.status == 503


def test_login_with_token_checks_then_stores(client, stub):
    me = client.login_with_token(VALID_TOKEN)
    assert me["user"]["id"] == "u-1" and config.load_token(stub.base_url) == VALID_TOKEN
    with pytest.raises(sdk.ApiError) as e:
        client.login_with_token("a4sx-test-" + "notvalid999")
    assert e.value.status == 401 and e.value.session_ended
    assert config.load_token(stub.base_url) == VALID_TOKEN       # unchanged


def test_logout_revokes_on_the_server_and_removes_the_file(client, stub):
    client.login_with_token(VALID_TOKEN)
    assert client.logout() is True
    assert stub.calls("POST", "/auth/logout")[0].auth == f"Bearer {VALID_TOKEN}"
    assert VALID_TOKEN not in stub.tokens and config.load_token(stub.base_url) is None
    assert client.logout() is False


def test_not_logged_in(client):
    with pytest.raises(sdk.NotLoggedIn):
        client.list_sessions()


def test_env_token_is_used_only_for_its_server(client, stub, monkeypatch):
    monkeypatch.setenv("A4SX_SESSION", VALID_TOKEN)
    with pytest.raises(sdk.NotLoggedIn):                         # $A4SX_SESSION is for the public server
        client.list_sessions()
    assert stub.requests == []
    monkeypatch.setenv("A4SX_SESSION_BASE", stub.base_url)
    assert client.list_sessions() == []


def test_token_never_appears_in_repr_or_errors(stub):
    c = sdk.Client(stub.base_url, token="a4sx-test-" + "secretvalue42")
    assert "secretvalue42" not in repr(c)
    with pytest.raises(sdk.ApiError) as e:
        c.list_sessions()
    assert "secretvalue42" not in str(e.value) and "secretvalue42" not in repr(e.value.body)


def test_error_envelope_and_flat_errors():
    e = sdk.ApiError(404, {"error": {"code": "not_found", "message": "no such thing"}}, "fetch")
    assert str(e) == "fetch failed: HTTP 404 not_found: no such thing" and not e.session_ended
    e = sdk.ApiError(403, {"ok": False, "error": "read_only_tenant", "reason": "demo"}, "push")
    assert e.code == "read_only_tenant" and "read_only_tenant: demo" in str(e)
    assert sdk.ApiError(401, {}, "list").session_ended


def test_network_error_is_clean():
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                                    # nothing listens there now
    c = sdk.Client(f"http://127.0.0.1:{port}", token="t-123456")
    with pytest.raises(sdk.NetworkError):
        c.list_sessions()
    assert c.server_version() is None


def test_bearer_is_not_forwarded_on_a_redirect(stub):
    seen = []

    class Other(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen.append(self.headers.get("authorization"))
            body = b'{"sessions": []}'
            self.send_response(200)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    other = ThreadingHTTPServer(("127.0.0.1", 0), Other)
    threading.Thread(target=other.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        stub.queue("GET", "/library/sessions", 302, {},
                   {"Location": f"http://localhost:{other.server_address[1]}/library/sessions"})
        sdk.Client(stub.base_url, token=VALID_TOKEN).list_sessions()
        assert seen == [None]
        assert stub.requests[-1].auth == f"Bearer {VALID_TOKEN}"
    finally:
        other.shutdown()
        other.server_close()


def test_user_agent_names_the_client(client, stub):
    client.server_version()
    assert stub.requests[0].headers["user-agent"].startswith("a4sx/")


def test_the_offline_guard_blocks_real_hosts():
    with pytest.raises(AssertionError, match="non-loopback"):
        sdk.Client().server_version()           # the default public server is never contacted by tests
