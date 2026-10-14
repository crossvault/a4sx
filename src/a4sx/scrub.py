# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 crossVault GmbH
"""The client-side scrub that runs before a transcript leaves your machine.

It applies exactly the rules of the a4sx CLI (vendored in :mod:`a4sx._cli_scrub` by
``tools/vendor_scrub.py``, with parity vectors in the test suite), which are the a4sx server's rules:

- ``full`` (the default): secrets (API keys and tokens, bearer, basic and digest auth, credentials in
  URLs, private keys, AWS signatures, JWTs, secret-named assignments), command-line passwords, Google
  Sheet ids, and personal data, hosts, IP and MAC addresses, home-directory user names and key-file paths;
- ``secrets-only`` (``push --keep-pii``): everything except the personal-data, host, IP, MAC, home-directory
  and key-file rules;
- ``none`` (``push --no-scrub``): nothing.

Each matched value is replaced with a fixed marker (``[REDACTED]``, ``[REDACTED-EMAIL]``,
``[REDACTED-USER]`` ...) that keeps JSON string values valid. Lines with nothing to redact keep their exact
bytes. The server scrubs every upload again. Both are pattern-based backstops, not a guarantee: review what
you push.

A report never contains a matched value: only rule names, counts, line and column numbers and lengths.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Tuple

from . import _cli_scrub

# The a4sx token rules (`session_token`, `polaris_tok` and the `ge_…_` token shapes in _cli_scrub) match the
# tokens the a4sx service issues: `ge_sess_`, `ge_tok_`, `pt_` ... are the service's own token prefixes.

#: The scrub modes, as the CLI names them (and sends with an upload as ``client_scrub``).
SCRUB_MODES = ("full", "secrets-only", "none")
#: The rules ``secrets-only`` (``--keep-pii``) leaves out.
PII_RULES = frozenset(_cli_scrub._PII)
#: The a4sx CLI release the rules were vendored from.
RULES_VERSION = _cli_scrub.CLI_VERSION


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class ScrubReport:
    """What the scrub changed. Never holds a matched value."""

    enabled: bool
    mode: str = "full"
    lines_changed: int = 0
    rules: Dict[str, int] = field(default_factory=dict)
    lines: List[int] = field(default_factory=list)
    hits: List[dict] = field(default_factory=list)  # {line, col, rule, chars[, to_line]}: positions only
    local_sha256: str = ""
    uploaded_sha256: str = ""

    @property
    def identical(self) -> bool:
        return self.local_sha256 == self.uploaded_sha256

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("hits")
        d["identical"] = self.identical
        return d

    def summary(self) -> str:
        """One line, e.g. ``client scrub: 2 lines changed (bearer: 2) — lines 1, 3``."""
        if not self.enabled:
            return "client scrub: skipped (--no-scrub)"
        line = _cli_scrub._scrub_summary(self.rules, self.hits)
        if self.mode == "secrets-only":
            line += " [--keep-pii: personal data, hosts, IPs and paths kept]"
        return line


def _mode(enabled: bool, mode: str) -> str:
    if mode not in SCRUB_MODES:
        raise ValueError(f"unknown scrub mode {mode!r} (one of {', '.join(SCRUB_MODES)})")
    return mode if enabled else "none"


def scrub_bytes(data: bytes, enabled: bool = True, mode: str = "full") -> Tuple[bytes, ScrubReport]:
    """Scrub ``data`` (a transcript, JSON Lines or text). Returns ``(upload_bytes, report)``.

    ``mode`` is ``full`` (default), ``secrets-only`` or ``none``; ``enabled=False`` is ``none``. With
    ``none`` the bytes pass through unchanged and the report says so. Invalid UTF-8 is replaced (U+FFFD),
    as the CLI does; the report's two hashes then differ even when no rule fired."""
    mode = _mode(enabled, mode)
    local = sha256(data)
    if mode == "none":
        return data, ScrubReport(enabled=False, mode="none", local_sha256=local, uploaded_sha256=local)
    text, counts, hits = _cli_scrub.scrub_detail(data.decode("utf-8", "replace"), mode)
    out = text.encode("utf-8")
    lines = sorted({h["line"] for h in hits})
    return out, ScrubReport(
        enabled=True,
        mode=mode,
        lines_changed=len(lines),
        rules=dict(sorted(counts.items())),
        lines=lines,
        hits=[dict(h) for h in hits],
        local_sha256=local,
        uploaded_sha256=sha256(out),
    )


def scrub_text(text: str, mode: str = "full") -> Tuple[str, Dict[str, int]]:
    """Scrub one string. Returns ``(scrubbed, per-rule counts)``."""
    if _mode(True, mode) == "none":
        return text, {}
    out, counts = _cli_scrub.scrub(text, mode)
    return out, {k: v for k, v in counts.items() if v}
