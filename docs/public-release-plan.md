# Public release preparation

Use this checklist to qualify a new public Flotilla release and support users
installing it. The [installation guide](connect-client.md) covers the released
source checkout and client pairing.

Before a public release:

- Inventory tracked files, git history, generated artifacts, personal paths,
  workspace/user identifiers, internal links, logs, and setup defaults. Perform
  a deliberate secret/history scan; a clean working tree is not proof that
  history is safe. Decide whether to sanitize history or publish a clean export.
- Revoke any credential exposed during release preparation. Install and validate
  replacement Slack credentials before retiring the old ones.
- Decide the project license, ownership/contributor terms, dependency notices,
  name/package availability, and security-reporting contact. The working CLI
  name `flotilla` does not settle public naming or trademark questions.
- Check setup examples for private defaults. Document the paired-client trust
  model, supported versions, and upgrade and rollback steps. Verify a clean
  install from the release checkout with `uv`.
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
