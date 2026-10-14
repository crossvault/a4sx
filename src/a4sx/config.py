# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 crossVault GmbH
"""Where a4sx keeps its local state, and how the server address is chosen.

Config directory (first that applies):

1. ``$A4SX_CONFIG_DIR``
2. Windows: ``%LOCALAPPDATA%\\a4sx`` (not roamed to file servers), else ``%APPDATA%\\a4sx``
3. ``$XDG_CONFIG_HOME/a4sx``, else ``~/.config/a4sx`` (Linux and macOS; the same place the
   standalone ``a4sx`` script has always used, so an existing sign-in keeps working)

Files in it:

- ``credentials.json``: one session token per server, mode 0600, never printed; a token is only
  ever sent to the server that issued it
- ``config.json``: the server you signed in to last (the default for later commands)
- ``lineage/<session-id>.json``: which saved session a local session was resumed from
- ``cache/``: fetched transcripts

Nothing here is ever written by importing the module.
"""

from __future__ import annotations

import ipaddress
import json
import os
import sys
from typing import Optional
from urllib.parse import urlsplit

APP = "a4sx"
ENV_PREFIX = "A4SX_"
PUBLIC_HOST = "session-exchange.com"
DEFAULT_BASE_URL = f"https://{PUBLIC_HOST}"

_LOOPBACK_NAMES = ("localhost",)


def env(name: str) -> str:
    """``$A4SX_<name>``, stripped; empty string if unset."""
    return (os.environ.get(ENV_PREFIX + name) or "").strip()


def config_dir() -> str:
    """The OS-appropriate config directory (evaluated at call time, never created here)."""
    override = env("CONFIG_DIR")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    if sys.platform.startswith("win"):
        appdata = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if appdata:
            return os.path.join(appdata, APP)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg and os.path.isabs(xdg):
        return os.path.join(xdg, APP)
    return os.path.join(os.path.expanduser("~"), ".config", APP)


def session_file() -> str:
    return os.path.join(config_dir(), "session")


def settings_file() -> str:
    return os.path.join(config_dir(), "config.json")


def cache_dir() -> str:
    return os.path.join(config_dir(), "cache")


def lineage_dir() -> str:
    return os.path.join(config_dir(), "lineage")


# --- private files ---------------------------------------------------------------------------------


def _ensure_private_dir(path: str) -> None:
    os.makedirs(path, mode=0o700, exist_ok=True)
    if os.name == "posix":
        try:
            if os.stat(path).st_mode & 0o077:  # an existing dir from an older install
                os.chmod(path, 0o700)
        except OSError:
            pass


def write_private(path: str, data: str) -> None:
    """Atomically write ``data`` to ``path`` with mode 0600 (created that way, never widened).

    The file is created by ``os.open(..., 0o600)`` under a temporary name and renamed into place,
    so there is no window in which another user can read it. On Windows the mode is advisory;
    the file lives under the per-user ``%APPDATA%``.
    """
    _ensure_private_dir(os.path.dirname(path))
    tmp = f"{path}.tmp-{os.getpid()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        try:
            os.chmod(tmp, 0o600)  # in case a umask or an existing file left it wider
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- session tokens, each bound to the server that issued it ---------------------------------------
#
# A token is stored per origin (``credentials.json``: {"https://host[:port]": token}) and is only ever
# returned for that same origin. Pointing a4sx at another server (``--base``, ``$A4SX_BASE``) therefore
# never sends it a token issued by a different one: that server needs its own ``a4sx login``.


def credentials_file() -> str:
    return os.path.join(config_dir(), "credentials.json")


def _tighten(path: str) -> None:
    if os.name == "posix":
        try:
            if os.stat(path).st_mode & 0o077:
                os.chmod(path, 0o600)
        except FileNotFoundError:
            pass


def _load_credentials() -> dict:
    path = credentials_file()
    _tighten(path)
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(d, dict):
        return {}
    return {k: v for k, v in d.items() if isinstance(k, str) and isinstance(v, str) and v}


def _legacy_origin() -> str:
    """The server the standalone script's ``session`` file belongs to (its ``amdx.json``), else the
    public server."""
    try:
        with open(os.path.join(config_dir(), "amdx.json"), encoding="utf-8") as f:
            base = json.load(f).get("base")
        return normalize_base_url(base) if isinstance(base, str) and base.strip() else DEFAULT_BASE_URL
    except (OSError, ValueError, AttributeError):
        return DEFAULT_BASE_URL


def _check_token(token: str) -> str:
    token = (token or "").strip()
    if not token:
        raise ValueError("empty token")
    if any(c.isspace() for c in token):
        raise ValueError("a session token contains no whitespace")
    return token


def save_token(token: str, origin: str = DEFAULT_BASE_URL) -> str:
    """Store the session token for ``origin`` (mode 0600). Returns the file path. Never logged."""
    token = _check_token(token)
    origin = normalize_base_url(origin)
    creds = _load_credentials()
    creds[origin] = token
    path = credentials_file()
    write_private(path, json.dumps(creds, indent=2) + "\n")
    return path


def env_token_origin() -> str:
    """The server ``$A4SX_SESSION`` belongs to: ``$A4SX_SESSION_BASE``, else the public server.
    Deliberately NOT ``$A4SX_BASE``: an injected base must not receive a token from the environment."""
    return normalize_base_url(env("SESSION_BASE") or DEFAULT_BASE_URL)


def load_token(origin: str = DEFAULT_BASE_URL) -> Optional[str]:
    """The session token for ``origin``, or None. Never a token issued for another server.

    Order: ``$A4SX_SESSION`` (only if ``origin`` is :func:`env_token_origin`), the stored token for
    ``origin``, the standalone script's ``session`` file (only for the server it was issued by).
    Stored files readable by group or others are tightened back to 0600 on read.
    """
    origin = normalize_base_url(origin)
    tok = env("SESSION")
    if tok and origin == env_token_origin():
        return tok
    tok = _load_credentials().get(origin)
    if tok:
        return tok
    if origin == _legacy_origin():
        path = session_file()
        _tighten(path)
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().strip() or None
        except FileNotFoundError:
            return None
    return None


def signed_in_origins() -> list:
    """Servers a token is stored for (never the tokens)."""
    out = sorted(_load_credentials())
    if os.path.exists(session_file()) and _legacy_origin() not in out:
        out.append(_legacy_origin())
    return out


def clear_token(origin: str = DEFAULT_BASE_URL) -> bool:
    """Forget the stored token for ``origin``. True if one was removed."""
    origin = normalize_base_url(origin)
    removed = False
    creds = _load_credentials()
    if origin in creds:
        del creds[origin]
        write_private(credentials_file(), json.dumps(creds, indent=2) + "\n")
        removed = True
    if origin == _legacy_origin():
        try:
            os.unlink(session_file())
            removed = True
        except FileNotFoundError:
            pass
    return removed


# --- the server address ----------------------------------------------------------------------------


def _is_loopback(host: str) -> bool:
    h = (host or "").strip("[]").lower()
    if h in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def normalize_base_url(value: Optional[str]) -> str:
    """Turn ``session-exchange.com``, ``https://host/``, ``localhost:18080`` ... into an origin.

    - No scheme: ``https``, except a loopback host (``localhost``, ``127.0.0.1``, ``::1``),
      which gets ``http`` (a local test server).
    - ``http://`` is accepted **only** for a loopback host: a session token must never travel in
      clear text to a real server. Anything else raises ``ValueError``.
    - A path, query or trailing slash is dropped.
    """
    raw = (value or "").strip() or DEFAULT_BASE_URL
    if "://" not in raw:
        host_part = raw.split("/", 1)[0]
        probe = urlsplit("//" + host_part)
        raw = ("http://" if _is_loopback(probe.hostname or "") else "https://") + raw
    u = urlsplit(raw)
    scheme = u.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"unsupported scheme in base URL: {scheme!r}")
    host = u.hostname or ""
    if not host:
        raise ValueError(f"no host in base URL: {value!r}")
    if scheme == "http" and not _is_loopback(host):
        raise ValueError(f"refusing plain http to {host}: use https (http is allowed for localhost only)")
    try:
        port = u.port
    except ValueError:
        raise ValueError(f"bad port in base URL: {value!r}") from None
    netloc = f"[{host}]" if ":" in host else host
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        netloc += f":{port}"
    return f"{scheme}://{netloc}"


def load_settings() -> dict:
    """The stored settings (``config.json``), or ``{}``."""
    for name in ("config.json", "amdx.json"):  # amdx.json: written by the standalone script
        try:
            with open(os.path.join(config_dir(), name), encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                return d
        except (OSError, ValueError):
            continue
    return {}


def save_settings(d: dict) -> None:
    write_private(settings_file(), json.dumps(d, indent=2) + "\n")


def resolve_base_url(explicit: Optional[str] = None) -> str:
    """The server to talk to: ``explicit`` (a flag), else ``$A4SX_BASE``, else the server you
    signed in to, else ``https://session-exchange.com``."""
    for cand in (explicit, env("BASE"), load_settings().get("base")):
        if isinstance(cand, str) and cand.strip():
            return normalize_base_url(cand)
    return DEFAULT_BASE_URL
