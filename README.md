> **Pre-release:** not yet published to PyPI. Do not use it to upload sessions yet; use the official a4sx CLI from session-exchange.com.

<!--
SPDX-FileCopyrightText: 2026 crossVault GmbH
SPDX-License-Identifier: Apache-2.0
-->

# a4sx

[Repository](https://github.com/crossvault/a4sx) · [Issues](https://github.com/crossvault/a4sx/issues) · [Changelog](CHANGELOG.md)

Save your coding-agent sessions to a private library, and **resume Claude Code sessions on any machine**
as a new local session. `a4sx` is the command-line tool and Python SDK for
[session-exchange.com](https://session-exchange.com).

- **Push** a session to your private library: Claude Code and Codex CLI transcripts, and Grok Build
  session folders. A client-side secret scrub runs *before* anything leaves your machine, and push
  tells you exactly what it changed.
- **Resume** a saved session anywhere: `a4sx resume <name>` writes it as a fresh Claude Code
  session in the directory you are in and starts `claude --resume`. Your saved copy never changes.
- **Lineage**: a session you resumed and push again is recorded as a child of the one it came from,
  so `a4sx list` shows your library as a tree.
- **Search** published sessions and add the ones you are entitled to to your library.
- **SDK**: everything the command does is in `a4sx.sdk`, for your own scripts, plugins or MCP servers.

Python 3.9+. The only dependency is [`redact-transcripts`](https://pypi.org/project/redact-transcripts/)
(stdlib-only), which powers the pre-scrub.

| Harness | push, list, search, fetch | resume |
|---|---|---|
| Claude Code | yes | yes |
| Codex CLI | yes | not yet |
| Grok Build | yes | not yet |

`resume` refuses a session recorded with another harness; it never turns one into a Claude Code
session. Such a session stays in your library, and `a4sx fetch` downloads its original file.

## Quickstart (5 minutes)

```console
$ pipx install a4sx            # or: pip install --user a4sx
$ a4sx login                   # opens your browser; on SSH or headless it prints a link for your phone
signing in to https://session-exchange.com
...
signed in. your private library on https://session-exchange.com is ready.
```

Save the Claude Code session you worked in most recently:

```console
$ a4sx push --latest --label my-work
a4sx push: client scrub: 1 line changed (secret.github_token: 1) — line 212
a4sx push: local sha256    5d1c…
a4sx push: uploaded sha256 9a07…: DIFFERS from the local file (the client scrub rewrote it). …
{ "collection": "my-work-…", "status": "staged (private)", … }
```

Not sure what would leave your machine? `a4sx push --latest --dry-run` shows which lines the scrub
would change, uploads nothing and needs no sign-in.

On any machine, in the folder you want to work in:

```console
$ a4sx list
 #  name          created           updated           vers     ctx  title
 1  my-work       2026-10-05 09:12  2026-10-05 17:40     3    84k  Fix the flaky upload test
$ a4sx resume my-work
a4sx: new local session 0b6f… seeded from my-work-… (in /home/me/app)
```

`resume` starts `claude --resume <new-id>`. Use `--no-launch` to only write the session, and
pass arguments to `claude` after `--`, e.g. `a4sx resume my-work -- --model opus`.

When you are done, `a4sx push --latest` saves your continuation; it is linked to `my-work` in the tree.

Other harnesses push the same way; the server recognises the format:

```console
$ a4sx push ~/.codex/sessions/2026/10/14/rollout-….jsonl --label codex-run     # Codex CLI
$ a4sx push ~/.grok/sessions/<project>/<session-id>/ --label grok-run           # Grok Build: the folder
```

For a Grok Build session folder only its `updates.jsonl` is uploaded. Pushing one of the folder's other
files (`chat_history.jsonl`, `system_prompt.txt`, ...) is refused.

## Commands

| Command | What it does |
|---|---|
| `a4sx login [--no-browser] [--provider github]` | Device login in the browser. `--token -` stores a token you already have (read from stdin). If the account still has to confirm an e-mail address, it says so and sets up the library on the next login. |
| `a4sx logout` | Delete the stored token and end the session on the server. |
| `a4sx push FILE\|FOLDER \| --latest [--label L] [--into ID] [--dry-run] [--fail-on-scrub] [--no-scrub]` | Save a session (a Claude Code or Codex CLI `.jsonl`, or a Grok Build session folder; `--latest` picks the newest local Claude Code session). `--into` adds a new version of a saved session. `--fail-on-scrub` exits 3 if the scrub would change anything. If the server is busy scrubbing other uploads, push waits as long as it asks and sends the same push again. |
| `a4sx list [--sort updated\|created\|name] [--json]` | Your library, as a lineage tree. |
| `a4sx resume NAME [--cwd DIR] [--from-compact] [--no-launch]` | Continue a saved Claude Code session as a new local session. A marketplace listing recorded with another harness is refused before it is added to your library. |
| `a4sx search [WORDS…] [--library] [--tag T]` | Search published sessions (no sign-in needed), or your own library. |
| `a4sx acquire ID` | Add a published session you are entitled to (free, bought, granted) to your library. Never buys anything. |
| `a4sx fetch NAME [-o PATH]` | Download a saved transcript without resuming it (for another harness: its original file). |
| `a4sx tokens NAME\|FILE [--window 1m]` | Token footprint against a context window. |
| `a4sx squash NAME` | Recipe to compact a long session into a resumable tip. |
| `a4sx craft SRC… --label L` | Compile one resumable base session from several sessions (pre-scrubbed like `push`). |
| `a4sx delete NAME` | Permanently delete a saved session; every stored version is erased. |
| `a4sx whoami`, `a4sx version`, `a4sx completion bash` | |

`NAME` is a saved session's label, its id, or a unique part of its title.

## Configuration

| Setting | Default |
|---|---|
| Server | `--base URL` (before or after the command), else `$A4SX_BASE`, else the server you signed in to last, else `https://session-exchange.com`. Plain `http://` is accepted for `localhost` only. |
| Session token | Stored per server by `a4sx login`; each server needs its own login. `$A4SX_SESSION` supplies one for this shell, for `https://session-exchange.com` only, or for `$A4SX_SESSION_BASE` if you set it. |
| Config dir | `$A4SX_CONFIG_DIR`, else `%LOCALAPPDATA%\a4sx` on Windows, else `$XDG_CONFIG_HOME/a4sx` or `~/.config/a4sx`. |
| Claude Code dir | `$CLAUDE_CONFIG_DIR`, else `~/.claude`. |
| Hints | `A4SX_HINTS=0` turns off the `next:` hints on stderr. |
| Upload timeout | Scales with the size; `A4SX_UPLOAD_TIMEOUT_S` overrides it. |
| Headless login | `A4SX_NO_BROWSER=1` or `--no-browser`. |

## What leaves your machine

- `push` uploads the transcript **after** the client scrub (unless `--no-scrub`). It reports the
  changed lines, the rules that fired (never the values), and the sha256 of your file and of the
  upload. The server scrubs again before storing. Both are regex backstops, **not a guarantee**:
  review what you push.
- The scrub covers credential shapes: API keys and tokens (Anthropic, OpenAI, GitHub, GitLab,
  Slack, AWS, Google, Stripe, JWTs), bearer and basic auth, credentials in URLs, private keys,
  secret-named assignments, a4sx session tokens. It does **not** remove names, e-mail addresses,
  IP addresses or paths; use [`redact-transcripts`](https://pypi.org/project/redact-transcripts/)
  directly if you need that.
- Your session token is stored with mode 0600, **bound to the server that issued it**, and is only
  ever sent to that server, as an `Authorization` header that is not forwarded on redirects.
  Pointing a4sx at another server (`--base`, `$A4SX_BASE`) never sends it your token; that server
  needs its own `a4sx login`. The sign-in links a server sends are only shown or opened if they are
  on that server's own origin. The token is never printed or logged.
- Resumed sessions are written with mode 0600. A session from another author, and anything you
  derive from it (a continuation you push, a `craft`ed base), is treated as untrusted input:
  `resume` keeps Claude Code's permission prompts on unless you pass `--trust-base`.
- `search` and `version` send no token.

## SDK

```python
from a4sx.sdk import Client, NotFound

client = Client()                                  # same server and token resolution as the CLI

# sign in (device login); show the link and code however you like
client.login(on_pairing=lambda login, restarted: print(login.login_url, login.user_code))

res = client.push_file("session.jsonl", label="my-work")      # pre-scrubbed, retried safely
print(res.response["collection"], res.scrub.summary())

rows = client.list_sessions()
hits = client.search("flaky test", scope="public")["items"]
client.acquire(hits[0]["id"])                      # entitled listings only; never buys

resumed = client.resume("my-work", cwd="/home/me/app")
print(" ".join(resumed.local.resume_command))      # claude --resume <new id>
```

| Area | API |
|---|---|
| Auth | `Client.login()`, `device_login()`, `start_device_login()` / `poll_device_login()`, `login_with_token()`, `logout()`, `me()` |
| Library | `list_sessions()`, `find()`, `resolve()`, `search()`, `get_listing()`, `fetch()`, `acquire()`, `delete()` |
| Publish | `push()`, `push_file()`, `push_path()`, `craft()`; `scrub_bytes()` for a dry run; `PushResult.harness` |
| Resume | `Client.resume()`; offline: `resume_locally()`, `rekey()`, `project_dir_name()`, `claude_projects_dir()`, `latest_local_session()` |
| Transcripts | `token_stats()`, `summarize()`, `trim_to_last_compact()`, `stitch()` |
| Harnesses | `harness_id()`, `harness_name()`, `FetchedSession.harness`, `Client.listing_harness()` |
| Errors | `A4sxError` > `ApiError` (`PaymentRequired`, `MemoryOnly`), `NotLoggedIn`, `NetworkError`, `NotFound`, `LoginExpired`, `LoginTimedOut`, `EmailVerificationPending`, `OtherHarness` |

The SDK never prints and never calls `sys.exit`. Resuming without the SDK takes four steps, which
`resume_locally()` implements: decode the transcript, pick a fresh session id, set `sessionId` on
every record that has one, and write it to
`${CLAUDE_CONFIG_DIR:-~/.claude}/projects/<cwd with every non-alphanumeric character replaced by "-">/<id>.jsonl`.
Then run `claude --resume <id>`.

## Compatibility

- Claude Code stores sessions in an undocumented format that can change between releases. `a4sx`
  only rewrites `sessionId` (and, for `craft`, the `uuid`/`parentUuid` chain), so it is robust to
  most changes, but a resume can still fail after a large format change. Please open an issue.
- The standalone `a4sx` script from the website's installer and this package share the config dir,
  so a sign-in from one works in the other. This package does not include older command aliases,
  the launch profiles or the workspace-admin commands; keep using the installer's script if you rely on those.
- The pre-scrub uses `redact-transcripts` 0.1.x. The installer's script carries newer scrub rules; this
  package moves to them with `redact-transcripts` 0.2.x. The server scrubs every upload either way.

## Development

```console
$ python -m pip install -e '.[dev]'
$ pytest -q            # offline: SDK tests run against a local stub server
$ ruff check . && reuse lint
```

See [CONTRIBUTING.md](CONTRIBUTING.md). Security issues: [SECURITY.md](SECURITY.md).

## About

`a4sx` is made by [crossVault GmbH](https://session-exchange.com), the company behind **a4sx**
([session-exchange.com](https://session-exchange.com)), a library and marketplace for coding-agent
sessions. This client is open source so you can see exactly what touches your transcripts before
anything is uploaded, and so other tools can build on the same SDK. The hosted service is not part
of this repository. Parts of this project were developed with AI assistance (Claude).

## License

Apache-2.0, see [LICENSE](LICENSE). Copyright 2026 crossVault GmbH.
