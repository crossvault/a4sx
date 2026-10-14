# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 crossVault GmbH
"""SDK push against the local stub server: pre-scrub, idempotent retries, lineage, timeouts."""
import hashlib
import json

import pytest

from a4sx import sdk

from conftest import make_transcript
from stub_server import VALID_TOKEN

GH = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3"


@pytest.fixture
def authed(client):
    client._token = VALID_TOKEN
    return client


def _secret_transcript() -> bytes:
    return make_transcript("sid-p", extra=[{"type": "user", "sessionId": "sid-p", "uuid": "u3",
                                            "message": {"content": f"token {GH}"}}])


def test_push_scrubs_before_upload(authed, stub):
    res = authed.push(_secret_transcript(), label="my-work", title="T")
    sent = stub.calls("POST", "/library/push")[0]
    assert GH not in sent.body.decode() and GH[4:] not in sent.body.decode()
    assert sent.query == {"label": "my-work", "title": "T", "client_scrub": "full"}
    assert sent.headers["content-type"] == "application/octet-stream"
    assert sent.headers["x-content-sha256"] == hashlib.sha256(sent.body).hexdigest() == res.scrub.uploaded_sha256
    assert res.scrub.lines == [3] and res.scrub.rules == {"github_token": 1}
    assert res.response["collection"] == "my-work-1"


def test_push_without_scrub_sends_the_bytes(authed, stub):
    data = _secret_transcript()
    res = authed.push(data, scrub=False)
    sent = stub.calls("POST", "/library/push")[0]
    assert sent.body == data and res.scrub.identical and sent.query["client_scrub"] == "none"


def test_retries_reuse_one_idempotency_key(authed, stub):
    stub.queue("POST", "/library/push", 503, {})
    stub.queue("POST", "/library/push", 429, {})
    told = []
    waits = []
    authed._sleep = waits.append
    authed._rand = lambda lo, hi: 0
    res = authed.push(make_transcript(), label="x", on_retry=lambda st, w: told.append((st, w)))
    calls = stub.calls("POST", "/library/push")
    assert len(calls) == 3 and len({c.headers["idempotency-key"] for c in calls}) == 1
    assert told == [(503, 5), (429, 15)] and waits == [5, 15]
    assert res.response["collection"] == "x-1"


def _busy(status, code, after=None):
    return {"error": {"code": code, "message": "busy", "details": {} if after is None else {"retry_after_s": after}}}


def test_a_busy_scrub_pool_is_waited_out_with_the_servers_hint(authed, stub):
    stub.queue("POST", "/library/push", 429, _busy(429, "scrub_busy", 7))
    stub.queue("POST", "/library/push", 503, _busy(503, "scrub_pool_busy", 600))   # capped at 60 s
    for _ in range(3):
        stub.queue("POST", "/library/push", 503, _busy(503, "scrub_pool_busy"))
    waits = []
    authed._sleep = waits.append
    authed._rand = lambda lo, hi: hi   # the most jitter: +25 %
    res = authed.push(make_transcript(), label="busy")
    calls = stub.calls("POST", "/library/push")
    assert len(calls) == 6 and len({c.headers["idempotency-key"] for c in calls}) == 1
    assert waits == [7 * 1.25, 60 * 1.25, 15 * 1.25, 15 * 1.25, 15 * 1.25]
    assert res.response["collection"] == "busy-1"


def test_a_busy_scrub_pool_gives_up_after_six_attempts(authed, stub):
    for _ in range(6):
        stub.queue("POST", "/library/push", 429, _busy(429, "scrub_busy", 1))
    with pytest.raises(sdk.ApiError) as e:
        authed.push(make_transcript())
    assert e.value.code == "scrub_busy" and len(stub.calls("POST", "/library/push")) == 6


def test_a_scrub_timeout_is_never_retried(authed, stub):
    stub.queue("POST", "/library/push", 413, _busy(413, "scrub_timeout"))
    with pytest.raises(sdk.ApiError):
        authed.push(make_transcript())
    assert len(stub.calls("POST", "/library/push")) == 1


def test_a_retry_after_a_stored_push_gets_the_original_answer(authed, stub):
    # the server stored the first attempt but its answer was lost (the client sees 504)
    stub.lose_next_push_answer = True
    res = authed.push(make_transcript(), label="once")
    calls = stub.calls("POST", "/library/push")
    assert len(calls) == 2 and calls[0].headers["idempotency-key"] == calls[1].headers["idempotency-key"]
    assert res.response["collection"] == "once-1"
    assert len([r for r in stub.rows if r.get("label") == "once"]) == 1      # no duplicate


def test_non_retryable_errors_raise_at_once(authed, stub):
    stub.queue("POST", "/library/push", 409, {"error": {"code": "conflict", "message": "nope"}})
    with pytest.raises(sdk.ApiError) as e:
        authed.push(make_transcript())
    assert e.value.status == 409 and len(stub.calls("POST", "/library/push")) == 1


def test_gives_up_after_three_attempts(authed, stub):
    for _ in range(3):
        stub.queue("POST", "/library/push", 502, {})
    with pytest.raises(sdk.ApiError) as e:
        authed.push(make_transcript())
    assert e.value.status == 502 and len(stub.calls("POST", "/library/push")) == 3


def test_a_resumed_session_pushed_fresh_records_its_parent(authed, stub, tmp_path):
    stub.add_session("c-base", make_transcript("base"), label="base", version="v2")
    r = authed.resume("base", cwd=str(tmp_path))
    pushed = authed.push(open(r.local.path, "rb").read(), label="spin-off")
    assert pushed.parent == "c-base@v2"
    assert stub.calls("POST", "/library/push")[0].query["parent"] == "c-base@v2"
    # --into versions an entry the server already tracks: no lineage edge
    authed.push(open(r.local.path, "rb").read(), into="c-base")
    assert "parent" not in stub.calls("POST", "/library/push")[1].query


def test_explicit_parent_and_compact(authed, stub):
    stub.add_session("c-base", b"", label="base")
    authed.push(make_transcript(), into="c-x", compact=True, parent="base")
    q = stub.calls("POST", "/library/push")[0].query
    assert q == {"into": "c-x", "compact": "1", "parent": "c-base", "client_scrub": "full"}
    authed.push(make_transcript(), compact=True)                    # compact needs --into
    assert "compact" not in stub.calls("POST", "/library/push")[1].query


def test_push_file(authed, stub, tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_bytes(make_transcript())
    assert authed.push_file(str(p)).response["version"] == "v1"


def test_craft_pushes_one_rethreaded_unscrubbed_base(authed, stub):
    res, sid, rep = authed.craft([make_transcript("a"), make_transcript("b")], label="combo")
    sent = stub.calls("POST", "/library/push")[0]
    rows = [json.loads(x) for x in sent.body.decode().splitlines()]
    assert {r["sessionId"] for r in rows} == {sid} and rep["records"] == 4
    assert sent.query == {"label": "combo", "craft_sources": "2", "client_scrub": "full"}


@pytest.mark.parametrize("n,want", [(0, 120), (512 * 1024, 121), (145 * 1024 * 1024, 410), (10 ** 12, 1800)])
def test_upload_timeout_scales(n, want):
    assert sdk.upload_timeout(n) == want


def test_upload_timeout_override(monkeypatch):
    monkeypatch.setenv("A4SX_UPLOAD_TIMEOUT_S", "900")
    assert sdk.upload_timeout(0) == 900
    monkeypatch.setenv("A4SX_UPLOAD_TIMEOUT_S", "99999999")
    assert sdk.upload_timeout(0) == sdk.UPLOAD_TIMEOUT_OVERRIDE_MAX_S
    for bad in ("abc", "0", "-5", "1.5"):
        monkeypatch.setenv("A4SX_UPLOAD_TIMEOUT_S", bad)
        assert sdk.upload_timeout(0) == 120
