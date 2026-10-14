# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 crossVault GmbH
"""The ``a4sx`` command. A thin layer over :mod:`a4sx.sdk`: parsing, printing, exit codes.

stdout carries results (JSON where a script may parse it); progress, notes and errors go to stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from typing import List, Optional, Sequence

from . import __version__, sdk
from . import config as cfg
from .scrub import scrub_bytes
from .transcript import latest_local_session, token_stats

PROG = "a4sx"
SESSION_FLAG = "--a4sx-session"
FAIL_ON_SCRUB_EXIT = 3
LIST_SORTS = ("updated", "created", "name")


def _err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --- next-step hints -------------------------------------------------------------------------------
# One "next:" line on stderr after push / fetch / acquire / craft / resume, for a person at a terminal
# only: nothing when stderr is not a TTY or A4SX_HINTS=0. stdout never changes.


def _hints_on() -> bool:
    if cfg.env("HINTS").lower() in ("0", "false", "off", "no"):
        return False
    try:
        return sys.stderr.isatty()
    except Exception:
        return False


def _hint(text: str) -> None:
    if _hints_on():
        print(f"next: {text}", file=sys.stderr)


def _client(a: argparse.Namespace) -> sdk.Client:
    return sdk.Client(getattr(a, "base", None))


# --- login -----------------------------------------------------------------------------------------

_TEXT_BROWSERS = ("lynx", "w3m", "links", "elinks", "www-browser", "browsh")


def no_browser_reason(force: bool = False) -> Optional[str]:
    """None when a graphical browser can be opened here, else why not (shown to the user)."""
    if force:
        return "--no-browser was given"
    if cfg.env("NO_BROWSER"):
        return "A4SX_NO_BROWSER is set"
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY") or os.environ.get("SSH_CLIENT"):
        return "this is an SSH session"
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/sys/kernel/osrelease") as f:
                osrel = f.read().lower()
        except OSError:
            osrel = ""
        if "microsoft" in osrel:  # WSL reaches a Windows browser through wslview
            if not (shutil.which("wslview") or shutil.which("explorer.exe")):
                return "WSL without wslview"
            return None
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return "there is no graphical display (DISPLAY / WAYLAND_DISPLAY unset)"
    try:
        import webbrowser

        b = webbrowser.get()
    except Exception:
        return "no web browser is configured"
    name = str(getattr(b, "name", "") or getattr(b, "basename", "") or type(b).__name__).lower()
    if any(t in name for t in _TEXT_BROWSERS):
        return "only a text-mode browser is available"
    return None


def _qr_lines(text: str) -> List[str]:
    """A terminal QR code via the `qrencode` tool when installed; [] otherwise."""
    exe = shutil.which("qrencode")
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "-t", "UTF8", "-m", "1", text], capture_output=True, text=True,
                             timeout=5, check=True).stdout
    except Exception:
        return []
    return out.rstrip("\n").splitlines()


def _show_pairing(login: sdk.DeviceLogin, no_browser: Optional[str], restarted: bool = False) -> None:
    if no_browser is None:
        verb = "Open this URL" if not restarted else "If the browser tab says it expired, open"
        print(f"\n{verb} in your browser and sign in:\n\n  {login.login_url}\n")
    else:
        short = login.short_url or login.login_url
        if not restarted:
            print(f"\nNo browser can be opened from here ({no_browser}).")
        print(f"Sign in from any device, e.g. your phone or another computer:\n\n  {short}\n")
        for line in _qr_lines(short):
            print("  " + line)
        if login.verification_url and login.user_code:
            print(f"  (or open {login.verification_url} and type the code below)\n")
        if not restarted:
            print(f"Already have a session token? `{PROG} login --token -` reads it from stdin; no browser needed.\n")
    if login.user_code:
        print(f"On the approval page, check that this code matches (anti-phishing check):\n\n      {login.user_code}\n",
              flush=True)


def cmd_login(a: argparse.Namespace) -> int:
    client = _client(a)
    print(f"signing in to {client.base_url}", flush=True)
    if a.token is not None:
        if a.token != "-":
            _err(f"{PROG}: warning: a token on the command line is visible in `ps` and your shell history; "
                 f"prefer `{PROG} login --token -` and paste it on stdin.")
        tok = sys.stdin.readline().strip() if a.token == "-" else a.token.strip()
        if not tok:
            sys.exit("no token given")
        try:
            client.login_with_token(tok)
        except sdk.ApiError as e:
            sys.exit(f"that token isn't a valid session (HTTP {e.status}); run `{PROG} login` without --token")
        print(f"saved. your private library on {client.base_url} is ready; no browser needed.")
        return 0
    nb = no_browser_reason(force=a.no_browser)
    state = {"nb": nb}

    def on_pairing(login: sdk.DeviceLogin, restarted: bool) -> None:
        if restarted:
            print("\nthe sign-in expired; started a fresh one (a link you already opened keeps working).", flush=True)
        elif state["nb"] is None:
            try:
                import webbrowser

                if not webbrowser.open(login.login_url):
                    state["nb"] = "the browser could not be opened"
            except Exception:
                state["nb"] = "the browser could not be opened"
        _show_pairing(login, state["nb"], restarted)
        if not restarted:
            print("waiting for you to sign in and approve in the browser...", flush=True)

    def on_waiting(login: sdk.DeviceLogin, left: int) -> None:
        left = max(0, left)
        print(f"  still waiting… ({left // 60}m{left % 60:02d}s left); open the link and approve "
              f"the code {login.user_code}", flush=True)

    def on_wait(status: int, wait: int) -> None:
        why = "is starting up" if status == 503 else "is rate-limiting sign-ins"
        _err(f"the server {why} (HTTP {status}); retrying in {wait}s…")

    try:
        client.login(a.provider, on_pairing=on_pairing, on_waiting=on_waiting, on_wait=on_wait)
    except sdk.LoginTimedOut:
        sys.exit(f"login timed out: nobody approved it. Run `{PROG} login` again when you're ready")
    except sdk.EmailVerificationPending as e:
        print(f"\n{e}.")
        return 0
    except KeyboardInterrupt:
        sys.exit("\nlogin cancelled")
    print(f"\nsigned in. your private library on {client.base_url} is ready.")
    if client.new_account_hint:
        print(f"\nthis sign-in created a new account. {client.new_account_hint}")
    return 0


def cmd_logout(a: argparse.Namespace) -> int:
    client = _client(a)
    removed = client.logout(revoke=not a.keep_server_session)
    print("signed out." if removed else "no stored session (nothing to remove).")
    if cfg.env("SESSION"):
        _err("note: A4SX_SESSION is still set in this shell.")
    return 0


def cmd_whoami(a: argparse.Namespace) -> int:
    me = _client(a).me()
    print(json.dumps(me, indent=2))
    return 0


# --- push ------------------------------------------------------------------------------------------


def _push_path(a: argparse.Namespace) -> str:
    if a.latest:
        path = latest_local_session()
        if not path:
            sys.exit(f"no local Claude Code sessions under {sdk.claude_projects_dir()}; pass a path")
        _err(f"latest session: {path}")
        return path
    if not a.jsonl:
        sys.exit("give a session .jsonl, or --latest.  Find one:  ls -t ~/.claude/projects/*/*.jsonl | head"
                 "   (Codex CLI:  ls -t ~/.codex/sessions/*/*/*/rollout-*.jsonl | head)"
                 "   (Grok Build:  ls -t ${GROK_HOME:-~/.grok}/sessions/*/*/updates.jsonl | head)")
    path = sdk.push_path(a.jsonl)
    if path != os.path.expanduser(a.jsonl):
        _err(f"Grok Build session folder: pushing {path}")
    return path


# Loud warnings for the client-scrub opt-outs, printed on every push that uses one (--dry-run included).
SCRUB_OPT_OUT_WARNINGS = {
    "secrets-only": (
        "WARNING: --keep-pii: the client scrub KEEPS personal data, host names, IP and MAC addresses, "
        "home-directory user names and key-file paths in this upload. Secrets, command-line passwords and "
        "Sheet ids are still removed. The server still scrubs personal data on upload."),
    "none": (
        "WARNING: --no-scrub: the client scrub is OFF. Nothing is removed on this machine: secrets, tokens, "
        "command-line passwords, private keys, personal data, host names, IP addresses and paths leave it "
        "exactly as they are in the file. The server still scrubs on upload, so the stored copy is NOT a raw "
        "backup."),
}


def _scrub_mode(a: argparse.Namespace) -> str:
    if a.no_scrub and a.fail_on_scrub:
        sys.exit(f"{PROG} push: --no-scrub and --fail-on-scrub are mutually exclusive")
    if a.no_scrub and getattr(a, "keep_pii", False):
        sys.exit(f"{PROG} push: --no-scrub and --keep-pii are mutually exclusive "
                 "(--no-scrub already skips every client rule)")
    return "none" if a.no_scrub else "secrets-only" if getattr(a, "keep_pii", False) else "full"


def cmd_push(a: argparse.Namespace) -> int:
    mode = _scrub_mode(a)
    path = _push_path(a)
    if mode in SCRUB_OPT_OUT_WARNINGS:
        _err(f"{PROG} push: {SCRUB_OPT_OUT_WARNINGS[mode]}")
    with open(os.path.expanduser(path), "rb") as f:
        local = f.read()
    upload, rep = scrub_bytes(local, mode=mode)
    if a.dry_run:
        print(f"{PROG} push --dry-run: {path}")
        print(rep.summary())
        for h in rep.hits:
            span = f"lines {h['line']}-{h['to_line']}" if h.get("to_line") else f"line {h['line']}"
            print(f"  {span} col {h['col']}: {h['rule']}  [masked {h['chars']} chars]")
        print(f"local sha256:    {rep.local_sha256}")
        print(f"would upload:    {rep.uploaded_sha256}"
              + ("  (identical to the local file)" if rep.identical else "  (DIFFERS from the local file)"))
        print("dry run: nothing uploaded")
        return FAIL_ON_SCRUB_EXIT if (a.fail_on_scrub and rep.lines) else 0
    _err(f"{PROG} push: {rep.summary()}")
    if a.fail_on_scrub and rep.lines:
        _err(f"{PROG} push: --fail-on-scrub: the client scrub would change the upload; nothing uploaded. "
             "Run with --dry-run to see the lines, fix the source, or drop --fail-on-scrub.")
        return FAIL_ON_SCRUB_EXIT
    _err(f"{PROG} push: local sha256    {rep.local_sha256}")
    if rep.identical:
        _err(f"{PROG} push: uploaded sha256 {rep.uploaded_sha256} (identical to the local file)")
    else:
        _err(f"{PROG} push: uploaded sha256 {rep.uploaded_sha256}: DIFFERS from the local file (the client "
             "scrub rewrote it). Your library will hold the uploaded bytes: `--dry-run` shows where, "
             "`--no-scrub` uploads the file as-is.")
    client = _client(a)

    def on_retry(status: int, wait: int) -> None:
        why = "did not answer" if status == 0 else f"answered HTTP {status}"
        _err(f"{PROG} push: the server {why}; retrying the same push in {wait}s "
             "(safe: it is sent with the same idempotency key)…")

    # The upload is exactly what was reported above (scrub_bytes is deterministic).
    res = client.push(local, label=a.label, title=a.title, into=a.into, parent=a.parent, compact=a.compact,
                      scrub=mode != "none", keep_pii=mode == "secrets-only", on_retry=on_retry)
    if res.parent:
        _err(f"{PROG}: recording lineage: parent {res.parent}")
    srv = res.response.get("redactions")
    if isinstance(srv, int):
        extra = (f"; the {rep.lines_changed} client-side line change(s) above are NOT included"
                 if rep.lines_changed else "")
        _err(f"{PROG} push: server scrub: {srv} redaction{'s' if srv != 1 else ''} "
             f"(counted on the uploaded bytes{extra})")
    print(json.dumps({**res.response, "client_scrub": rep.to_dict(), "status": "staged (private)"}, indent=2))
    handle = a.label or res.response.get("collection") or ""
    if handle:
        if res.harness in ("claude-code", "unknown"):
            _hint(f"resume it any time with `{PROG} resume {handle}` · `{PROG} list` shows your library")
        else:  # only Claude Code sessions resume today; never suggest it for another harness
            shown = sdk.harness_name(res.harness)
            _hint(f"saved as a {shown} session; only Claude Code sessions resume today · "
                  f"`{PROG} list` shows your library")
    return 0


# --- list / search ---------------------------------------------------------------------------------


def fmt_ts(iso: Optional[str], tz=None) -> str:
    """Server ISO-8601 -> compact local ``YYYY-MM-DD HH:MM``; ``-`` when absent or unparseable."""
    from datetime import datetime, timezone

    if not iso:
        return "-"
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return "-"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def sort_rows(rows: Sequence[dict], key: str = "updated") -> List[dict]:
    """updated/created newest first, name A-Z; rows without a time last; ties by name."""
    from datetime import datetime

    def _name(c: dict) -> str:
        return str(c.get("label") or c.get("title") or c.get("id") or "").lower()

    if key == "name":
        return sorted(rows, key=_name)
    fld = "created_at" if key == "created" else "updated_at"

    def _t(c: dict) -> float:
        try:
            return datetime.fromisoformat(str(c.get(fld)).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            return float("-inf")

    return sorted(sorted(rows, key=_name), key=_t, reverse=True)


def cmd_list(a: argparse.Namespace) -> int:
    rows = sort_rows(_client(a).list_sessions(), a.sort)
    if a.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print(f"(your library is empty: `{PROG} push <session.jsonl>` to add one)")
        return 0
    by_cid = {c.get("id"): c for c in rows}
    children: dict = {}
    roots = []
    for c in rows:
        par = (c.get("parent") if isinstance(c.get("parent"), dict) else {}).get("cid")
        if par and par in by_cid:
            children.setdefault(par, []).append(c)
        else:
            roots.append(c)

    def _ctx(c: dict) -> str:
        cur = (c.get("tokens") or {}).get("current") or 0
        return f"{round(cur / 1000)}k" if cur else ""

    def _vers(c: dict) -> str:
        v = c.get("versions")
        return str(v) if isinstance(v, int) and not isinstance(v, bool) else ""

    print(f"{'#':>2}  {'name':30}  {'created':16}  {'updated':16}  {'vers':>4}  {'ctx':>6}  title")
    n = [0]
    seen: set = set()

    def _emit(c: dict, depth: int) -> None:
        cid = c.get("id")
        if cid in seen:
            return
        seen.add(cid)
        n[0] += 1
        prefix = ("  " * (depth - 1) + "└─ ") if depth else ""
        name = prefix + str(c.get("label") or c.get("id") or "(unnamed)")
        par = c.get("parent") if isinstance(c.get("parent"), dict) else {}
        orphan = bool(par.get("cid")) and (c.get("parent_deleted") or par.get("cid") not in by_cid)
        title = (c.get("title") or "") + ("  (parent deleted)" if orphan else "")
        print(f"{n[0]:>2}  {name:30.30}  {fmt_ts(c.get('created_at')):16}  {fmt_ts(c.get('updated_at')):16}  "
              f"{_vers(c):>4}  {_ctx(c):>6}  {title}")
        for ch in children.get(cid, []):
            _emit(ch, depth + 1)

    for r in roots:
        _emit(r, 0)
    for c in rows:  # rows only reachable through a parent cycle
        if c.get("id") not in seen:
            _emit(c, 0)
    return 0


def _price(r: dict) -> str:
    cents = r.get("price_cents")
    if not isinstance(cents, int) or cents <= 0:
        return "free"
    return f"{cents / 100:.2f} {str(r.get('currency') or '').upper()}".strip()


def cmd_search(a: argparse.Namespace) -> int:
    res = _client(a).search(" ".join(a.query), scope="library" if a.library else "public", tags=a.tag or (),
                            sort=a.sort, limit=a.limit, offset=a.offset)
    if a.json:
        print(json.dumps(res, indent=2))
        return 0
    items = res["items"]
    if not items:
        print("(no matches)")
        return 0
    for r in items:
        if res["scope"] == "public":
            ws = f"  --workspace {r['tenant']}" if r.get("tenant") else ""
            print(f"{r.get('id', '')}{ws}\n    {r.get('title') or ''}  [{_price(r)}]")
        else:
            print(f"{r.get('label') or r.get('id', '')}\n    {r.get('title') or ''}")
    if res.get("next_offset") is not None:
        _err(f"more: --offset {res['next_offset']}")
    if res["scope"] == "public":
        _hint(f"`{PROG} acquire <id>` adds a listing you're entitled to; then `{PROG} resume <id>`")
    return 0


# --- fetch / acquire / delete ----------------------------------------------------------------------


def cmd_fetch(a: argparse.Namespace) -> int:
    f = _client(a).fetch(a.name, member=a.session, compact=a.from_compact)
    if f.acquired_now:
        _err(f"{PROG}: added {a.name} to your library from the marketplace -> {f.cid}")
    if not f.session_id:
        sys.exit("the transcript has no sessionId: it can't be resumed")
    path = f.save(a.output)
    print(json.dumps({"collection": f.cid, "session": f.member_id, "internal_sid": f.session_id,
                      "recorded_cwd": f.recorded_cwd, "resumed": f.resumed, "path": path}))
    _hint(f"`{PROG} resume {f.cid}` fetches it and resumes it in Claude Code in one step")
    return 0


def cmd_acquire(a: argparse.Namespace) -> int:
    try:
        got = _client(a).acquire(a.cid, a.workspace, a.version)
    except sdk.PaymentRequired:
        sys.exit(f"{a.cid!r} is a paid listing you don't own yet: buy it in the web app first, then retry.")
    except sdk.MemoryOnly as e:
        use = e.details.get("use", "")
        sys.exit("This listing is reading material only: there is no session to resume."
                 + (f" Download it with `GET {use}`." if use else ""))
    print(json.dumps({**got, "status": "added to your library (private copy)"}, indent=2))
    _err(f"\nresume it:  {PROG} resume {got['collection']}")
    af = got.get("acquired_from") or {}
    if af.get("cid"):
        _err(f"(added from {af['cid']}@{af.get('version')})")
    _hint(f"after you work in it, `{PROG} push --latest` saves your continuation with its lineage")
    return 0


def cmd_delete(a: argparse.Namespace) -> int:
    r = _client(a).delete(a.name)
    print(json.dumps(r, indent=2))
    if r.get("status") == "already_erased":
        _err(f"{PROG}: already deleted; its stored versions are erased.")
    else:
        hours = r.get("erase_within_hours")
        _err(f"{PROG}: deleted. Stored versions are erased {f'within {hours} hours' if hours else 'shortly'}. "
             "This can't be undone.")
    return 0


# --- resume ----------------------------------------------------------------------------------------

_BYPASS_FLAGS = ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions")


def strip_permission_bypass(args: Sequence[str]):
    """``(kept, dropped)``: ``args`` without any spelling of Claude Code's permission bypass
    (``--dangerously-skip-permissions[=…]``, ``--permission-mode [=]bypassPermissions``)."""
    toks: List[str] = []
    for f in args or []:
        f = str(f)
        try:
            toks.extend(shlex.split(f) if any(c.isspace() for c in f) else [f])
        except ValueError:
            toks.append(f)
    kept, dropped, i = [], [], 0
    while i < len(toks):
        t = toks[i]
        name, eq, val = t.partition("=")
        if name in _BYPASS_FLAGS:
            dropped.append(t)
        elif name == "--permission-mode" and eq:
            (dropped if val.strip().lower() == "bypasspermissions" else kept).append(t)
        elif t == "--permission-mode" and i + 1 < len(toks) and toks[i + 1].strip().lower() == "bypasspermissions":
            dropped.extend([t, toks[i + 1]])
            i += 1
        else:
            kept.append(t)
        i += 1
    return kept, dropped


def _exec_claude(argv: List[str], cwd: str) -> int:
    exe = shutil.which("claude")
    if not exe:
        _err(f"{PROG}: `claude` (Claude Code) is not on PATH. Install it, then run in {cwd}:\n  "
             + " ".join(shlex.quote(x) for x in argv))
        return 0
    _hint(f"when you're done, save it back with `{PROG} push --latest`")
    os.chdir(cwd)
    os.execvp(exe, argv)
    return 0  # pragma: no cover


def cmd_resume(a: argparse.Namespace) -> int:
    cwd = os.path.abspath(os.path.expanduser(a.cwd or os.getcwd()))
    if not os.path.isdir(cwd):
        sys.exit(f"{PROG}: no such directory: {cwd}")
    passthrough = list(a.passthrough or [])
    r = _client(a).resume(a.name, cwd=cwd, compact=a.from_compact)
    f, local = r.fetched, r.local
    if f.acquired_now:
        _err(f"{PROG}: added {a.name} to your library from the marketplace -> {f.cid}")
    if f.from_compact:
        _err(f"{PROG}: resuming the compact tip ({f.resumed}).")
    else:
        cur = token_stats(f.transcript).get("current") or 0
        if cur:
            smaller = f.has_compact or sdk.has_compaction(f.transcript)
            more = "  (--from-compact resumes a smaller tip)" if smaller else ""
            _err(f"{PROG}: base context ~{round(cur / 1000)}k.{more}")
    _err(f"{PROG}: new local session {local.session_id} seeded from {f.cid} (in {cwd})")
    if f.untrusted:
        # Someone else's transcript can carry planted instructions: never run it with permission
        # prompts off unless asked for explicitly.
        _err(f"{PROG}: this session holds content from another author (acquired, or derived from an "
             "acquired session); treat it as untrusted input.")
        if not a.trust_base:
            passthrough, dropped = strip_permission_bypass(passthrough)
            if dropped:
                _err(f"{PROG}: launching WITH permission prompts (dropped: {' '.join(dropped)}). "
                     "Pass --trust-base to keep them.")
    argv = local.resume_command + passthrough
    if a.no_launch:
        print(json.dumps({"session_id": local.session_id, "path": local.path, "cwd": cwd,
                          "collection": f.cid, "command": argv}))
        return 0
    return _exec_claude(argv, cwd)


def cmd_launch(a: argparse.Namespace) -> int:
    """Compatibility with the standalone script: `launch --a4sx-session NAME` = `resume NAME`."""
    if a.session is None:
        return _exec_claude(["claude"] + list(a.passthrough or []), os.getcwd())
    if not a.session:
        sys.exit(f"{PROG} launch {SESSION_FLAG} needs a NAME (see `{PROG} list`)")
    ns = argparse.Namespace(base=a.base, name=a.session, cwd=None, from_compact=a.from_compact,
                            no_launch=False, trust_base=a.trust_base, passthrough=a.passthrough)
    return cmd_resume(ns)


# --- craft / tokens / squash -----------------------------------------------------------------------


def cmd_craft(a: argparse.Namespace) -> int:
    client = _client(a)
    blobs = []
    untrusted_from: List[str] = []  # saved ids of untrusted sources (for the lineage edge)
    untrusted = False
    for src in a.sources:
        p = os.path.expanduser(src)
        if os.path.isfile(p):
            with open(p, "rb") as f:
                blobs.append(f.read())
            side = sdk.read_lineage(sdk.session_id_of(blobs[-1])) or {}
            if side.get("untrusted"):
                untrusted = True
                untrusted_from.append(str(side.get("parent_cid") or ""))
            _err(f"  + file:{src}")
        else:
            got = client.fetch(src)
            blobs.append(got.transcript)
            if got.untrusted:
                untrusted = True
                untrusted_from.append(got.cid)
            _err(f"  + {src} ({got.cid})")
    parent = a.parent
    if untrusted:
        # keep the marker: a server-side parent edge to the untrusted source (visible on every
        # machine) unless --parent names another, plus the local mark
        parent = parent or next((c for c in untrusted_from if c), None)
        _err(f"{PROG} craft: a source holds content from another author; the crafted base is marked "
             "untrusted, so resuming it keeps permission prompts on.")
    if a.no_scrub:
        _err(f"{PROG} craft: WARNING: --no-scrub: the crafted session is uploaded WITHOUT the client-side "
             "secret scrub (the server still scrubs). Local files may contain credentials.")
    res, sid, rep = client.craft(blobs, label=a.label, title=a.title, into=a.into, parent=parent,
                                 scrub=not a.no_scrub, untrusted=untrusted)
    _err(f"{PROG} craft: {res.scrub.summary()}")
    if rep["tool_use"] or rep["tool_result"]:
        _err(f"{PROG} craft: dropped {rep['tool_use']} tool_use + {rep['tool_result']} tool_result block(s) "
             f"that would be rejected on resume ({rep['dup_use']} duplicate-id, "
             f"{rep['orphan_results']} unmatched or misordered result(s))")
    if rep["emptied"]:
        _err(f"{PROG} craft: dropped {rep['emptied']} record(s) left empty after that")
    if rep["boundary"]:
        _err(f"{PROG} craft: dropped {rep['boundary']} stale compaction marker(s)")
    if len(rep["models"]) > 1:
        _err(f"{PROG} craft: heads-up: the sources span {len(rep['models'])} models ({', '.join(rep['models'])})")
    print(json.dumps({**res.response, "crafted_from": a.sources, "internal_sid": sid, "records": rep["records"],
                      "guard": rep, "client_scrub": res.scrub.to_dict(), "untrusted": res.untrusted,
                      "status": "staged (private)"}, indent=2))
    handle = a.label or res.response.get("collection") or ""
    if handle:
        _hint(f"resume the crafted base with `{PROG} resume {handle}`")
    return 0


def window_arg(v: str) -> int:
    """``--window`` is a SIZE IN TOKENS (200000, 200k, 1m), not a model name."""
    raw = v.strip().replace(",", "").replace("_", "")
    mult = 1
    if raw[-1:].lower() == "k":
        mult, raw = 1_000, raw[:-1]
    elif raw[-1:].lower() == "m":
        mult, raw = 1_000_000, raw[:-1]
    try:
        n = int(float(raw) * mult)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{v!r} is not a size. --window takes the context window as a NUMBER OF TOKENS (not a model "
            "name), e.g. --window 200000, --window 200k, --window 1m. Omit it to use the 200k default."
        ) from None
    if n <= 0:
        raise argparse.ArgumentTypeError(f"{v!r} must be a positive number of tokens, e.g. --window 200k.")
    return n


def cmd_tokens(a: argparse.Namespace) -> int:
    window = a.window or 200000
    src = a.session
    if a.latest:
        src = latest_local_session()
        if not src:
            sys.exit("no local Claude Code sessions found")
        _err(f"latest session: {src}")
    if not src:
        sys.exit("give a saved name, a local .jsonl path, or --latest")
    p = os.path.expanduser(src)
    if os.path.isfile(p):
        with open(p, "rb") as f:
            data = f.read()
        origin = f"file:{src}"
    else:
        got = _client(a).fetch(src)
        data, origin = got.transcript, f"{src} ({got.cid})"
    s = token_stats(data)

    def pct(n: int) -> str:
        return f"{100 * n / window:.0f}% of {window // 1000}k"

    print(f"session: {origin}")
    if s["has_usage"]:
        print(f"  current context : {s['current']:>9,}  ({pct(s['current'])})")
        print(f"  peak context    : {s['peak']:>9,}  ({pct(s['peak'])})")
        print(f"  total output    : {s['output']:>9,}   over {s['turns']} turns")
        if s["current"] >= 0.8 * window or s["peak"] >= window:
            print(f"  ⚠ near or over the {window // 1000}k window: run `{PROG} squash {src}` for the recipe.")
    else:
        print(f"  no usage records: rough estimate ~{s['est_tokens']:,} tokens ({s['bytes']:,} bytes, ≈chars/4)")
        if s["est_tokens"] and s["est_tokens"] >= 0.8 * window:
            print(f"  ⚠ estimate near the {window // 1000}k window")
    return 0


def cmd_squash(a: argparse.Namespace) -> int:
    client = _client(a)
    got = client.fetch(a.name, for_resume=True)  # a session of another harness is refused up front
    cid = got.cid
    cur = (got.row.get("tokens") or {}).get("current") or token_stats(got.transcript).get("current") or 0
    leaf = got.save(os.path.join(cfg.cache_dir(), f"squash-src-{re.sub(r'[^A-Za-z0-9_.-]', '-', cid)}.jsonl"))
    pct = f" (~{round(100 * cur / a.window)}% of {a.window // 1000}k)" if (a.window and cur) else ""
    _err(f"{PROG}: {a.name} -> {cid}   current context ~{round(cur / 1000)}k{pct}")
    _err(f"{PROG}: leaf transcript cached at {leaf}")
    print("\nSquash recipe: the compression runs in YOUR Claude Code session (no server-side model):")
    print(f"  1) Resume it:                   {PROG} resume {a.name}")
    print("  2) Run /compact, or ask for a COMPACT hand-off (durable state, open threads, decisions).")
    print(f"  3) Save it as the resume tip:   {PROG} push --latest --into {cid} --compact")
    print(f"  4) Check:                       {PROG} tokens {a.name}")
    print(f"\nAfter step 3, `{PROG} resume {a.name} --from-compact` resumes the compact tip; "
          f"plain `{PROG} resume {a.name}` still resumes the full history.")
    return 0


# --- misc ------------------------------------------------------------------------------------------


def cmd_version(a: argparse.Namespace) -> int:
    line = f"{PROG} {__version__}"
    if not getattr(a, "offline", False):
        try:
            v = _client(a).server_version()
        except ValueError:
            v = None
        if v:
            line += f"  (server {v})"
    print(line)
    return 0


def cmd_complete(a: argparse.Namespace) -> int:
    """Hidden: tab-completion candidates. Fast and silent on any error."""
    try:
        client = sdk.Client(timeout=5)
        if a.kind == "sessions" and client.token:
            for c in client.list_sessions():
                name = c.get("label") or c.get("id")
                if name:
                    print(name)
    except Exception:
        pass
    return 0


_BASH_COMPLETION = r'''# a4sx bash completion.  Load with:  eval "$(a4sx completion bash)"
_a4sx_complete() {
  local cur prev sub
  cur="${COMP_WORDS[COMP_CWORD]}"
  prev="${COMP_WORDS[COMP_CWORD-1]}"
  sub="${COMP_WORDS[1]}"
  case "$prev" in
    --a4sx-session|--parent) COMPREPLY=($(compgen -W "$(a4sx __complete sessions 2>/dev/null)" -- "$cur")); return;;
    --cwd|--output) COMPREPLY=($(compgen -d -- "$cur")); return;;
    --sort) COMPREPLY=($(compgen -W "updated created name relevance imports rating recent" -- "$cur")); return;;
    --label|--title|--into|--window|--provider|--base|--token|--workspace|--version|--tag|--limit|--offset) return;;
  esac
  if [ "$COMP_CWORD" -eq 1 ]; then
    COMPREPLY=($(compgen -W "login logout whoami push list search fetch acquire resume launch delete craft \
tokens squash version completion" -- "$cur"))
    return
  fi
  case "$sub" in
    fetch|delete|squash|resume) COMPREPLY=($(compgen -W "$(a4sx __complete sessions 2>/dev/null)" -- "$cur"));;
    tokens|craft)  COMPREPLY=($(compgen -W "$(a4sx __complete sessions 2>/dev/null)" -- "$cur"))
                   COMPREPLY+=($(compgen -f -- "$cur"));;
    push)          COMPREPLY=($(compgen -f -W "--latest --label --title --into --parent --compact --keep-pii \
--no-scrub --dry-run --fail-on-scrub" -- "$cur"));;
    list)          COMPREPLY=($(compgen -W "--sort --json" -- "$cur"));;
    search)        COMPREPLY=($(compgen -W "--library --tag --sort --limit --offset --json" -- "$cur"));;
    login)         COMPREPLY=($(compgen -W "--provider --base --token --no-browser" -- "$cur"));;
    completion)    COMPREPLY=($(compgen -W "bash" -- "$cur"));;
  esac
}
complete -F _a4sx_complete a4sx
'''


def cmd_completion(a: argparse.Namespace) -> int:
    sys.stdout.write(_BASH_COMPLETION)
    return 0


_EPILOG = f"""\
Quickstart:
  {PROG} login                         # sign in (opens a browser, or prints a link for another device)
  {PROG} push --latest --label my-work # save your most recent Claude Code session (scrubbed first)
  {PROG} list                          # your private library, as a lineage tree
  {PROG} resume my-work                # continue it as a new local session, here or on another machine

Server: --base, else $A4SX_BASE, else the one you signed in to, else {cfg.DEFAULT_BASE_URL}.
Docs: https://{cfg.PUBLIC_HOST}
"""


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--base", default=None, metavar="URL",
                        help=f"server (default: $A4SX_BASE, the server you signed in to, else {cfg.DEFAULT_BASE_URL})")
    ap = argparse.ArgumentParser(
        prog=PROG, formatter_class=argparse.RawDescriptionHelpFormatter, epilog=_EPILOG,
        description="Save, share and resume coding-agent sessions: back Claude Code, Codex CLI and Grok Build "
                    "sessions up to your private library, resume Claude Code sessions on any machine, track "
                    "their lineage, and find published ones.")
    ap.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    ap.add_argument("--base", dest="global_base", default=None, metavar="URL",
                    help="server for any command (same as the per-command --base)")
    sub = ap.add_subparsers(dest="cmd", metavar="<command>")

    def add(name: str, fn, help: str, **kw) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help, parents=[common], **kw)
        p.set_defaults(fn=fn)
        return p

    p = add("login", cmd_login, "sign in (browser device login) and set up your private library")
    p.add_argument("--provider", help="pin a sign-in provider (e.g. google, github; as offered by the server); "
                                      "default: choose in the browser")
    p.add_argument("--token", nargs="?", const="-", default=None, metavar="TOKEN",
                   help="store a session token you already have instead of signing in; `-` (or no value) "
                        "reads it from stdin, which keeps it out of your shell history")
    p.add_argument("--no-browser", action="store_true",
                   help="don't open a browser here; print a short link (and a QR code if `qrencode` is installed)")

    p = add("logout", cmd_logout, "forget the stored session (and end it on the server)")
    p.add_argument("--keep-server-session", action="store_true", help="only delete the local token file")

    add("whoami", cmd_whoami, "show the signed-in account")

    p = add("push", cmd_push, "save a Claude Code, Codex CLI or Grok Build session to your private library")
    p.add_argument("jsonl", nargs="?", help="path to a session .jsonl (Claude Code or Codex CLI), or a Grok Build "
                                            "session folder (its updates.jsonl is pushed); or use --latest")
    p.add_argument("--latest", action="store_true",
                   help="pick the most recently modified local Claude Code session")
    p.add_argument("--label", help="short handle to save it under")
    p.add_argument("--title", help="human-readable title")
    p.add_argument("--into", metavar="ID", help="save as a NEW VERSION of an existing saved session")
    p.add_argument("--parent", metavar="NAME", help="record a lineage parent (name or id)")
    p.add_argument("--compact", action="store_true", help="with --into: mark this version as the compact tip")
    p.add_argument("--keep-pii", action="store_true",
                   help="keep personal data, host names, IP addresses and paths: the client scrub removes secrets, "
                        "command-line passwords and Sheet ids only (the server still scrubs personal data)")
    p.add_argument("--no-scrub", action="store_true",
                   help="skip the client-side scrub entirely (the server still scrubs)")
    p.add_argument("--dry-run", action="store_true",
                   help="show what the client scrub would change and the sha256 that would be uploaded; "
                        "uploads nothing, needs no sign-in")
    p.add_argument("--fail-on-scrub", action="store_true",
                   help=f"exit {FAIL_ON_SCRUB_EXIT} without uploading if the client scrub would change anything")

    p = add("list", cmd_list, "list your saved sessions as a lineage tree")
    p.add_argument("--sort", choices=LIST_SORTS, default="updated",
                   help="updated (default, last push first) | created (newest first) | name (A-Z)")
    p.add_argument("--json", action="store_true", help="print the raw rows as JSON")

    p = add("search", cmd_search, "search published sessions (or your library with --library)")
    p.add_argument("query", nargs="*", help="search words (empty = browse)")
    p.add_argument("--library", action="store_true", help="search your own saved sessions instead")
    p.add_argument("--tag", action="append", help="only items with this tag (repeatable)")
    p.add_argument("--sort", choices=("relevance", "imports", "rating", "recent"), default="relevance")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--json", action="store_true", help="print the raw result as JSON")

    p = add("fetch", cmd_fetch, "download a saved session's transcript (no resume)")
    p.add_argument("name", help="saved name, label or id (or an entitled marketplace id)")
    p.add_argument("--session", help="a specific member session id in the collection")
    p.add_argument("--from-compact", action="store_true",
                   help="the compact tip, or the context after the last /compact")
    p.add_argument("-o", "--output", metavar="PATH", help="write here instead of the cache dir")

    p = add("acquire", cmd_acquire, "add a marketplace session you're entitled to to your library")
    p.add_argument("cid", help="the listing id (from `a4sx search` or the web app)")
    p.add_argument("--workspace", "--tenant", dest="workspace", help="the listing's workspace (usually not needed)")
    p.add_argument("--version", help="a specific version (default: latest)")

    p = add("resume", cmd_resume, "continue a saved session as a NEW local Claude Code session")
    p.add_argument("name", help="saved name, label or id (or an entitled marketplace id)")
    p.add_argument("--cwd", metavar="DIR", help="directory to work in (default: the current one)")
    p.add_argument("--from-compact", action="store_true",
                   help="resume smaller: the compact tip, or the context after the last /compact")
    p.add_argument("--no-launch", action="store_true",
                   help="write the session and print it as JSON; don't start claude")
    p.add_argument("--trust-base", action="store_true",
                   help="keep permission-bypass flags even for a session from another author")
    p.add_argument("passthrough", nargs="*", help="arguments for claude, after --")

    p = add("launch", cmd_launch, f"start claude; with {SESSION_FLAG} NAME the same as `resume NAME`")
    p.add_argument(SESSION_FLAG, dest="session", nargs="?", const="", default=None, metavar="NAME")
    p.add_argument("--from-compact", action="store_true")
    p.add_argument("--trust-base", action="store_true")
    p.add_argument("passthrough", nargs="*", help="arguments for claude, after --")

    p = add("delete", cmd_delete, "PERMANENTLY delete a saved session (every stored version is erased)")
    p.add_argument("name", help="saved name, label or id")

    p = add("craft", cmd_craft, "compile ONE resumable base session from several sources")
    p.add_argument("sources", nargs="+", help="saved names or local .jsonl paths, merged in this order")
    p.add_argument("--label")
    p.add_argument("--title")
    p.add_argument("--into", metavar="ID")
    p.add_argument("--parent", metavar="NAME")
    p.add_argument("--no-scrub", action="store_true",
                   help="DANGEROUS: upload without the client-side scrub (the server still scrubs)")

    p = add("tokens", cmd_tokens, "estimate a session's token footprint against a context window")
    p.add_argument("session", nargs="?", help="saved name, or a local .jsonl path")
    p.add_argument("--latest", action="store_true", help="the most recently modified local session")
    p.add_argument("--window", type=window_arg, default=200000, metavar="TOKENS",
                   help="context window SIZE IN TOKENS, e.g. 200000, 200k, 1m (not a model name; default 200k)")

    p = add("squash", cmd_squash, "recipe to compact a long session into a fresh, resumable tip")
    p.add_argument("name")
    p.add_argument("--window", type=window_arg, default=200000, metavar="TOKENS")

    p = add("version", cmd_version, "print the client and server version")
    p.add_argument("--offline", action="store_true", help="don't ask the server")

    p = add("completion", cmd_completion, 'print the bash completion script: eval "$(a4sx completion bash)"')
    p.add_argument("shell", nargs="?", default="bash", choices=["bash"])

    p = sub.add_parser("__complete")
    p.add_argument("kind", choices=["sessions"])
    p.set_defaults(fn=cmd_complete)
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    extra: List[str] = []
    if "--" in argv:  # everything after `--` is for claude (argparse mishandles `--` with subcommands)
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    a = ap.parse_args(argv)
    if getattr(a, "base", None) is None and getattr(a, "global_base", None):
        a.base = a.global_base
    if extra:
        if not hasattr(a, "passthrough"):
            ap.error("arguments after -- are only accepted by resume and launch")
        a.passthrough = list(a.passthrough or []) + extra
    if not getattr(a, "cmd", None):
        ap.print_help()
        return 2
    try:
        return int(a.fn(a) or 0)
    except sdk.NotLoggedIn as e:
        _err(f"{PROG}: {e}. Or `{PROG} login --token -` to store a token you already have, or export "
             "A4SX_SESSION for this shell (it applies to https://session-exchange.com, or to $A4SX_SESSION_BASE).")
        return 1
    except sdk.ApiError as e:
        hint = (f": your session has expired or is no longer valid; run `{PROG} login`"
                if e.session_ended else "")
        _err(f"{PROG}: {e}{hint}")
        return 1
    except (sdk.A4sxError, ValueError, OSError) as e:
        _err(f"{PROG}: {e}")
        return 1
    except KeyboardInterrupt:
        _err("cancelled")
        return 130
