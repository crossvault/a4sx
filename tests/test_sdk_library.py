# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 crossVault GmbH
"""SDK library calls against the local stub server: list, search, acquire, fetch, resume, delete."""
import json
import os

import pytest

from a4sx import sdk
from a4sx.transcript import project_dir_name

from conftest import make_transcript
from stub_server import VALID_TOKEN


@pytest.fixture
def authed(client):
    client._token = VALID_TOKEN
    return client


def test_list_and_resolve(authed, stub):
    stub.add_session("c-1", make_transcript(), label="my-work", title="Fix the flaky test")
    stub.add_session("c-2", make_transcript(), label="other", title="Refactor uploads", tags=["label:alias"])
    rows = authed.list_sessions()
    assert [r["id"] for r in rows] == ["c-1", "c-2"]
    assert authed.resolve(rows, "c-2")["id"] == "c-2"
    assert authed.resolve(rows, "my-work")["id"] == "c-1"
    assert authed.resolve(rows, "alias")["id"] == "c-2"
    assert authed.resolve(rows, "flaky")["id"] == "c-1"           # unique title substring
    assert authed.resolve(rows, "c-") is None                     # ambiguous
    assert authed.find("nothing") is None


def test_public_search_is_anonymous(authed, stub):
    stub.listings = [{"id": "L1", "tenant": "acme", "title": "Pytest flakes", "price_cents": 0},
                     {"id": "L2", "tenant": "acme", "title": "Docker builds", "price_cents": 500}]
    res = authed.search("pytest", tags=["ci"], limit=5)
    assert res["scope"] == "public" and [x["id"] for x in res["items"]] == ["L1"]
    req = stub.calls("GET", "/memory/discover")[0]
    assert req.auth is None
    assert req.query == {"q": "pytest", "tag": "ci", "sort": "relevance", "limit": "5", "offset": "0"}
    assert authed.get_listing("L2")["price_cents"] == 500
    with pytest.raises(sdk.ApiError):
        authed.get_listing("nope")


def test_library_search_filters_locally_and_pages(authed, stub):
    for i in range(5):
        stub.add_session(f"c-{i}", b"", label=f"work-{i}", title="tests" if i % 2 else "docs", tags=["x"])
    res = authed.search("tests", scope="library", tags=["X"], limit=1)
    assert res["total"] == 2 and [r["id"] for r in res["items"]] == ["c-1"] and res["next_offset"] == 1
    with pytest.raises(ValueError):
        authed.search(scope="everything")


def test_acquire_outcomes(authed, stub):
    stub.listings = [{"id": "free", "title": "Free one"}, {"id": "paid", "price_cents": 900},
                     {"id": "notes", "title": "Notes"}]
    stub.memory_only.add("notes")
    got = authed.acquire("free")
    assert got["collection"] == "acq-free" and got["acquired_from"]["cid"] == "free"
    with pytest.raises(sdk.PaymentRequired):
        authed.acquire("paid")
    with pytest.raises(sdk.MemoryOnly) as e:
        authed.acquire("notes")
    assert e.value.details["use"] == "/memory/x"
    with pytest.raises(sdk.ApiError) as e:
        authed.acquire("unknown")
    assert e.value.status == 404


def test_fetch_from_the_library(authed, stub):
    data = make_transcript("sid-a")
    stub.add_session("c-1", data, label="my-work")
    f = authed.fetch("my-work")
    assert (f.cid, f.member_id, f.transcript, f.session_id) == ("c-1", "s-c-1", data, "sid-a")
    assert f.recorded_cwd == "/work/app" and f.acquired_from is None and f.resumed == "latest"
    path = f.save()
    assert open(path, "rb").read() == data and path.endswith("sid-a.jsonl")
    if os.name == "posix":
        assert os.stat(path).st_mode & 0o777 == 0o600


def test_fetch_falls_back_to_acquiring_an_entitled_listing(authed, stub):
    stub.listings = [{"id": "L1", "title": "Shared"}]
    stub.transcripts["L1"] = make_transcript("sid-l")
    f = authed.fetch("L1")
    assert f.cid == "acq-L1" and f.acquired_now["acquired_from"]["cid"] == "L1"
    assert f.acquired_from == {"cid": "L1", "version": "v1", "tenant": None}
    with pytest.raises(sdk.NotFound):
        authed.fetch("missing")
    with pytest.raises(sdk.NotFound):
        authed.fetch("L1-other", acquire=False)


def test_fetch_compact_tip_and_boundary(authed, stub):
    full = make_transcript("s")
    tip = make_transcript("s-tip")
    stub.add_session("c-1", full, label="tipped", compact_version="v3")
    stub.versions[("c-1", "v3")] = tip
    f = authed.fetch("tipped", compact=True)
    assert f.transcript == tip and f.from_compact and f.resumed == "v3"

    compacted = make_transcript("s", extra=[
        {"type": "system", "subtype": "compact_boundary", "sessionId": "s", "uuid": "b", "parentUuid": "u2"},
        {"type": "user", "isCompactSummary": True, "sessionId": "s", "uuid": "c", "parentUuid": "b"}])
    stub.add_session("c-2", compacted, label="boundary")
    f = authed.fetch("boundary", compact=True)
    assert f.from_compact and f.resumed == "compaction boundary (2 records)"

    stub.add_session("c-3", full, label="plain")
    with pytest.raises(sdk.A4sxError, match="never compacted"):
        authed.fetch("plain", compact=True)


def test_resume_writes_a_new_local_session_and_lineage(authed, stub, tmp_path):
    stub.add_session("c-1", make_transcript("base-sid"), label="my-work", version="v7")
    work = tmp_path / "proj.x"
    work.mkdir()
    r = authed.resume("my-work", cwd=str(work))
    expected = tmp_path / "claude" / "projects" / project_dir_name(str(work)) / f"{r.local.session_id}.jsonl"
    assert r.local.path == str(expected) and r.local.base_session_id == "base-sid"
    assert {json.loads(x)["sessionId"] for x in open(expected)} == {r.local.session_id}
    assert sdk.read_lineage(r.local.session_id) == {"parent_cid": "c-1", "parent_version": "v7",
                                                   "base_internal": "base-sid", "untrusted": False}
    assert stub.transcripts["c-1"] == make_transcript("base-sid")          # the saved copy is untouched


def test_delete(authed, stub):
    stub.add_session("c-1", b"", label="gone")
    assert authed.delete("gone")["erase_within_hours"] == 24
    assert stub.calls("DELETE", "/library/session/c-1")
    with pytest.raises(sdk.NotFound):
        authed.delete("gone")


def test_server_version_needs_no_token(client, stub):
    assert client.server_version() == "9.9.9" and stub.requests[0].auth is None


def test_me(authed):
    assert authed.me()["user"]["email"] == "dev@example.com"
