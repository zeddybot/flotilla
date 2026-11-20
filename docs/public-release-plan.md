# Public release preparation

These checks cover publishing Flotilla's source, qualifying a release, and
supporting users who install it.

Before a public release:

- Inventory tracked files, git history, generated artifacts, personal paths,
  workspace/user identifiers, internal links, logs, and setup defaults. Perform
  a deliberate secret/history scan; a clean working tree is not proof that
  history is safe. Decide whether to sanitize history or publish a clean export.
- Rotate the previously exposed Slack bot and Socket Mode tokens. Sameer owns
  that rotation. Install and validate replacements, then retire old credentials;
  removing a token from a file does not revoke it.
- Decide the project license, ownership/contributor terms, dependency notices,
  name/package availability, and security-reporting contact. The working CLI
  name `flotilla` does not settle public naming or trademark questions.
- Replace personal setup defaults with examples, document the single-owner
  trust model and supported versions, and run a clean-machine install. Choose a
  versioned distribution channel and upgrade/rollback policy; today's installer
  requires a checkout or bundled installed runtime and `uv`.
- Document public support boundaries, known capability gaps, contributor setup,
  issue reporting without secrets, and a minimal changelog/release process.
- Confirm hosted GitHub Actions results for the release revision. Inspect the
  active ruleset, account limits, permissions, and runner availability.
- Run the supported OS/Python matrix, packaging checks, secret scans, and a
  disposable end-to-end release exercise on the exact release revision. Record
  what was actually tested and what remains unqualified.

Record the decisions on repository visibility, history handling, and distribution
before publishing. Document the clients, hosts, and workflows the release
supports.
