# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 crossVault GmbH
"""The a4sx session SDK: sign in, find, fetch, acquire, push and resume coding-agent sessions.

Claude Code, Codex CLI and Grok Build sessions can be saved, listed, searched and fetched; only Claude
Code sessions can be resumed today.

Everything the ``a4sx`` command does goes through this module, so other tools (an MCP server, an
editor plugin, a script) can do the same. Stdlib only.

Quick tour::

    from a4sx.sdk import Client

    client = Client()                      # $A4SX_BASE / stored server / https://session-exchange.com
    client.login(on_pairing=lambda login, restarted: print(login.login_url, login.user_code))

    result = client.push_file("~/.claude/projects/-home-me-app/1234.jsonl", label="my-work")
    print(result.response["collection"], result.scrub.summary())

    for row in client.list_sessions():
        print(row["id"], row.get("label"), row.get("title"))

    hits = client.search("pytest flaky", scope="public")
    resumed = client.resume("my-work", cwd="/home/me/app")
    print("now run:", " ".join(resumed.local.resume_command))

Errors are exceptions (:class:`A4sxError` and subclasses); nothing here calls ``sys.exit`` or
prints. Tokens are never included in an exception message, a log line or a repr.
"""

from __future__ import annotations

import base64
import json
import os
import platform
import random
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union
from urllib.parse import quote, urlencode

from . import __version__
from . import config as _config
from .config import (
    DEFAULT_BASE_URL,
    clear_token,
    config_dir,
    load_token,
    normalize_base_url,
    resolve_base_url,
    save_token,
)
from .scrub import ScrubReport, scrub_bytes
from .transcript import (
    LocalSession,
    claude_projects_dir,
    has_compaction,
    latest_local_session,
    project_dir_name,
    recorded_cwd_of,
    rekey,
    resume_locally,
    session_id_of,
    stitch,
    summarize,
    token_stats,
    trim_to_last_compact,
)

__all__ = [
    "A4sxError",
    "ApiError",
    "Client",
    "DEFAULT_BASE_URL",
    "DeviceLogin",
    "EmailVerificationPending",
    "FetchedSession",
    "LocalSession",
    "LoginExpired",
    "LoginTimedOut",
    "MemoryOnly",
    "NetworkError",
    "NotFound",
    "NotLoggedIn",
    "OtherHarness",
    "PaymentRequired",
    "PushResult",
    "ResumedSession",
    "ScrubReport",
    "claude_projects_dir",
    "clear_token",
    "config_dir",
    "harness_id",
    "harness_name",
    "has_compaction",
    "latest_local_session",
    "load_token",
    "mark_untrusted",
    "marked_untrusted",
    "normalize_base_url",
    "project_dir_name",
    "push_path",
    "read_lineage",
    "record_lineage",
    "rekey",
    "row_is_untrusted",
    "resolve_base_url",
    "resume_locally",
    "save_token",
    "scrub_bytes",
    "session_id_of",
    "stitch",
    "summarize",
    "token_stats",
    "trim_to_last_compact",
    "upload_timeout",
]


# --- errors ----------------------------------------------------------------------------------------


class A4sxError(Exception):
    """Base class for every error raised by the SDK."""


class NotLoggedIn(A4sxError):
    def __init__(self, message: str = "not signed in: run `a4sx login`, or set A4SX_SESSION") -> None:
        super().__init__(message)


class NetworkError(A4sxError):
    """The server could not be reached (offline, DNS, refused, timed out)."""


class ApiError(A4sxError):
    """The server answered with an error status.

    ``code`` and ``message`` come from the error envelope ``{"error": {"code", "message",
    "details"}}`` (or a flat ``{"error": "<code>", "reason": "..."}``)."""

    def __init__(self, status: int, body: Any = None, what: str = "request") -> None:
        self.status = status
        self.body = body
        self.what = what
        self.code, self.message, self.details = _error_fields(body)
        detail = f"{self.code}: {self.message}" if self.code and self.message else (self.code or self.message)
        if not detail:
            detail = json.dumps(body)[:300] if body not in (None, b"", {}) else "no details"
        super().__init__(f"{what} failed: HTTP {status} {detail}")

    @property
    def session_ended(self) -> bool:
        """True for a 401 that means the session expired, was revoked, or was never sent."""
        return self.status == 401 and (self.code in ("auth_invalid", "auth_missing", "not_logged_in") or not self.code)


class PaymentRequired(ApiError):
    """Acquire of a paid listing you don't own yet (HTTP 402). Buy it in the web app first."""


class MemoryOnly(ApiError):
    """Acquire of a listing that holds reading material only: there is no session to resume."""


class NotFound(A4sxError):
    """No saved session (or entitled listing) matches the name."""


class LoginExpired(A4sxError):
    """The device-login pairing expired before it was approved."""


class LoginTimedOut(A4sxError):
    """Nobody approved the device login within the restart budget."""


class EmailVerificationPending(A4sxError):
    """Signed in, but the account must confirm an e-mail address in the browser first. Until it does,
    the server refuses everything else, and no library is provisioned for it."""


class OtherHarness(A4sxError):
    """The session was recorded with a harness other than Claude Code, and only Claude Code sessions can
    be resumed today. It stays viewable (and fetchable) in your library."""

    def __init__(self, name: str, harness: str) -> None:
        self.name = name
        self.harness = harness
        shown = harness_name(harness)
        super().__init__(f"{name!r} was recorded with {shown}, and only Claude Code sessions can be resumed "
                         f"today; a4sx never turns a {shown} session into a Claude Code one. It stays "
                         "viewable in your library.")


#: Harnesses a saved session can be recorded with (the server's ``harness`` ids) and their names.
HARNESS_NAMES = {
    "claude-code": "Claude Code",
    "codex-cli": "Codex CLI",
    "gemini-cli": "Gemini CLI",
    "opencode": "OpenCode",
    "grok-build": "Grok Build",
}


def harness_id(value: Any) -> str:
    """The harness id of a server ``harness`` value (an id, or ``{id, version}``); ``unknown`` if none."""
    hid = value.get("id") if isinstance(value, dict) else value
    return hid.strip().lower() if isinstance(hid, str) and hid.strip() else "unknown"


def harness_name(value: Any) -> str:
    """A display name for a harness id or server ``harness`` value."""
    hid = harness_id(value)
    return HARNESS_NAMES.get(hid, hid)


def _harness_of(value: Any, paths: Iterable[Any] = ()) -> str:
    """The harness id from a ``harness`` value, else from a stored ``native/<harness>.<ext>`` member file
    (a session stored in that layout was recorded with that harness even if no label was sent)."""
    hid = harness_id(value)
    if hid == "unknown":
        for p in paths or ():
            if isinstance(p, str) and p.startswith("native/") and not p.startswith("native/claude-code."):
                return p[len("native/"):].rsplit(".", 1)[0].lower() or "unknown"
    return hid


def _resumable(hid: str) -> bool:
    # `unknown` (never identified) keeps the old behaviour: the transcript checks decide.
    return hid in ("claude-code", "unknown")


#: A Grok Build session folder: only this file is uploaded; the folder's other files are not.
GROK_SESSION_LOG = "updates.jsonl"
GROK_SESSION_SIDECARS = frozenset({
    "chat_history.jsonl", "system_prompt.txt", "prompt_context.json", "tool_definitions.json", "summary.json",
    "plan.json", "rewind_points.jsonl", "signals.json", "feedback.jsonl"})


def push_path(path: str) -> str:
    """The file to upload for ``path``. A Grok Build session folder resolves to its ``updates.jsonl``;
    another file of such a folder is refused before it is opened. Anything else is returned unchanged
    (``~`` expanded). Reads directory entries only, never a file's content."""
    p = os.path.expanduser(path)
    if os.path.isdir(p):
        log = os.path.join(p, GROK_SESSION_LOG)
        if os.path.islink(log):  # refused, not followed: it could point at a sidecar or elsewhere
            raise A4sxError(f"{log} is a symlink; a Grok Build session folder is pushed only when its "
                            f"{GROK_SESSION_LOG} is a regular file")
        if os.path.lexists(log) and not os.path.isfile(log):
            raise A4sxError(f"{log} is not a file; a Grok Build session folder is pushed only when its "
                            f"{GROK_SESSION_LOG} is a regular file")
        if os.path.isfile(log):
            return log
        raise A4sxError(f"{path} is a folder; give a session file. A Grok Build session folder holds "
                        f"{GROK_SESSION_LOG}, and this one has none")
    folder, name = os.path.split(p)
    if name in GROK_SESSION_SIDECARS and os.path.isfile(os.path.join(folder or ".", GROK_SESSION_LOG)):
        raise A4sxError(f"{name} is not uploaded from a Grok Build session; only {GROK_SESSION_LOG} is. Push "
                        f"{os.path.join(folder, GROK_SESSION_LOG)} (or the session folder) instead")
    return p


def _error_fields(body: Any) -> Tuple[str, str, dict]:
    if not isinstance(body, dict):
        return "", "", {}
    err = body.get("error")
    if isinstance(err, dict):
        det = err.get("details")
        return str(err.get("code") or ""), str(err.get("message") or ""), det if isinstance(det, dict) else {}
    if isinstance(err, str):
        return err, str(body.get("reason") or ""), {}
    return "", "", {}


# --- results ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviceLogin:
    """One device-login pairing (``POST /auth/cli/start``).

    Show ``login_url`` (or ``short_url`` on another device) and ``user_code``; the person checks that
    the code on the approval page matches, then approves.
    """

    code: str = field(repr=False)
    login_url: str
    user_code: str = ""
    short_url: str = ""
    verification_url: str = ""
    expires_in: int = 600

    @classmethod
    def from_response(cls, r: dict, base_url: Optional[str] = None) -> "DeviceLogin":
        """Build from the server's answer. With ``base_url``, every link must be on that same
        origin (https, or http on loopback): a server must not send you to approve a sign-in
        somewhere else (device-code phishing)."""
        if not isinstance(r, dict) or not r.get("code") or not r.get("login_url"):
            raise A4sxError("login start: the server sent no pairing")
        if base_url is not None:
            for key in ("login_url", "short_url", "verification_url"):
                link = r.get(key)
                if link and not _same_origin(str(link), base_url):
                    raise A4sxError(f"login start: the server's {key} is not on {base_url}; refusing to "
                                    "show or open it")
        try:
            exp = int(r.get("expires_in") or 600)
        except (TypeError, ValueError):
            exp = 600
        exp = max(30, min(exp, MAX_PAIRING_S))
        return cls(
            code=str(r["code"]),
            login_url=str(r["login_url"]),
            user_code=str(r.get("user_code") or ""),
            short_url=str(r.get("short_url") or ""),
            verification_url=str(r.get("verification_url") or ""),
            expires_in=exp,
        )


@dataclass
class FetchedSession:
    """A saved session's transcript, fetched from your library."""

    cid: str
    member_id: str
    transcript: bytes = field(repr=False)
    version: str = ""
    row: dict = field(default_factory=dict, repr=False)
    resumed: str = "latest"
    from_compact: bool = False
    acquired_now: Optional[dict] = None
    untrusted: bool = False  # contains someone else's content (acquired, or descends from such)
    harness: str = "unknown"  # the harness it was recorded with (``claude-code``, ``codex-cli``, ...)

    @property
    def session_id(self) -> Optional[str]:
        return session_id_of(self.transcript)

    @property
    def recorded_cwd(self) -> Optional[str]:
        return recorded_cwd_of(self.transcript)

    @property
    def acquired_from(self) -> Optional[dict]:
        """Where the entry was acquired from, if it is a copy of someone else's listing."""
        af = self.row.get("acquired_from") or (self.acquired_now or {}).get("acquired_from")
        return af if isinstance(af, dict) and af else None

    @property
    def has_compact(self) -> bool:
        return bool(self.row.get("compact_version"))

    def save(self, path: Optional[str] = None) -> str:
        """Write the transcript to ``path`` (default: the cache dir, named by its session id), 0600."""
        if path is None:
            name = self.session_id or self.cid
            path = os.path.join(_config.cache_dir(), f"{_safe_name(name)}.jsonl")
        os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(self.transcript)
        try:
            os.chmod(path, 0o600)  # an existing file keeps its mode on O_TRUNC
        except OSError:
            pass
        return path


@dataclass
class ResumedSession:
    local: LocalSession
    fetched: FetchedSession


@dataclass
class PushResult:
    response: dict
    scrub: ScrubReport
    parent: Optional[str] = None
    untrusted: bool = False

    @property
    def harness(self) -> str:
        """The harness the server recognised the upload as (``claude-code``, ``codex-cli``, ...)."""
        return harness_id(self.response.get("harness"))


MAX_PAIRING_S = 900  # a server-sent expiry is clamped, so a login never waits unbounded


def _same_origin(url: str, base_url: str) -> bool:
    """True if ``url`` is an http(s) URL on exactly the origin of ``base_url``."""
    from urllib.parse import urlsplit

    try:
        u = urlsplit(url)
        if u.scheme.lower() not in ("http", "https") or not u.hostname:
            return False
        return normalize_base_url(f"{u.scheme}://{u.netloc}") == normalize_base_url(base_url)
    except ValueError:
        return False


def _safe_name(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "-" for c in s)[:128] or "session"


# --- lineage sidecars ------------------------------------------------------------------------------


def record_lineage(local_session_id: str, parent_cid: str, parent_version: str = "", base_session_id: str = "",
                   untrusted: bool = False) -> None:
    """Remember that a local session was seeded from a saved one, so a later fresh push records
    the parent edge and your library shows it as a tree, and keeps an "untrusted content" marker
    if the saved one had it. Best effort."""
    if not local_session_id or not parent_cid:
        return
    try:
        _config.write_private(
            os.path.join(_config.lineage_dir(), f"{_safe_name(local_session_id)}.json"),
            json.dumps({"parent_cid": parent_cid, "parent_version": parent_version or "",
                        "base_internal": base_session_id or "", "untrusted": bool(untrusted)}),
        )
    except OSError:
        pass


# --- untrusted-content marker ----------------------------------------------------------------------
# A session acquired from another author is untrusted input (it can carry planted instructions); the
# server marks it with `acquired_from`. That marker must survive what we derive from it: a resumed and
# re-pushed continuation, a crafted base, a version pushed --into another entry. Derived entries are
# found through their server-side `parent` edges (so this works on any machine), and, where no edge
# can carry it, through a local registry of collection ids (`untrusted.json`, per server).


def _untrusted_file() -> str:
    return os.path.join(_config.config_dir(), "untrusted.json")


def _untrusted_registry() -> dict:
    try:
        with open(_untrusted_file(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def marked_untrusted(base_url: str) -> List[str]:
    """Collection ids this machine has marked as holding untrusted content on ``base_url``."""
    ids = _untrusted_registry().get(normalize_base_url(base_url))
    return [str(x) for x in ids] if isinstance(ids, list) else []


def mark_untrusted(base_url: str, cid: str) -> None:
    if not cid:
        return
    reg = _untrusted_registry()
    origin = normalize_base_url(base_url)
    ids = [str(x) for x in reg.get(origin) or [] if isinstance(x, str)]
    if cid not in ids:
        ids.append(cid)
        reg[origin] = ids
        _config.write_private(_untrusted_file(), json.dumps(reg, indent=2) + "\n")


def row_is_untrusted(row: dict, rows: Sequence[dict], base_url: str) -> bool:
    """True if the library row, or any ancestor through its ``parent`` edges, was acquired from
    another author or is marked untrusted locally."""
    by_id = {r.get("id"): r for r in rows}
    local = set(marked_untrusted(base_url))
    seen = set()
    cur: Optional[dict] = row
    while isinstance(cur, dict) and cur.get("id") not in seen:
        seen.add(cur.get("id"))
        if cur.get("acquired_from") or cur.get("id") in local:
            return True
        par = cur.get("parent") if isinstance(cur.get("parent"), dict) else {}
        cur = by_id.get(par.get("cid"))
    return False


def read_lineage(local_session_id: Optional[str]) -> Optional[dict]:
    if not local_session_id:
        return None
    try:
        with open(os.path.join(_config.lineage_dir(), f"{_safe_name(local_session_id)}.json"), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


# --- upload sizing ---------------------------------------------------------------------------------

UPLOAD_TIMEOUT_MAX_S = 1800
UPLOAD_TIMEOUT_OVERRIDE_MAX_S = 21600
PUSH_RETRY_WAITS_S = (5, 15)
PUSH_RETRY_STATUSES = (0, 429, 502, 503, 504)  # 0 = network error or client-side timeout
# The server's scrub pool is busy: `scrub_busy` (429: your own earlier uploads are still being scrubbed)
# or `scrub_pool_busy` (503). Nothing was stored, so the same push is sent again: more attempts than for
# a generic failure, each after the server's `retry_after_s` (capped). `413 scrub_timeout` is never retried.
PUSH_BUSY_CODES = ("scrub_busy", "scrub_pool_busy")
PUSH_BUSY_MAX_ATTEMPTS = 6
PUSH_RETRY_WAIT_CAP_S = 60


def push_retry_wait(attempt: int, body: Any, rand: Callable[[float, float], float] = random.uniform) -> float:
    """Seconds to wait before the next attempt of a push: the server's ``details.retry_after_s`` when it
    sent one (at most :data:`PUSH_RETRY_WAIT_CAP_S`), else :data:`PUSH_RETRY_WAITS_S`, plus up to 25 %
    jitter, so pushes that were refused together don't come back together."""
    try:
        hint = int(_error_fields(body)[2].get("retry_after_s") or 0)
    except (TypeError, ValueError):
        hint = 0
    if hint > 0:
        base = min(hint, PUSH_RETRY_WAIT_CAP_S)
    else:
        base = PUSH_RETRY_WAITS_S[min(attempt, len(PUSH_RETRY_WAITS_S) - 1)]
    return base + rand(0, base * 0.25)


def upload_timeout(n_bytes: int) -> int:
    """Read timeout for an upload. The server answers only after it has received, scrubbed and
    stored the whole file: 120 s plus about 2 s per MB, at most 30 minutes.
    ``$A4SX_UPLOAD_TIMEOUT_S`` (a positive integer, at most 6 h) overrides it."""
    try:
        v = int(_config.env("UPLOAD_TIMEOUT_S"))
    except ValueError:
        v = 0
    if v > 0:
        return min(v, UPLOAD_TIMEOUT_OVERRIDE_MAX_S)
    return min(UPLOAD_TIMEOUT_MAX_S, 120 + int(n_bytes) // (512 * 1024))


# --- the client ------------------------------------------------------------------------------------

ProgressFn = Callable[[str], None]


class Client:
    """A client for one a4sx server.

    ``base_url``: ``https://session-exchange.com`` by default; see :func:`a4sx.config.resolve_base_url`.
    ``token``: a session token; by default ``$A4SX_SESSION`` or the stored token, read when needed.
    ``sleep``, ``clock`` and ``rand`` (the retry jitter) are injectable for tests.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        *,
        timeout: float = 120,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        rand: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.base_url = resolve_base_url(base_url)
        self._token = token
        self.timeout = timeout
        self._sleep = sleep
        self._clock = clock
        self._rand = rand
        self.user_agent = f"a4sx/{__version__} (python {platform.python_version()})"
        #: After a device login that created a NEW account: the server's hint for it, else "".
        self.new_account_hint = ""

    def __repr__(self) -> str:  # never shows the token
        return f"Client(base_url={self.base_url!r})"

    # -- transport --------------------------------------------------------------------------------

    @property
    def token(self) -> Optional[str]:
        """The session token for THIS server, or None. A token stored for another server is never
        used here. Never print it."""
        return self._token or load_token(self.base_url)

    def require_token(self) -> str:
        tok = self.token
        if not tok:
            others = [o for o in _config.signed_in_origins() if o != self.base_url]
            msg = f"not signed in to {self.base_url}: run `a4sx login`"
            if self.base_url != DEFAULT_BASE_URL:
                msg += f" --base {self.base_url}"
            if others:
                msg += f" (you are signed in to {', '.join(others)}; tokens are never sent to another server)"
            raise NotLoggedIn(msg)
        return tok

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Any = None,
        data: Optional[bytes] = None,
        content_type: Optional[str] = None,
        auth: bool = True,
        token: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        raw: bool = False,
    ) -> Tuple[int, Any]:
        """One HTTP call. Returns ``(status, body)``; the body is parsed JSON unless ``raw``.

        Raises :class:`NetworkError` when the server can't be reached. The bearer token is attached
        as an *unredirected* header, so it is never forwarded if the server answers with a redirect.
        """
        q = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        url = self.base_url + path + (("&" if "?" in path else "?") + urlencode(q, doseq=True) if q else "")
        if data is None and json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            content_type = content_type or "application/json"
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("User-Agent", self.user_agent)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", content_type or "application/octet-stream")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        bearer = token or (self.token if auth else None)
        if bearer:
            req.add_unredirected_header("Authorization", "Bearer " + bearer)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                payload = r.read()
                status = r.status
        except urllib.error.HTTPError as e:
            payload, status = e.read(), e.code
        except OSError as e:
            raise NetworkError(f"could not reach {self.base_url}: {getattr(e, 'reason', None) or e}") from None
        if raw:
            return status, payload
        try:
            return status, json.loads(payload.decode("utf-8") or "{}")
        except ValueError:
            return status, {}

    def _call(self, method: str, path: str, what: str, ok: Iterable[int] = (200, 201, 202), **kw) -> Any:
        status, body = self.request(method, path, **kw)
        if status not in tuple(ok):
            raise ApiError(status, body, what)
        return body

    # -- auth -------------------------------------------------------------------------------------

    def start_device_login(
        self,
        provider: Optional[str] = None,
        replaces: Optional[str] = None,
        on_wait: Optional[Callable[[int, int], None]] = None,
        attempts: int = 4,
    ) -> DeviceLogin:
        """``POST /auth/cli/start``. Rides out a deploy (503) or a rate limit (429): waits what the
        server asks (``details.retry_after_s``), at most 30 s per try. ``on_wait(status, seconds)``
        is told before each wait."""
        body: Dict[str, str] = {}
        if provider:
            body["provider"] = provider
        if replaces:
            body["replaces"] = replaces
        for attempt in range(attempts):
            status, r = self.request("POST", "/auth/cli/start", json_body=body, auth=False)
            if status in (200, 201):
                return DeviceLogin.from_response(r, self.base_url)
            if status not in (429, 503) or attempt == attempts - 1:
                raise ApiError(status, r, "login start")
            _, _, details = _error_fields(r)
            try:
                wait = int(details.get("retry_after_s") or (5 if status == 503 else 20))
            except (TypeError, ValueError):
                wait = 5
            wait = max(1, min(wait, 30))
            if on_wait:
                on_wait(status, wait)
            self._sleep(wait)
        raise A4sxError("login start failed")  # pragma: no cover

    def poll_device_login(self, login: Union[DeviceLogin, str]) -> Optional[str]:
        """One poll: the session token once approved, else None. Raises :class:`LoginExpired`."""
        code = login.code if isinstance(login, DeviceLogin) else login
        status, r = self.request("GET", "/auth/cli/poll", params={"code": code}, auth=False)
        if status == 200 and isinstance(r, dict) and r.get("session"):
            hint = r.get("hint")
            self.new_account_hint = hint if r.get("new_account") and isinstance(hint, str) else ""
            return str(r["session"])
        if status == 404:
            raise LoginExpired("the sign-in link expired")
        if status >= 400 and status not in (408, 425, 429, 502, 503, 504):
            raise ApiError(status, r, "login poll")
        return None

    def device_login(
        self,
        provider: Optional[str] = None,
        *,
        on_pairing: Optional[Callable[[DeviceLogin, bool], None]] = None,
        on_waiting: Optional[Callable[[DeviceLogin, int], None]] = None,
        on_wait: Optional[Callable[[int, int], None]] = None,
        max_restarts: int = 5,
        poll_interval: float = 2.0,
        note_every: float = 30.0,
    ) -> str:
        """Run the whole device login and return the session token (not stored; see :meth:`login`).

        ``on_pairing(login, restarted)`` must show the link and code to the person. An expired
        pairing is restarted in place (``replaces=<old code>``: a link already opened keeps working)
        up to ``max_restarts`` times, then :class:`LoginTimedOut`. ``on_waiting(login, seconds_left)``
        is called about every ``note_every`` seconds.
        """
        login = self.start_device_login(provider, on_wait=on_wait)
        if on_pairing:
            on_pairing(login, False)
        restarts = 0
        last_note = self._clock()
        while True:
            deadline = self._clock() + login.expires_in
            while self._clock() < deadline:
                try:
                    tok = self.poll_device_login(login)
                except LoginExpired:
                    break
                except NetworkError:
                    tok = None
                if tok:
                    return tok
                if on_waiting and self._clock() - last_note >= note_every:
                    on_waiting(login, int(deadline - self._clock()))
                    last_note = self._clock()
                self._sleep(poll_interval)
            if restarts >= max_restarts:
                raise LoginTimedOut("nobody approved the sign-in")
            restarts += 1
            login = self.start_device_login(provider, replaces=login.code, on_wait=on_wait)
            if on_pairing:
                on_pairing(login, True)
            last_note = self._clock()

    def ensure_library(self) -> dict:
        """``POST /library/ensure``: provision your private library (idempotent)."""
        return self._call("POST", "/library/ensure", "library ensure", token=self.require_token())

    def me(self) -> dict:
        """``GET /accounts/me``: the signed-in account."""
        return self._call("GET", "/accounts/me", "whoami", token=self.require_token())

    def login(self, provider: Optional[str] = None, *, save: bool = True, **callbacks: Any) -> str:
        """Device login, then provision the library, then (``save=True``) store the token (0600)
        and remember this server. Returns the token. Callbacks as in :meth:`device_login`.

        An account that still has to confirm an e-mail address (it signed in with a provider that sent
        none) gets no library yet: the token is stored and :class:`EmailVerificationPending` is raised;
        confirm the address in the browser, then log in again."""
        tok = self.device_login(provider, **callbacks)
        self._token = tok
        if save:
            self._persist(tok)
        status, me = self.request("GET", "/accounts/me", token=tok)
        if status == 200 and isinstance(me, dict) and me.get("email_verification_pending") is True:
            raise EmailVerificationPending(
                f"signed in, but this account must confirm an e-mail address first: open {self.base_url}/ "
                "in a browser, confirm the code that is e-mailed to you, then log in again")
        self.ensure_library()
        return tok

    def login_with_token(self, token: str, *, save: bool = True) -> dict:
        """Check that ``token`` is a live session (``GET /accounts/me``), then store it.
        Returns the account. Raises :class:`ApiError` if the server refuses it."""
        token = (token or "").strip()
        if not token:
            raise ValueError("empty token")
        me = self._call("GET", "/accounts/me", "token check", token=token)
        self._token = token
        if save:
            self._persist(token)
        return me if isinstance(me, dict) else {}

    def _persist(self, token: str) -> None:
        save_token(token, self.base_url)
        settings = _config.load_settings()
        settings["base"] = self.base_url
        _config.save_settings(settings)

    def logout(self, revoke: bool = True) -> bool:
        """Forget the stored token; with ``revoke`` also end the session on the server (best effort).
        Returns True if a stored token was removed."""
        tok = self.token
        if revoke and tok:
            try:
                self.request("POST", "/auth/logout", token=tok)
            except NetworkError:
                pass
        self._token = None
        return clear_token(self.base_url)

    # -- library ----------------------------------------------------------------------------------

    def list_sessions(self) -> List[dict]:
        """Your saved sessions (``GET /library/sessions``), as the server returns them: ``id``,
        ``label``, ``title``, ``created_at``, ``updated_at``, ``versions``, ``tokens``, ``parent``,
        ``acquired_from``, ``compact_version`` ..."""
        r = self._call("GET", "/library/sessions", "list", token=self.require_token())
        rows = r.get("sessions") if isinstance(r, dict) else None
        return [x for x in (rows or []) if isinstance(x, dict)]

    @staticmethod
    def resolve(rows: Sequence[dict], name: str) -> Optional[dict]:
        """Match ``name`` against library rows: exact id, then label, then a UNIQUE substring of
        title or id. None if nothing (or more than one row) matches."""
        for c in rows:
            if c.get("id") == name:
                return c
        for c in rows:
            if c.get("label") == name or f"label:{name}" in (c.get("tags") or []):
                return c
        low = name.lower()
        hits = [c for c in rows if low in (str(c.get("title") or "") + str(c.get("id") or "")).lower()]
        return hits[0] if len(hits) == 1 else None

    def find(self, name: str) -> Optional[dict]:
        return self.resolve(self.list_sessions(), name)

    def search(
        self,
        query: str = "",
        *,
        scope: str = "public",
        tags: Sequence[str] = (),
        kind: str = "",
        sort: str = "relevance",
        limit: int = 10,
        offset: int = 0,
        harness: Sequence[str] = (),
    ) -> dict:
        """Search sessions. ``scope="public"``: the published marketplace (anonymous, no sign-in);
        ``scope="library"``: your own saved sessions. Returns ``{scope, query, total, next_offset,
        items}``; public items are the server's listing rows (``id``, ``tenant``, ``title``,
        ``description``, ``tags``, ``price_cents``, ``harness`` ...).

        ``harness`` keeps only sessions recorded with these harnesses (``claude-code``, ``codex-cli``,
        ``grok-build`` ...; ``unknown`` = not identified)."""
        query = (query or "").strip()
        harn = {str(h).strip().lower() for h in harness if str(h).strip()}
        if scope == "library":
            want = {t.lower() for t in tags}
            low = query.lower()
            rows = []
            for r in self.list_sessions():
                hay = " ".join(str(r.get(k) or "") for k in ("id", "title", "label", "description", "summary")).lower()
                rtags = {str(t).lower() for t in r.get("tags") or []}
                if harn and harness_id(r.get("harness")) not in harn:
                    continue
                if (not low or low in hay) and want <= rtags:
                    rows.append(r)
            page = rows[offset : offset + limit]
            nxt = offset + len(page) if offset + len(page) < len(rows) else None
            return {"scope": "library", "query": query, "total": len(rows), "next_offset": nxt, "items": page}
        if scope != "public":
            raise ValueError("scope must be 'public' or 'library'")
        r = self._call(
            "GET", "/memory/discover", "search", auth=False,
            params={"q": query, "tag": ",".join(tags), "kind": kind, "sort": sort, "limit": limit, "offset": offset,
                    "harness": ",".join(sorted(harn))},
        )
        r = r if isinstance(r, dict) else {}
        items = [x for x in r.get("items") or [] if isinstance(x, dict)]
        return {"scope": "public", "query": query, "total": r.get("total", len(items)),
                "next_offset": r.get("next_offset"), "items": items}

    def get_listing(self, listing_id: str, workspace: Optional[str] = None) -> dict:
        """One public marketplace listing (anonymous)."""
        r = self._call("GET", "/memory/discover/item", "listing", auth=False,
                       params={"id": listing_id, "t": workspace})
        return (r.get("item") if isinstance(r, dict) else None) or {}

    def acquire(self, cid: str, workspace: Optional[str] = None, version: Optional[str] = None) -> dict:
        """Copy a marketplace listing you are entitled to (free, bought or granted) into your private
        library. Never buys anything: a paid listing raises :class:`PaymentRequired`.
        Returns ``{collection, session, acquired_from, ...}``."""
        status, r = self.request(
            "POST", f"/library/acquire/{quote(cid, safe='')}",
            params={"tenant": workspace, "version": version}, token=self.require_token(),
        )
        if status in (200, 201) and isinstance(r, dict) and r.get("collection"):
            return r
        code = _error_fields(r)[0]
        if status == 402:
            raise PaymentRequired(status, r, f"acquire {cid}")
        if status == 409 and code == "memory_only":
            raise MemoryOnly(status, r, f"acquire {cid}")
        raise ApiError(status, r, f"acquire {cid}")

    def is_untrusted(self, name_or_row: Union[str, dict], rows: Optional[Sequence[dict]] = None) -> bool:
        """Whether a saved session holds someone else's content (see :func:`row_is_untrusted`)."""
        rows = list(rows) if rows is not None else self.list_sessions()
        row = name_or_row if isinstance(name_or_row, dict) else self.resolve(rows, name_or_row)
        return bool(row) and row_is_untrusted(row, rows, self.base_url)

    def _checked_get(self, path: str, name: str) -> Tuple[int, Any]:
        """GET for a pre-acquire harness check. FAILS CLOSED: no answer (offline, timeout) or a 5xx
        raises, so nothing is acquired on a check that could not be made. Any other status is returned."""
        try:
            status, r = self.request("GET", path)
        except NetworkError as e:
            raise A4sxError(f"could not check the marketplace listing {name!r} ({e}); nothing was added to "
                            "your library. Try again in a moment.") from None
        if status >= 500:
            raise ApiError(status, r, f"listing check for {name!r} (nothing was added to your library)")
        return status, r

    def listing_harness(self, cid: str) -> str:
        """The harness of marketplace id ``cid`` as this caller may read it, without acquiring anything:
        the public listing, else the collection (an entitled buyer can read it even when the public
        listing answers 404). ``unknown`` when neither says. Raises when the check itself fails."""
        status, r = self._checked_get("/memory/discover/item?" + urlencode({"id": cid}), cid)
        item = r.get("item") if status == 200 and isinstance(r, dict) else None
        if isinstance(item, dict) and item.get("harness") is not None:
            return harness_id(item.get("harness"))
        status, r = self._checked_get(f"/memory/collections/{quote(cid, safe='')}", cid)
        return harness_id(r.get("harness") if status == 200 and isinstance(r, dict) else None)

    def _row_or_acquire(self, name: str, acquire: bool, resume: bool = False) -> Tuple[dict, Optional[dict]]:
        rows = self.list_sessions()
        self._last_rows = rows
        row = self.resolve(rows, name)
        if row is not None:
            if resume and not _resumable(harness_id(row.get("harness"))):
                raise OtherHarness(name, harness_id(row.get("harness")))
            return row, None
        if acquire and resume:
            # Refuse another harness BEFORE the acquire, so a refusal writes nothing server-side.
            hid = self.listing_harness(name)
            if not _resumable(hid):
                raise OtherHarness(name, hid)
        if acquire:
            try:
                got = self.acquire(name)
            except (PaymentRequired, MemoryOnly):
                raise
            except ApiError as e:
                if e.session_ended:
                    raise
                got = None
            if got:
                rows = self.list_sessions()
                self._last_rows = rows
                row = self.resolve(rows, got["collection"])
                if row is not None:
                    return row, got
        raise NotFound(f"no saved session or entitled listing matches {name!r}")

    def fetch(self, name: str, *, member: Optional[str] = None, compact: bool = False,
              acquire: bool = True, for_resume: bool = False) -> FetchedSession:
        """Fetch a saved session's transcript by id, label or a unique part of its title.

        Not in your library but an entitled marketplace id? With ``acquire=True`` it is acquired
        first (a private copy). ``compact=True`` returns the designated compact tip, else the
        context after the transcript's last ``/compact``; raises if there is neither.

        A session recorded with another harness (``result.harness``) comes back as its stored native
        file (e.g. a Codex CLI rollout)."""
        row, got = self._row_or_acquire(name, acquire, resume=for_resume)
        untrusted = bool(got) or row_is_untrusted(row, getattr(self, "_last_rows", [row]), self.base_url)
        cid = str(row["id"])
        tok = self.require_token()
        compact_ver = row.get("compact_version")
        if compact and compact_ver:
            vb = self._call("GET", f"/library/session/{quote(cid, safe='')}/version/{quote(str(compact_ver), safe='')}",
                            "fetch version", token=tok)
            data = base64.b64decode((vb or {}).get("transcript_b64") or "")
            return FetchedSession(cid, str(row.get("session") or cid), data, str(row.get("version") or ""),
                                  row, str(compact_ver), True, got, untrusted, harness_id(row.get("harness")))
        bd = self._call("GET", "/library/fetch", "fetch", params={"cid": cid}, token=tok)
        members = [m for m in (bd or {}).get("members", []) if isinstance(m, dict) and m.get("kind") == "session"]
        if not members:
            raise A4sxError(f"saved session {cid} has no session member")
        mem = next((m for m in members if m.get("id") == member), members[0])
        files = [f for f in mem.get("files", []) if isinstance(f, dict)]
        paths = [str(f.get("path", "")) for f in files]
        hid = _harness_of(bd.get("harness") if isinstance(bd, dict) else None, paths)
        if hid == "unknown":
            hid = harness_id(row.get("harness"))
        if for_resume and not _resumable(hid):
            raise OtherHarness(name, hid)
        # Claude Code: transcript.jsonl. Other harnesses are stored as native/<harness>.<ext>.
        tf = next((f for f in files if str(f.get("path", "")).endswith(".jsonl")
                   and not str(f.get("path", "")).startswith("native/")), None)
        tf = tf or next((f for f in files if str(f.get("path", "")).startswith("native/")), None)
        if tf is None:
            raise A4sxError(f"session {mem.get('id')} has no transcript")
        data = base64.b64decode(tf.get("content_b64") or "")
        fetched = FetchedSession(cid, str(mem.get("id") or ""), data, str(row.get("version") or ""), row,
                                 acquired_now=got, untrusted=untrusted, harness=hid)
        if compact:
            trimmed = trim_to_last_compact(data)
            if not trimmed:
                raise A4sxError(f"{name!r} has no compact tip and was never compacted: run /compact in "
                                "Claude Code (or push a compact tip with --into ... --compact), then retry")
            fetched.transcript, fetched.from_compact = trimmed[0], True
            fetched.resumed = f"compaction boundary ({trimmed[1]} records)"
        return fetched

    def resume(self, name: str, *, cwd: Optional[str] = None, compact: bool = False,
               claude_config_dir: Optional[str] = None, acquire: bool = True) -> ResumedSession:
        """Fetch a saved session and write it as a NEW local Claude Code session in ``cwd``
        (fresh session id, ``sessionId`` rewritten, written into the project dir). The saved copy is
        never changed. Records the lineage so a later push of the new session links back to it.
        Then run ``claude --resume <id>`` (``result.local.resume_command``) in ``cwd``.

        Only Claude Code sessions can be resumed: one recorded with another harness raises
        :class:`OtherHarness`, and a marketplace listing is checked BEFORE it is acquired."""
        fetched = self.fetch(name, compact=compact, acquire=acquire, for_resume=True)
        local = resume_locally(fetched.transcript, cwd=cwd, claude_config_dir=claude_config_dir)
        record_lineage(local.session_id, fetched.cid, fetched.version, local.base_session_id or "",
                       untrusted=fetched.untrusted)
        return ResumedSession(local, fetched)

    def push(
        self,
        data: bytes,
        *,
        label: Optional[str] = None,
        title: Optional[str] = None,
        into: Optional[str] = None,
        parent: Optional[str] = None,
        compact: bool = False,
        scrub: bool = True,
        keep_pii: bool = False,
        lineage: bool = True,
        extra_params: Optional[Dict[str, str]] = None,
        untrusted: bool = False,
        on_retry: Optional[Callable[[int, int], None]] = None,
    ) -> PushResult:
        """Save a transcript to your private library (``POST /library/push``).

        - ``scrub``: run the client-side scrub first (:func:`a4sx.scrub.scrub_bytes`, the a4sx CLI's rules:
          secrets and personal data); the server scrubs again either way. The report is in
          ``result.scrub``. ``keep_pii=True`` keeps personal data, hosts, IPs and paths (``secrets-only``).
        - ``into``: store as a new version of that saved session; ``compact`` marks it as the compact
          tip to resume from.
        - ``parent`` (a name or id) records a lineage edge; otherwise, for a fresh push, the edge
          recorded by :meth:`resume` for this transcript's session id is used (``lineage=True``).

        One push is sent with one ``Idempotency-Key``; a lost or refused attempt (network error, 429,
        502-504) is retried twice with the same key, so a retry never creates a duplicate version. A busy
        server scrub pool (``scrub_busy``/``scrub_pool_busy``) is retried up to 6 attempts, each after the
        wait the server asks for (at most 60 s). ``on_retry(status, wait_s)`` is told before each retry.

        Claude Code, Codex CLI and Grok Build sessions are accepted; the server recognises the format
        (``result.harness``).

        Untrusted content stays marked: if the transcript was resumed from an untrusted session (its
        lineage says so), ``parent`` or ``into`` is untrusted, or ``untrusted=True``, the new entry is
        marked too (``result.untrusted``), so a later resume treats it as someone else's content.
        """
        upload, report = scrub_bytes(bytes(data), enabled=scrub, mode="secrets-only" if keep_pii else "full")
        tok = self.require_token()
        q: Dict[str, str] = {k: v for k, v in (("label", label), ("title", title), ("into", into)) if v}
        edge = None
        side = read_lineage(session_id_of(upload)) if lineage else None
        untrusted = bool(untrusted) or bool(side and side.get("untrusted"))
        rows: Optional[List[dict]] = None
        if parent:
            rows = self.list_sessions()
            row = self.resolve(rows, parent)
            edge = str(row["id"]) if row else parent
            untrusted = untrusted or bool(row and row_is_untrusted(row, rows, self.base_url))
        elif side and side.get("parent_cid") and not into:
            pv = side.get("parent_version") or ""
            edge = f"{side['parent_cid']}@{pv}" if pv else str(side["parent_cid"])
        if into and not untrusted:
            rows = rows if rows is not None else self.list_sessions()
            row = self.resolve(rows, into)
            untrusted = bool(row and row_is_untrusted(row, rows, self.base_url))
        if edge:
            q["parent"] = edge
        if compact and into:
            q["compact"] = "1"
        q.update(extra_params or {})
        # What the client scrub did (upload metadata; the server scrubs regardless): full | secrets-only | none.
        q["client_scrub"] = report.mode
        headers = {"Idempotency-Key": str(uuid.uuid4()), "X-Content-SHA256": report.uploaded_sha256}
        timeout = upload_timeout(len(upload))
        attempt = 0
        while True:
            try:
                status, r = self.request("POST", "/library/push", params=q, data=upload, token=tok,
                                         content_type="application/octet-stream", headers=headers, timeout=timeout)
                net_error = None
            except NetworkError as e:
                status, r, net_error = 0, {}, e
            busy = status in (429, 503) and _error_fields(r)[0] in PUSH_BUSY_CODES
            last = attempt + 1 >= (PUSH_BUSY_MAX_ATTEMPTS if busy else len(PUSH_RETRY_WAITS_S) + 1)
            if status not in PUSH_RETRY_STATUSES or last:
                if net_error is not None:
                    raise net_error
                break
            wait = push_retry_wait(attempt, r, self._rand)
            if on_retry:
                on_retry(status, int(round(wait)))
            self._sleep(wait)
            attempt += 1
        if status not in (200, 201, 202):
            raise ApiError(status, r, "push")
        r = r if isinstance(r, dict) else {}
        if untrusted:
            try:
                mark_untrusted(self.base_url, str(r.get("collection") or into or ""))
            except OSError:
                pass
        return PushResult(r, report, edge, untrusted)

    def push_file(self, path: str, **kw: Any) -> PushResult:
        """:meth:`push` the file at ``path`` (``~`` is expanded; a Grok Build session folder pushes its
        ``updates.jsonl``, see :func:`push_path`)."""
        with open(push_path(path), "rb") as f:
            return self.push(f.read(), **kw)

    def craft(self, transcripts: Sequence[bytes], *, label: Optional[str] = None, title: Optional[str] = None,
              into: Optional[str] = None, parent: Optional[str] = None, scrub: bool = True,
              untrusted: bool = False,
              on_retry: Optional[Callable[[int, int], None]] = None) -> Tuple[PushResult, str, dict]:
        """Compile ONE resumable base from several transcripts (:func:`a4sx.transcript.stitch`) and
        push it. Like every upload, the stitched result is pre-scrubbed first (``result.scrub``);
        ``scrub=False`` is an explicit opt-out. Pass ``untrusted=True`` if any source holds someone
        else's content (:meth:`fetch` results carry ``.untrusted``), so the base stays marked.
        Returns ``(push_result, new_session_id, report)``."""
        blob, sid, rep = stitch(transcripts)
        res = self.push(blob, label=label, title=title, into=into, parent=parent, scrub=scrub, lineage=False,
                        untrusted=untrusted, extra_params={"craft_sources": str(len(transcripts))},
                        on_retry=on_retry)
        return res, sid, rep

    def delete(self, name: str) -> dict:
        """PERMANENTLY delete a saved session (by id, label or unique title part). Every stored
        version is erased server-side shortly after (``erase_within_hours`` in the reply)."""
        row = self.find(name)
        if row is None:
            raise NotFound(f"no unambiguous saved session matches {name!r}")
        r = self._call("DELETE", f"/library/session/{quote(str(row['id']), safe='')}", "delete",
                       token=self.require_token())
        return r if isinstance(r, dict) else {}

    def server_version(self) -> Optional[str]:
        """The server's version (``GET /healthz``), or None."""
        try:
            status, r = self.request("GET", "/healthz", auth=False, timeout=10)
        except NetworkError:
            return None
        return str(r["version"]) if status == 200 and isinstance(r, dict) and r.get("version") else None
