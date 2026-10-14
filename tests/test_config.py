# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 crossVault GmbH
import os
import stat
import sys

import pytest

from a4sx import config


@pytest.mark.parametrize("value,want", [
    (None, "https://session-exchange.com"),
    ("", "https://session-exchange.com"),
    ("session-exchange.com", "https://session-exchange.com"),
    ("https://session-exchange.com/", "https://session-exchange.com"),
    ("https://session-exchange.com/some/path?x=1", "https://session-exchange.com"),
    ("example.org:8443", "https://example.org:8443"),
    ("https://example.org:443", "https://example.org"),
    ("localhost:18080", "http://localhost:18080"),
    ("127.0.0.1:18080", "http://127.0.0.1:18080"),
    ("[::1]:8080", "http://[::1]:8080"),
    ("http://localhost:18080", "http://localhost:18080"),
    ("https://localhost:18080", "https://localhost:18080"),
    ("localhost.example.com", "https://localhost.example.com"),
    ("203.0.113.5:8080", "https://203.0.113.5:8080"),
])
def test_normalize_base_url(value, want):
    assert config.normalize_base_url(value) == want


@pytest.mark.parametrize("bad", ["http://example.org", "http://203.0.113.5:8080", "http://localhost.example.com",
                                 "ftp://example.org", "https://"])
def test_plain_http_and_odd_schemes_are_refused(bad):
    with pytest.raises(ValueError):
        config.normalize_base_url(bad)


def test_base_precedence_flag_env_stored_default(monkeypatch):
    assert config.resolve_base_url() == config.DEFAULT_BASE_URL
    config.save_settings({"base": "stored.example.org"})
    assert config.resolve_base_url() == "https://stored.example.org"
    monkeypatch.setenv("A4SX_BASE", "http://localhost:51521")
    assert config.resolve_base_url() == "http://localhost:51521"
    assert config.resolve_base_url("flag.example.org") == "https://flag.example.org"


def test_reads_the_standalone_scripts_settings_file(tmp_path):
    d = config.config_dir()
    os.makedirs(d)
    with open(os.path.join(d, "amdx.json"), "w") as f:
        f.write('{"base": "older.example.org"}')
    assert config.resolve_base_url() == "https://older.example.org"


def test_config_dir_rules(monkeypatch, tmp_path):
    monkeypatch.setenv("A4SX_CONFIG_DIR", str(tmp_path / "x"))
    assert config.config_dir() == str(tmp_path / "x")
    monkeypatch.delenv("A4SX_CONFIG_DIR")
    if sys.platform.startswith("win"):
        return
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config.config_dir() == str(tmp_path / "xdg" / "a4sx")
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/ignored")
    assert config.config_dir() == os.path.join(os.path.expanduser("~"), ".config", "a4sx")


def test_import_writes_nothing(tmp_path):
    assert not os.path.exists(config.config_dir())


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
def test_token_is_written_0600_in_a_0700_dir():
    path = config.save_token("a4sx-test-" + "abcdefgh1234")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700
    assert config.load_token() == "a4sx-test-abcdefgh1234"
    assert not [n for n in os.listdir(os.path.dirname(path)) if ".tmp-" in n]


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
def test_a_widened_token_file_is_tightened_on_read():
    path = config.save_token("tok-1234567")
    os.chmod(path, 0o644)
    assert config.load_token() == "tok-1234567"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_env_token_wins_and_clear(monkeypatch):
    config.save_token("stored-token")
    monkeypatch.setenv("A4SX_SESSION", "env-token")
    assert config.load_token() == "env-token"
    monkeypatch.delenv("A4SX_SESSION")
    assert config.clear_token() is True
    assert config.load_token() is None
    assert config.clear_token() is False


@pytest.mark.parametrize("bad", ["", "   ", "two words"])
def test_bad_tokens_are_refused(bad):
    with pytest.raises(ValueError):
        config.save_token(bad)
