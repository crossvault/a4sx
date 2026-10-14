# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 crossVault GmbH
"""Every test runs offline and in a throwaway home.

- HOME, the a4sx config dir and the Claude Code dir point into tmp_path;
- every A4SX_* variable from the outer environment is removed;
- any DNS lookup or connection to a non-loopback address fails the test.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
from typing import List

import pytest

from stub_server import Stub

_real_getaddrinfo = socket.getaddrinfo
_real_connect = socket.socket.connect


def _loopback(host) -> bool:
    h = str(host or "").strip("[]").lower()
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h.split("%")[0]).is_loopback
    except ValueError:
        return False


def _guard_getaddrinfo(host, *a, **k):
    if not _loopback(host):
        raise AssertionError(f"test tried to resolve a non-loopback host: {host!r}")
    return _real_getaddrinfo(host, *a, **k)


def _guard_connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6) and not _loopback(address[0]):
        raise AssertionError(f"test tried to connect to a non-loopback address: {address!r}")
    return _real_connect(self, address)


@pytest.fixture(autouse=True)
def _offline_and_isolated(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith("A4SX_") or k in ("CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "SSH_CONNECTION", "SSH_TTY",
                                          "SSH_CLIENT"):
            monkeypatch.delenv(k, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("A4SX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setattr(socket, "getaddrinfo", _guard_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", _guard_connect)
    # never launch a real external binary (claude, qrencode, a browser) from a test
    for name in ("execv", "execvp", "execvpe", "execl", "execlp"):
        monkeypatch.setattr(os, name, _no_exec, raising=False)
    import webbrowser

    from a4sx import cli

    monkeypatch.setattr(cli, "_qr_lines", lambda text: [])
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: pytest.fail("a test tried to open a real browser"))
    yield


def _no_exec(*a, **k):
    raise AssertionError(f"test tried to exec an external program: {a[:2]!r}")


@pytest.fixture
def attacker():
    """A second server, standing in for a hostile --base / $A4SX_BASE."""
    s = Stub().start()
    yield s
    s.stop()


@pytest.fixture
def stub():
    s = Stub().start()
    yield s
    s.stop()


@pytest.fixture
def client(stub):
    from a4sx.sdk import Client

    return Client(stub.base_url, sleep=lambda s: None)


def make_transcript(session_id: str = "11111111-2222-3333-4444-555555555555", extra: List[dict] = ()) -> bytes:
    """A small synthetic Claude Code transcript."""
    rows = [
        {"type": "user", "sessionId": session_id, "uuid": "u1", "parentUuid": None, "cwd": "/work/app",
         "timestamp": "2026-10-01T10:00:00Z", "message": {"role": "user", "content": "fix the flaky test"}},
        {"type": "assistant", "sessionId": session_id, "uuid": "u2", "parentUuid": "u1",
         "timestamp": "2026-10-01T10:00:05Z",
         "message": {"role": "assistant", "model": "claude-x", "content": [{"type": "text", "text": "on it"}],
                     "usage": {"input_tokens": 10, "cache_read_input_tokens": 1000,
                               "cache_creation_input_tokens": 90, "output_tokens": 50}}},
        *extra,
    ]
    return "".join(json.dumps(r) + "\n" for r in rows).encode()
