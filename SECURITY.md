# Security policy

## Reporting a vulnerability

Please **do not** open a public issue for a security problem.

Report it privately through GitHub's **"Report a vulnerability"** button on this repository's Security tab
(private vulnerability reporting), or by e-mail to **info@session-exchange.com**.

Please include the affected version or commit, a description, and a minimal reproduction with synthetic
data only: never send a real transcript, token or key. We aim to acknowledge reports within 5 working days
and to agree on a disclosure timeline with you.

## Scope

In scope (this repository, the `a4sx` client and SDK):

- the session token: stored anywhere other than the config dir with mode 0600, printed, logged, put
  into an exception message, sent to a host other than the configured server, sent over plain http
  to a non-loopback host, or forwarded on a redirect;
- the client-side scrub: a secret or personal-data shape that it is documented to catch but misses, a
  difference from the a4sx CLI's scrub on the same input, or input that
  makes it use excessive CPU or memory;
- `resume`: writing outside the Claude Code projects directory, overwriting an existing session, or
  keeping permission-bypass flags for a session from another author without `--trust-base`;
- any command that sends transcript content when it says it doesn't (e.g. `push --dry-run`).

Out of scope:

- the hosted service at session-exchange.com. Report problems with it to the same address; they are
  handled, but not in this repository;
- personal data the scrub is not documented to remove (for example people's names in prose), or anything
  left in an upload made with `--keep-pii` or `--no-scrub`. The scrub is a backstop, not a guarantee;
- planted instructions in a session from another author. Treat such sessions as untrusted input;
  `a4sx resume` says so and keeps Claude Code's permission prompts on.

## Supported versions

Only the latest release receives fixes.
