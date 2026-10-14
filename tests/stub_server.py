# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 crossVault GmbH
"""A small in-memory stand-in for the a4sx HTTP API, served on 127.0.0.1 for the tests.

It implements just enough of each route the SDK calls, records every request, and lets a test
queue canned answers (``queue``) to simulate deploys, rate limits and failures.
"""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

VALID_TOKEN = "a4sx-test-" + "stubvalid0001"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class Recorded:
    def __init__(self, method: str, path: str, query: Dict[str, str], headers: Dict[str, str], body: bytes):
        self.method, self.path, self.query, self.headers, self.body = method, path, query, headers, body

    @property
    def auth(self) -> Optional[str]:
        return self.headers.get("authorization")

    def __repr__(self) -> str:
        return f"<{self.method} {self.path} {self.query}>"


class Stub:
    def __init__(self) -> None:
        self.requests: List[Recorded] = []
        self.tokens = {VALID_TOKEN}
        self.rows: List[dict] = []
        self.transcripts: Dict[str, bytes] = {}
        self.versions: Dict[Tuple[str, str], bytes] = {}
        self.listings: List[dict] = []
        self.entitled: set = set()
        self.memory_only: set = set()
        self.pairings: Dict[str, dict] = {}
        self.auto_approve = True
        self.expires_in = 600
        self.issue_token = VALID_TOKEN
        self.idempotency: Dict[str, Tuple[int, dict]] = {}
        self.lose_next_push_answer = False   # store the push, then answer 504 (a lost answer)
        self.email_verification_pending = False   # /accounts/me says the account must confirm an e-mail
        self.new_account_hint: Optional[str] = None   # the poll says this sign-in created a new account
        self.native: Dict[str, str] = {}   # cid -> native/<harness>.<ext>: stored in the other-harness layout
        self.push_harness = "claude-code"   # what the server "recognises" an upload as
        self._queued: List[Tuple[str, str, int, Any, dict]] = []
        self._n = 0
        self._srv: Optional[ThreadingHTTPServer] = None

    # -- test helpers -------------------------------------------------------------------------------

    @property
    def base_url(self) -> str:
        assert self._srv is not None
        return f"http://127.0.0.1:{self._srv.server_address[1]}"

    def queue(self, method: str, path_prefix: str, status: int, body: Any = None, headers: Optional[dict] = None):
        """The next matching request gets this answer instead of the normal route."""
        self._queued.append((method, path_prefix, status, body, headers or {}))

    def add_session(self, cid: str, transcript: bytes, **row: Any) -> dict:
        r = {"id": cid, "version": row.pop("version", "v1"), **row}
        self.rows.append(r)
        self.transcripts[cid] = transcript
        return r

    def calls(self, method: str, path: str) -> List[Recorded]:
        return [r for r in self.requests if r.method == method and r.path == path]

    # -- server -------------------------------------------------------------------------------------

    def start(self) -> "Stub":
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _do(self):
                u = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                n = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(n) if n else b""
                rec = Recorded(self.command, u.path, q, {k.lower(): v for k, v in self.headers.items()}, body)
                stub.requests.append(rec)
                status, payload, hdrs = stub.handle(rec)
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                for k, v in hdrs.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_DELETE = do_PUT = _do

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self._srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()

    # -- routes -------------------------------------------------------------------------------------

    def _authed(self, rec: Recorded) -> bool:
        a = rec.auth or ""
        return a.startswith("Bearer ") and a[7:] in self.tokens

    @staticmethod
    def _err(status: int, code: str, message: str, **details: Any):
        return status, {"error": {"code": code, "message": message, "details": details}}, {}

    def handle(self, rec: Recorded):
        for i, (m, p, st, body, hdrs) in enumerate(self._queued):
            if m == rec.method and rec.path.startswith(p):
                del self._queued[i]
                return st, body if body is not None else {}, hdrs
        path, m = rec.path, rec.method
        if path == "/healthz":
            return 200, {"ok": True, "version": "9.9.9"}, {}
        if path == "/auth/cli/start" and m == "POST":
            self._n += 1
            code = f"code{self._n}"
            req = json.loads(rec.body or b"{}")
            self.pairings[code] = {"approved": self.auto_approve, "req": req}
            if req.get("replaces") in self.pairings:
                self.pairings[req["replaces"]]["forwarded"] = code
            return 200, {"code": code, "login_url": f"{self.base_url}/auth/cli/choose?code={code}",
                         "short_url": f"{self.base_url}/auth/cli/d/ABCD-{self._n:04d}",
                         "verification_url": f"{self.base_url}/auth/cli/device",
                         "user_code": f"ABCD-{self._n:04d}", "expires_in": self.expires_in}, {}
        if path == "/auth/cli/poll":
            p = self.pairings.get(rec.query.get("code", ""))
            if p is None or p.get("expired"):
                return self._err(404, "not_found", "unknown or expired pairing")
            if p["approved"]:
                ok = {"status": "ok", "session": self.issue_token}
                if self.new_account_hint is not None:
                    ok.update(new_account=True, hint=self.new_account_hint)
                return 200, ok, {}
            return 202, {"status": "pending"}, {}
        if path == "/memory/discover":
            q = rec.query.get("q", "").lower()
            items = [x for x in self.listings if q in json.dumps(x).lower()]
            return 200, {"query": q, "items": items, "total": len(items), "next_offset": None}, {}
        if path == "/memory/discover/item":
            it = next((x for x in self.listings if x["id"] == rec.query.get("id")), None)
            if it is None:
                return self._err(404, "collection_not_found", "unknown listing")
            return 200, {"item": it}, {}
        # everything below needs a session
        if not self._authed(rec):
            if path == "/auth/logout":
                return 200, {"ok": True}, {}
            return self._err(401, "auth_invalid", "Session unknown, revoked, or expired")
        tok = (rec.auth or "")[7:]
        if path == "/auth/logout":
            self.tokens.discard(tok)
            return 200, {"ok": True}, {}
        if path == "/accounts/me":
            me = {"user": {"id": "u-1", "email": "dev@example.com"}}
            if self.email_verification_pending:
                me["email_verification_pending"] = True
            return 200, me, {}
        if path.startswith("/memory/collections/") and m == "GET":
            cid = unquote(path[len("/memory/collections/"):])
            row = next((r for r in self.rows + self.listings if r["id"] == cid), None)
            if row is None:
                return self._err(404, "collection_not_found", "unknown collection")
            return 200, {"id": cid, "harness": row.get("harness")}, {}
        if path == "/library/ensure":
            return 200, {"ok": True}, {}
        if path == "/library/sessions":
            return 200, {"sessions": self.rows}, {}
        if path == "/library/fetch":
            cid = rec.query.get("cid", "")
            if cid not in self.transcripts:
                return self._err(404, "not_found", "no such session")
            if cid in self.native:   # another harness: native file + canonical.json, no transcript.jsonl
                files = [{"path": "canonical.json", "content_b64": b64(b"{}")},
                         {"path": self.native[cid], "content_b64": b64(self.transcripts[cid])}]
            else:
                files = [{"path": "SESSION.md", "content_b64": b64(b"# x")},
                         {"path": "transcript.jsonl", "content_b64": b64(self.transcripts[cid])}]
            return 200, {"id": cid, "members": [
                {"kind": "memory", "id": "m1", "files": []},
                {"kind": "session", "id": "s-" + cid, "files": files},
            ]}, {}
        if path.startswith("/library/session/") and "/version/" in path and m == "GET":
            cid, ver = path[len("/library/session/"):].split("/version/")
            data = self.versions.get((unquote(cid), unquote(ver)))
            if data is None:
                return self._err(404, "not_found", "no such version")
            return 200, {"transcript_b64": b64(data)}, {}
        if path.startswith("/library/session/") and m == "DELETE":
            cid = unquote(path[len("/library/session/"):])
            self.rows = [r for r in self.rows if r["id"] != cid]
            return 200, {"status": "deleted", "erase_within_hours": 24}, {}
        if path == "/library/push" and m == "POST":
            key = rec.headers.get("idempotency-key", "")
            if key in self.idempotency:
                st, body = self.idempotency[key]
                return st, body, {}
            q = rec.query
            cid = q.get("into") or (q.get("label") or "session") + f"-{len(self.rows) + 1}"
            if not q.get("into"):
                row = {"id": cid, "label": q.get("label"), "title": q.get("title"), "version": "v1"}
                if q.get("parent"):
                    row["parent"] = {"cid": q["parent"].split("@")[0]}
                self.rows.append(row)
            self.transcripts[cid] = rec.body
            body = {"collection": cid, "version": "v1", "redactions": 0,
                    "harness": {"id": self.push_harness, "version": None}}
            if key:
                self.idempotency[key] = (201, body)
            if self.lose_next_push_answer:
                self.lose_next_push_answer = False
                return 504, {}, {}
            return 201, body, {}
        if path.startswith("/library/acquire/") and m == "POST":
            lid = unquote(path[len("/library/acquire/"):])
            it = next((x for x in self.listings if x["id"] == lid), None)
            if it is None:
                return self._err(404, "collection_not_found", "unknown listing")
            if lid in self.memory_only:
                return self._err(409, "memory_only", "no session", use="/memory/x", **{"import": "/memory/y"})
            if (it.get("price_cents") or 0) > 0 and lid not in self.entitled:
                return self._err(402, "payment_required", "buy it first")
            cid = f"acq-{lid}"
            af = {"cid": lid, "version": "v1", "tenant": it.get("tenant")}
            if not any(r["id"] == cid for r in self.rows):
                self.add_session(cid, self.transcripts.get(lid, b""), title=it.get("title"), acquired_from=af)
            return 201, {"collection": cid, "session": "s1", "acquired_from": af}, {}
        return self._err(404, "not_found", f"no route {m} {path}")
