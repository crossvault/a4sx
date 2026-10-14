# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 crossVault GmbH
"""Local Claude Code transcript handling. No network.

Claude Code stores each session as JSON Lines under
``${CLAUDE_CONFIG_DIR:-~/.claude}/projects/<project-dir>/<session-id>.jsonl``, where
``<project-dir>`` is the working directory with every character that is not a letter or a digit
replaced by ``-``. Every record carries the ``sessionId``; records link to each other with
``uuid``/``parentUuid``.

Resuming a saved transcript as a new local session takes four steps (``resume_locally``):

1. decode the transcript,
2. pick a fresh session id,
3. set ``sessionId`` on every record that has one (the ``uuid`` chain is untouched, so it stays
   consistent within the file),
4. write it to the project dir of the directory you will work in, then ``claude --resume <id>``.
"""

from __future__ import annotations

import glob
import json
import os
import re
import uuid
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple, Union

BytesOrStr = Union[bytes, str]


def _text(data: BytesOrStr) -> str:
    return data.decode("utf-8", "replace") if isinstance(data, (bytes, bytearray)) else data


def claude_projects_dir(claude_config_dir: Optional[str] = None) -> str:
    """Claude Code's ``projects`` directory; honours ``$CLAUDE_CONFIG_DIR`` like Claude Code does."""
    root = claude_config_dir or os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return os.path.join(root, "projects")


def project_dir_name(cwd: str) -> str:
    """Claude Code's folder name for a working directory: every non-alphanumeric character becomes
    ``-`` (``/a/b.c_d`` -> ``-a-b-c-d``). A mismatch makes a resumed session invisible."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def local_sessions(claude_config_dir: Optional[str] = None) -> List[str]:
    """All local Claude Code transcripts, newest first."""
    files = glob.glob(os.path.join(claude_projects_dir(claude_config_dir), "*", "*.jsonl"))
    return sorted(files, key=os.path.getmtime, reverse=True)


def latest_local_session(claude_config_dir: Optional[str] = None) -> Optional[str]:
    """The most recently modified local transcript, or None."""
    files = local_sessions(claude_config_dir)
    return files[0] if files else None


def iter_records(data: BytesOrStr) -> Iterable[dict]:
    """Parsed JSON objects, one per non-blank line; unparseable lines are skipped."""
    for ln in _text(data).splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if isinstance(rec, dict):
            yield rec


def session_id_of(data: BytesOrStr) -> Optional[str]:
    """The transcript's own ``sessionId`` (first record that has one)."""
    for rec in iter_records(data):
        sid = rec.get("sessionId")
        if sid:
            return str(sid)
    return None


def recorded_cwd_of(data: BytesOrStr) -> Optional[str]:
    for rec in iter_records(data):
        if rec.get("cwd"):
            return str(rec["cwd"])
    return None


def summarize(data: BytesOrStr) -> dict:
    """A small, content-light summary: ids, counts, time span and the opening user prompt."""
    rows = list(iter_records(data))
    ts = [r.get("timestamp") for r in rows if r.get("timestamp")]
    first_user = ""
    for r in rows:
        if r.get("type") != "user":
            continue
        c = (r.get("message") or {}).get("content")
        if isinstance(c, str):
            first_user = c
        elif isinstance(c, list):
            first_user = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
        if first_user.strip():
            break
    return {
        "session_id": next((r.get("sessionId") for r in rows if r.get("sessionId")), None),
        "cwd": next((r.get("cwd") for r in rows if r.get("cwd")), None),
        "records": len(rows),
        "user_turns": sum(1 for r in rows if r.get("type") == "user"),
        "assistant_turns": sum(1 for r in rows if r.get("type") == "assistant"),
        "first_ts": ts[0] if ts else None,
        "last_ts": ts[-1] if ts else None,
        "first_user": first_user.strip(),
    }


def rekey(data: BytesOrStr, new_session_id: str) -> bytes:
    """Return the transcript with ``sessionId`` set to ``new_session_id`` on every record that has
    one. Blank and unparseable lines are dropped. Nothing else changes."""
    out = []
    for rec in iter_records(data):
        if "sessionId" in rec:
            rec["sessionId"] = new_session_id
        out.append(json.dumps(rec) + "\n")
    return "".join(out).encode("utf-8")


@dataclass(frozen=True)
class LocalSession:
    """A transcript written where Claude Code finds it."""

    session_id: str
    path: str
    cwd: str
    base_session_id: Optional[str] = None

    @property
    def resume_command(self) -> List[str]:
        return ["claude", "--resume", self.session_id]


def resume_locally(
    data: BytesOrStr,
    cwd: Optional[str] = None,
    session_id: Optional[str] = None,
    claude_config_dir: Optional[str] = None,
) -> LocalSession:
    """Write a saved transcript as a NEW local Claude Code session and return where it went.

    ``cwd`` is the directory you will work in (default: the current one), ``session_id`` the new
    id (default: a fresh UUID4). The file is written under a hidden temporary name first and
    renamed into place, so a failure never leaves a half-written session behind. Then run
    ``claude --resume <session_id>`` in ``cwd``.
    """
    base_sid = session_id_of(data)
    if not base_sid:
        raise ValueError("the transcript has no sessionId: it is not a resumable Claude Code session")
    cwd = os.path.abspath(cwd or os.getcwd())
    new_sid = session_id or str(uuid.uuid4())
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", new_sid):
        raise ValueError(f"invalid session id: {new_sid!r}")
    projects = claude_projects_dir(claude_config_dir)
    proj = os.path.join(projects, project_dir_name(cwd))
    for d in (os.path.dirname(projects), projects, proj):  # dirs we create are 0700; existing ones are kept
        os.makedirs(d, mode=0o700, exist_ok=True)
    dest = os.path.join(proj, f"{new_sid}.jsonl")
    if os.path.lexists(dest):
        raise FileExistsError(dest)
    tmp = os.path.join(proj, f".{new_sid}.part")
    # 0600 from the start, never through a symlink, never over an existing file: the content is a
    # whole session, possibly someone else's.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(rekey(data, new_sid))
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return LocalSession(session_id=new_sid, path=dest, cwd=cwd, base_session_id=base_sid)


# --- token footprint -------------------------------------------------------------------------------


def token_stats(data: BytesOrStr) -> dict:
    """Token footprint from the per-turn ``usage`` records.

    ``current`` is the last turn's prompt size (input + cache read + cache creation), roughly what a
    resume costs; ``peak`` is the largest any turn reached. Without usage records (an old or crafted
    transcript) ``est_tokens`` is a rough bytes/4 estimate.
    """
    text = _text(data)
    turns = peak = last = out = 0
    has = False
    for r in iter_records(text):
        u = (r.get("message") or {}).get("usage") or r.get("usage") or {}
        if not isinstance(u, dict) or not u:
            continue
        has = True
        turns += 1
        ctx = (
            int(u.get("input_tokens") or 0)
            + int(u.get("cache_read_input_tokens") or 0)
            + int(u.get("cache_creation_input_tokens") or 0)
        )
        peak = max(peak, ctx)
        if ctx:
            last = ctx
        out += int(u.get("output_tokens") or 0)
    nbytes = len(text.encode("utf-8"))
    return {
        "turns": turns,
        "current": last,
        "peak": peak,
        "output": out,
        "has_usage": has,
        "est_tokens": None if has else nbytes // 4,
        "bytes": nbytes,
    }


# --- compaction ------------------------------------------------------------------------------------


def _is_compact_marker(r: dict) -> bool:
    return (
        r.get("isCompactSummary") is True
        or r.get("subtype") == "compact_boundary"
        or (r.get("message") or {}).get("isCompactSummary") is True
    )


def trim_to_last_compact(data: BytesOrStr) -> Optional[Tuple[bytes, int]]:
    """Keep only the context after Claude Code's last ``/compact``: the boundary or summary record
    and everything after it, with the first kept record made the root. Returns ``(transcript,
    kept_records)``, or None if the transcript was never compacted."""
    lines = [ln for ln in _text(data).splitlines() if ln.strip()]
    last_boundary = last_summary = -1
    for i, ln in enumerate(lines):
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(r, dict):
            continue
        if r.get("subtype") == "compact_boundary":
            last_boundary = i
        if r.get("isCompactSummary") is True or (r.get("message") or {}).get("isCompactSummary") is True:
            last_summary = i
    found = [i for i in (last_boundary, last_summary) if i >= 0]
    if not found:
        return None
    kept = lines[min(found):]  # the boundary and its summary are both kept
    try:
        r0 = json.loads(kept[0])
        if isinstance(r0, dict) and "parentUuid" in r0:
            r0["parentUuid"] = None
            kept[0] = json.dumps(r0)
    except ValueError:
        pass
    return ("\n".join(kept) + "\n").encode("utf-8"), len(kept)


def has_compaction(data: BytesOrStr) -> bool:
    return any(_is_compact_marker(r) for r in iter_records(data))


# --- crafting one base from several transcripts ----------------------------------------------------


def craft_guard(records: List[dict]) -> Tuple[List[dict], dict]:
    """Make a concatenation of transcripts resumable.

    The Messages API rejects a conversation unless, in order, every ``tool_use`` id is unique and is
    answered by exactly one later ``tool_result``. Concatenating sources breaks that in three ways:
    a result whose use was dropped, duplicate ids across sources, and a result that lands before its
    use. So: drop stale compaction markers first, then walk in order, keep the first use of each id,
    close it with the first later matching result, and drop every other use or result. Records left
    empty are dropped. Returns ``(records, report)``.
    """

    def blocks(r: dict):
        c = (r.get("message") or {}).get("content")
        return c if isinstance(c, list) else None

    rep = {"tool_use": 0, "tool_result": 0, "boundary": 0, "emptied": 0, "dup_use": 0, "orphan_results": 0}
    models = set()
    survivors = []
    for r in records:
        if _is_compact_marker(r):
            rep["boundary"] += 1
        else:
            survivors.append(r)
    drop_use, drop_res = set(), set()
    open_uses = {}
    seen = set()
    for ri, r in enumerate(survivors):
        m = (r.get("message") or {}).get("model")
        if m:
            models.add(m)
        for bi, b in enumerate(blocks(r) or []):
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                uid = b.get("id")
                if not uid:
                    drop_use.add((ri, bi))
                elif uid in seen:
                    drop_use.add((ri, bi))
                    rep["dup_use"] += 1
                else:
                    seen.add(uid)
                    open_uses[uid] = (ri, bi)
            elif b.get("type") == "tool_result":
                rid = b.get("tool_use_id")
                if rid and rid in open_uses:
                    del open_uses[rid]
                else:
                    drop_res.add((ri, bi))
                    rep["orphan_results"] += 1
    drop_use.update(open_uses.values())
    rep["tool_use"], rep["tool_result"] = len(drop_use), len(drop_res)
    out = []
    for ri, r in enumerate(survivors):
        bl = blocks(r)
        if bl is None:
            out.append(r)
            continue
        kept = [b for bi, b in enumerate(bl) if (ri, bi) not in drop_use and (ri, bi) not in drop_res]
        if len(kept) == len(bl):
            out.append(r)
            continue
        if bl and not kept:
            rep["emptied"] += 1
            continue
        r["message"]["content"] = kept
        out.append(r)
    rep["models"] = sorted(models)
    return out, rep


def stitch(sources: Iterable[BytesOrStr], session_id: Optional[str] = None) -> Tuple[bytes, str, dict]:
    """Concatenate transcripts into ONE resumable session: repaired with :func:`craft_guard`, then
    re-threaded linearly under one fresh ``sessionId`` (fresh ``uuid`` per record, each
    ``parentUuid`` pointing at the previous record). Returns ``(transcript, session_id, report)``."""
    records: List[dict] = []
    for src in sources:
        records.extend(iter_records(src))
    if not records:
        raise ValueError("no parseable records in the given sources")
    records, rep = craft_guard(records)
    if not records:
        raise ValueError("nothing left after dropping orphaned and compaction records")
    sid = session_id or str(uuid.uuid4())
    last = None
    for rec in records:
        if "sessionId" in rec:
            rec["sessionId"] = sid
        if "uuid" in rec:
            nu = str(uuid.uuid4())
            if "parentUuid" in rec:
                rec["parentUuid"] = last
            rec["uuid"] = nu
            last = nu
    rep["records"] = len(records)
    return "".join(json.dumps(r) + "\n" for r in records).encode("utf-8"), sid, rep
