# Public release preparation: plan only

The repository stays private. This change does not alter GitHub settings, enable
public distribution, rename the Slack app, or install a new ruleset. Local CI is
the current qualification path while private Actions billing is unavailable.

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
- Decide the GitHub Actions budget and required merge checks. Verify the actual
  ruleset when enabled; a checked-in ruleset JSON is not enforced protection.
  Public repository availability does not remove the need to inspect account
  limits, permissions, runner availability, and check configuration.
- Run the supported OS/Python matrix, packaging checks, secret scans, and a
  disposable end-to-end release exercise on the exact release revision. Record
  what was actually tested and what remains unqualified.

Publish only after a separate explicit decision covering repository visibility,
history handling, distribution, and any Slack app/product rebrand. Other clients,
new hosts, multiplayer, and billing are not prerequisites for a small public
single-owner release if limitations are clearly documented.
