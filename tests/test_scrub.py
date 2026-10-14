# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 crossVault GmbH
"""The client scrub: what it changes, and that its report never holds a value.

Token-shaped strings are assembled at runtime so this file holds none."""
import json
from pathlib import Path

import pytest

from a4sx import _cli_scrub
from a4sx.scrub import PII_RULES, scrub_bytes, scrub_text

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
    assert rep.mode == "full" and rep.lines_changed == len(rep.lines)
    assert {"session_token", "bearer", "github_token"} <= set(rep.rules)
    assert rep.summary().startswith("client scrub: ") and not rep.identical


def test_personal_data_is_removed_by_default():
    out, rep = scrub_bytes(_data())
    rows = [json.loads(x) for x in out.decode().splitlines()]
    assert "alice" not in rows[1]["cwd"] and "bob@" not in json.dumps(rows[1])
    assert "192.0.2.7" not in json.dumps(rows[1])
    assert {"homedir", "email", "ip"} <= set(rep.rules)


def test_keep_pii_keeps_personal_data_but_not_secrets():
    out, rep = scrub_bytes(_data(), mode="secrets-only")
    rows = [json.loads(x) for x in out.decode().splitlines()]
    assert rows[1] == ROWS[1]
    for s in SECRETS:
        assert s not in out.decode()
    assert rep.mode == "secrets-only" and not (set(rep.rules) & PII_RULES)
    assert rep.summary().endswith("[--keep-pii: personal data, hosts, IPs and paths kept]")


def test_output_stays_valid_jsonl_with_ids():
    out, _ = scrub_bytes(_data())
    rows = [json.loads(x) for x in out.decode().splitlines()]
    assert [r["uuid"] for r in rows] == ["u1", "u2", "u3"]
    assert {r["sessionId"] for r in rows} == {"sid-1"}


def test_unchanged_lines_keep_their_exact_bytes():
    data = b'{"sessionId": "s",   "uuid": "u", "message": {"content": "hi"}}\n'
    out, rep = scrub_bytes(data)
    assert out == data and rep.identical and rep.summary() == "client scrub: 0 lines changed"


@pytest.mark.parametrize("kw", [{"enabled": False}, {"mode": "none"}])
def test_disabled_scrub_passes_bytes_through(kw):
    out, rep = scrub_bytes(_data(), **kw)
    assert out == _data() and rep.identical and rep.mode == "none"
    assert rep.summary() == "client scrub: skipped (--no-scrub)"


def test_unknown_mode_is_refused():
    with pytest.raises(ValueError):
        scrub_bytes(_data(), mode="most")


def test_report_never_contains_a_value():
    _, rep = scrub_bytes(_data())
    blob = json.dumps(rep.to_dict()) + json.dumps(rep.hits) + rep.summary()
    for s in SECRETS + ("alice", "bob@corp", "192.0.2.7"):
        assert s not in blob


def test_private_key_in_plain_text():
    pem = "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEow" + "A" * 40 + "\n-----END RSA " + "PRIVATE KEY-----"
    out, rep = scrub_bytes(f"line1\n{pem}\nline5\n".encode())
    assert b"MIIEow" not in out and "private_key" in rep.rules


def test_scrub_text():
    out, counts = scrub_text(f"Bearer {FAKE}")
    assert FAKE not in out and counts == {"bearer": 1}
    assert scrub_text("mail ann@corp.example.net", mode="secrets-only") == ("mail ann@corp.example.net", {})


# --- parity with the a4sx CLI -----------------------------------------------------------------------
# tests/vectors/scrub_parity.json holds inputs and what the a4sx CLI itself returns for them (written by
# tools/vendor_scrub.py, which refuses to write them unless the vendored scrub returns the same). Here the
# vendored scrub must still return exactly that, offline.

_VECTORS = json.loads((Path(__file__).parent / "vectors" / "scrub_parity.json").read_text(encoding="utf-8"))


def _unjoin(v):
    if isinstance(v, str):
        return v.replace(_VECTORS["join"], "")
    if isinstance(v, dict):
        return {k: _unjoin(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_unjoin(x) for x in v]
    return v


@pytest.mark.parametrize("case", _VECTORS["cases"], ids=[c["name"] for c in _VECTORS["cases"]])
@pytest.mark.parametrize("mode", _VECTORS["modes"])
def test_parity_with_the_cli(case, mode):
    case = _unjoin(case)
    text, counts, hits = _cli_scrub.scrub_detail(case["input"], mode)
    want = case[mode]
    assert text == want["output"]
    assert dict(sorted(counts.items())) == want["counts"]
    assert hits == want["hits"]


def test_vectors_come_from_the_vendored_cli_release():
    assert _VECTORS["cli_version"] == _cli_scrub.CLI_VERSION
    fired = {rule for c in _VECTORS["cases"] for rule in c["full"]["counts"]}
    assert {name for name, _pattern in _cli_scrub._SCRUB} <= fired   # every CLI rule is exercised
