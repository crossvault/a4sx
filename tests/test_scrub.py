# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 crossVault GmbH
"""The client pre-scrub: what it changes, and that its report never holds a value.

Token-shaped strings are assembled at runtime so this file holds none."""
import json

from a4sx.scrub import scrub_bytes, scrub_text

FAKE = "not-a-real-token-0000"
GH = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3"
SESS = "ge_sess_" + "Zx9Yw8Vu7Ts6"
SECRETS = (FAKE, GH, GH[4:], SESS)

ROWS = [
    {"sessionId": "sid-1", "uuid": "u1", "type": "user",
     "message": {"content": f'curl -H "Authorization: Bearer {FAKE}" x'}},
    {"sessionId": "sid-1", "uuid": "u2", "type": "assistant", "cwd": "/home/alice/app",
     "message": {"content": "no secrets here; mail bob@corp.example.net from 192.0.2.7"}},
    {"sessionId": "sid-1", "uuid": "u3", "type": "user", "message": {"content": f"use {GH} and {SESS}"}},
]


def _data(rows=ROWS) -> bytes:
    return "".join(json.dumps(r) + "\n" for r in rows).encode()


def test_secrets_are_removed_and_lines_reported():
    out, rep = scrub_bytes(_data())
    text = out.decode()
    for s in SECRETS:
        assert s not in text
    assert rep.lines == [1, 3] and rep.lines_changed == 2
    assert rep.rules == {"secret.a4sx_session": 1, "secret.bearer": 1, "secret.github_token": 1}
    assert rep.summary() == ("client scrub: 2 lines changed (secret.a4sx_session: 1, secret.bearer: 1, "
                             "secret.github_token: 1) — lines 1, 3")
    assert not rep.identical


def test_output_stays_resumable_jsonl_with_ids_and_paths():
    out, _ = scrub_bytes(_data())
    rows = [json.loads(x) for x in out.decode().splitlines()]
    assert [r["uuid"] for r in rows] == ["u1", "u2", "u3"]
    assert {r["sessionId"] for r in rows} == {"sid-1"}
    # personal-data rules are off for the pre-scrub: paths, e-mails and IPs are left as they are
    assert rows[1] == ROWS[1]


def test_unchanged_lines_keep_their_exact_bytes():
    data = b'{"sessionId": "s",   "uuid": "u", "message": {"content": "hi"}}\n'
    out, rep = scrub_bytes(data)
    assert out == data and rep.identical and rep.summary() == "client scrub: 0 lines changed"


def test_disabled_scrub_passes_bytes_through():
    out, rep = scrub_bytes(_data(), enabled=False)
    assert out == _data() and rep.identical and rep.summary() == "client scrub: skipped (--no-scrub)"


def test_report_never_contains_a_value():
    _, rep = scrub_bytes(_data())
    blob = json.dumps(rep.to_dict())
    for s in SECRETS:
        assert s not in blob


def test_escaped_json_cannot_hide_a_secret():
    # the secret is JSON-escaped inside the line; the engine decodes before matching
    line = json.dumps({"sessionId": "s", "message": {"content": "use " + GH}})
    line = line.replace(GH, GH[:6] + "\\u%04x" % ord(GH[6]) + GH[7:])
    assert GH not in line
    out, rep = scrub_bytes((line + "\n").encode())
    assert rep.lines == [1] and rep.rules == {"secret.github_token": 1}
    assert GH not in json.loads(out)["message"]["content"]


def test_private_key_in_plain_text_spans_lines():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow" + "A" * 40 + "\n-----END RSA PRIVATE KEY-----"
    out, rep = scrub_bytes(f"line1\n{pem}\nline5\n".encode())
    assert b"MIIEow" not in out and rep.rules == {"secret.private_key": 1} and rep.lines == [2]


def test_scrub_text():
    out, counts = scrub_text(f"Bearer {FAKE}")
    assert FAKE not in out and counts == {"secret.bearer": 1}
