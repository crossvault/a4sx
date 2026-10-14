# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 crossVault GmbH
"""The client-side pre-scrub that runs before a transcript leaves your machine.

It uses the `redact-transcripts <https://pypi.org/project/redact-transcripts/>`_ engine with its
``secret.*`` rules (API keys, tokens, bearer and basic auth, URL credentials, private keys,
secret-named assignments, ...) plus one rule for a4sx session tokens. Personal-data rules
(e-mail, IP addresses, home directories) are **off** here, because they would rewrite paths a
resumed session relies on; run ``redact-transcripts`` yourself if you want them.

JSON Lines are redacted after decoding, so escaping cannot hide a secret, and every line stays
valid JSON with its ids and links intact. Lines with nothing to redact keep their exact bytes.

This is a backstop, not a guarantee: the server scrubs again on upload, and neither understands
meaning. Review what you push.

A report never contains a matched value: only rule names, counts and line numbers.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Tuple

from redact_transcripts import Redactor, Rule, default_rules, redact_bytes

#: a4sx session tokens (``a4sx login --token``) are not in the generic rule set.
# This rule matches a4sx session tokens: `ge_sess_` is the prefix of the tokens the a4sx service issues.
SESSION_TOKEN_RULE = Rule("secret.a4sx_session", re.compile(r"\bge_sess_[A-Za-z0-9_\-]{6,}"))

#: Rule-name prefixes that are switched off for the pre-scrub (see the module docstring).
DISABLED = ("pii", "infra")


def redactor() -> Redactor:
    """The redactor the pre-scrub uses."""
    return Redactor(rules=[SESSION_TOKEN_RULE] + list(default_rules()), disable=DISABLED)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class ScrubReport:
    """What the pre-scrub changed. Never holds a matched value."""

    enabled: bool
    lines_changed: int = 0
    rules: Dict[str, int] = field(default_factory=dict)
    lines: List[int] = field(default_factory=list)
    local_sha256: str = ""
    uploaded_sha256: str = ""

    @property
    def identical(self) -> bool:
        return self.local_sha256 == self.uploaded_sha256

    def to_dict(self) -> dict:
        d = asdict(self)
        d["identical"] = self.identical
        return d

    def summary(self) -> str:
        """One line, e.g. ``client scrub: 2 lines changed (secret.bearer: 2) — lines 1, 3``."""
        if not self.enabled:
            return "client scrub: skipped (--no-scrub)"
        if not self.lines:
            return "client scrub: 0 lines changed"
        n = len(self.lines)
        rules = ", ".join(f"{k}: {v}" for k, v in sorted(self.rules.items()))
        shown = ", ".join(map(str, self.lines[:20])) + (f", … (+{n - 20} more)" if n > 20 else "")
        s = "s" if n != 1 else ""
        return f"client scrub: {n} line{s} changed ({rules}) — line{s} {shown}"


def scrub_bytes(data: bytes, enabled: bool = True) -> Tuple[bytes, ScrubReport]:
    """Pre-scrub ``data`` (a transcript, JSON Lines or text). Returns ``(upload_bytes, report)``.

    With ``enabled=False`` the bytes pass through unchanged and the report says so.
    """
    local = sha256(data)
    if not enabled:
        return data, ScrubReport(enabled=False, local_sha256=local, uploaded_sha256=local)
    out, rep = redact_bytes(data, "auto", redactor())
    lines = sorted(set(rep.changed_lines))
    return out, ScrubReport(
        enabled=True,
        lines_changed=len(lines),
        rules=dict(sorted(rep.rules.items())),
        lines=lines,
        local_sha256=local,
        uploaded_sha256=sha256(out),
    )


def scrub_text(text: str) -> Tuple[str, Dict[str, int]]:
    """Pre-scrub one string. Returns ``(scrubbed, per-rule counts)``."""
    counts: Dict[str, int] = {}
    return redactor().redact_text(text, counts), {k: v for k, v in counts.items() if v}
