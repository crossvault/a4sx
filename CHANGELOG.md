# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.11.0] - unreleased

First release as a Python package. Before this, `a4sx` was a single script distributed by the
website's installer (versions up to 0.10.x); this package continues its version line.

### Added

- `push` takes Codex CLI sessions (`rollout-*.jsonl`) and Grok Build session folders (only the
  folder's `updates.jsonl` is uploaded; its other files, or a symlinked `updates.jsonl`, are refused).
  The server recognises the format; `PushResult.harness` says what it was recognised as.
- Harness support in the SDK: `harness_id()`, `harness_name()`, `FetchedSession.harness`,
  `Client.listing_harness()` and `push_path()`. `fetch` of a session recorded with another harness
  returns its stored original file.
- `push` and `craft` ride out a busy server scrub pool (`scrub_busy`, `scrub_pool_busy`): up to 6
  attempts with the same idempotency key, each after the wait the server asks for (at most 60 s),
  plus jitter. Other retryable failures keep 3 attempts; `scrub_timeout` is never retried.
- Every push tells the server what the client scrub did (`client_scrub=secrets-only` or `none`).
- `login`: an account that still has to confirm an e-mail address gets no library until it has
  (`EmailVerificationPending`); the hint for a newly created account is shown.

- `a4sx.sdk`: an importable, documented API for everything the command does (device login, list,
  search, fetch, acquire, push, craft, delete, resume). It raises exceptions instead of exiting and
  never prints.
- `a4sx resume NAME`: fetch a saved session, give it a fresh session id, write it into Claude Code's
  project dir for the current (or `--cwd`) directory, and start `claude --resume`. `--no-launch`
  only writes it.
- `a4sx search`: search published sessions (anonymous) or your own library (`--library`).
- `a4sx logout` (also ends the session on the server) and `a4sx whoami`.
- `a4sx login --token -` reads the token from stdin, keeping it out of shell history.
- `list --json`, `search --json`, `fetch --output`.

### Changed

- The client scrub is the a4sx CLI's own (the server's rules), vendored by `tools/vendor_scrub.py` from
  the CLI 0.10.54: secrets, command-line passwords, Sheet ids, and personal data, hosts, IP and MAC
  addresses, home-directory user names and key-file paths, ON by default as in the CLI. `push --keep-pii`
  keeps the personal-data group (`client_scrub=secrets-only`); `--no-scrub` turns it off. Rule names and
  the `--dry-run` listing (line, column, rule, masked length) are the CLI's. Parity vectors produced by
  the CLI are checked offline in the test suite, and CI checks the vendored module against the published
  CLI (on every push and weekly) and before every release. The one difference: the host rule covers
  subdomains of session-exchange.com only.
- The server address is a URL (`--base`, `A4SX_BASE`); `http://` is accepted for loopback hosts only.
- The session token is written atomically with mode 0600 from the start, and is attached so that it
  is never forwarded on a redirect.
- Config dir: `A4SX_CONFIG_DIR`, `%APPDATA%\a4sx` on Windows, `$XDG_CONFIG_HOME/a4sx` honoured.

### Security

- A session token is stored per server (`credentials.json`, 0600) and only ever sent to the server
  that issued it; `--base`/`A4SX_BASE` pointing elsewhere needs its own login. `A4SX_SESSION`
  applies to the public server, or to `A4SX_SESSION_BASE`.
- Every upload is pre-scrubbed, `craft` included; `craft --no-scrub` is an explicit, warned opt-out.
- Sign-in links are only shown or opened if they are on the server's own origin; pairing expiry
  is capped at 15 minutes.
- Resumed and fetched transcripts are written 0600 (never through a symlink); new directories 0700.
- `resume` (and `launch`, `squash`) refuse a session recorded with another harness
  (`OtherHarness`), and check a marketplace listing's harness BEFORE acquiring it, so a refusal adds
  nothing to your library. When that check can't be made (no answer, or a server error) nothing is
  acquired.
- The untrusted-content marker of an acquired session survives `resume` + `push`, `craft` and
  `push --into` (server-side parent edges, plus a local registry), so resuming anything derived
  from someone else's session keeps Claude Code's permission prompts on unless `--trust-base`.
- `login --token VALUE` warns that the value is visible in `ps` and shell history.

### Removed

- `update` (use `pipx upgrade a4sx`), launch profiles, workspace-admin commands, older command aliases and
  older environment-variable names. The installer's standalone script keeps them.

### Dependencies

- None: the package is stdlib only. (An earlier candidate depended on `redact-transcripts` 0.1.x for a
  secrets-only pre-scrub; it was replaced by the CLI's rules before release. It may move to
  `redact-transcripts` 0.2.x once that carries the same rules and passes the same parity vectors.)
