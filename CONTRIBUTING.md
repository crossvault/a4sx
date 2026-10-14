# Contributing

Thanks for helping. Bug reports, fixes and Claude Code format changes we missed are all welcome.

## Developer Certificate of Origin (DCO)

Every commit must be signed off under the [Developer Certificate of Origin 1.1](https://developercertificate.org/).
The sign-off certifies that you wrote the change, or otherwise have the right to submit it under this
project's licence (Apache-2.0).

Add it with `git commit -s`, which appends a line using your git name and email:

```
Signed-off-by: Your Name <you@example.com>
```

A DCO check runs on every pull request. To fix a missing sign-off on your last commit, run
`git commit --amend -s` and force-push your branch. For several commits, run
`git rebase --signoff main`. We don't use a CLA.

## Pull requests

- Keep each pull request to one change, with a test.
- Tests never touch the network: SDK tests run against the local stub server in `tests/stub_server.py`,
  and a guard fails any test that opens a non-loopback connection. Use synthetic transcripts and
  token-shaped strings assembled at runtime, never real ones.
- Before pushing, run `ruff check .`, `reuse lint` and `pytest -q`; CI runs the same on Python 3.9-3.13.
- There are no runtime dependencies (stdlib only). Please open an issue first if a change needs one.
- The client scrub (`src/a4sx/_cli_scrub.py`) is generated from the a4sx CLI by `tools/vendor_scrub.py`,
  so it applies exactly the CLI's (and the server's) rules. Don't edit it by hand: scrub rule changes go
  into the CLI, then `python tools/vendor_scrub.py` regenerates the module and the parity vectors.
  CI fails while they differ from the published CLI (`python tools/vendor_scrub.py --check`).
- The CLI (`a4sx/cli.py`) only parses, prints and maps errors; behaviour belongs in `a4sx/sdk.py`, so
  other tools get it too.
- New source files start with the SPDX header:

  ```python
  # SPDX-License-Identifier: Apache-2.0
  # Copyright 2026 crossVault GmbH
  ```

  If you prefer to keep your own copyright line, add it below; the sign-off is what matters.

## AI-assisted contributions

You may use AI tools. You are still the author: you must understand and be able to explain every line,
the DCO sign-off is yours, and the tests must pass. Say in the pull request if a substantial part was
generated. Unreviewed bulk changes will be closed.

## Security issues

Don't open a public issue. See [SECURITY.md](SECURITY.md).
