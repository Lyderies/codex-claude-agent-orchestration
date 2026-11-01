# Security policy

## Reporting a vulnerability

Report a vulnerability privately through GitHub's
[private vulnerability reporting](../../security/advisories/new) for this repository. Please do
not open a public issue for security problems. Include the release and Codex package version
(from `CHANGELOG.md` and `packages/codex-agents/runtime-releases.json`), the steps to reproduce,
and what an attacker gains.

## Current status

The current release is the release candidate for dogfood toolchain v1 (release 0.2.0, Codex
package 0.8.9): read-only children, macOS first, Linux partly untested. Fixes go into the next
release; there are no maintained older releases. Changes to authority, containment or billing
logic get an independent security review before release, recorded in the CHANGELOG.

## Scope

Areas of particular interest:

- a child agent gaining more authority than its parent's grant;
- escape from a child's sandbox, private export or (from G3) worktree;
- a Codex session reaching owner authority: billing attestation, owner keys, collector
  registration, the Codex command rule or its protected runtime copy in `~/.codex`;
- a command the rule allows running or loading anything a sandboxed session can write;
- the project's learning hooks (which Codex runs outside its sandbox) running anything but the
  installer's trusted interpreter and the checkout's own `learning_hook.py`;
- a session opened on a project folder getting past the session workspace guard;
- a bridged child loading execpolicy rules, connectors, MCP servers or hooks;
- the owner tools (`setup.sh`, `scripts/attest.py`, `scripts/install.py`) running project code,
  signing anything other than the statement the owner approved, or starting a program the
  project's Git settings name;
- any path that could cause metered billing;
- identifiers or run records leaking into the repository.

## Known limits

Accepted limits are listed in the README under "Does not protect against" and in the residual
risks of [the G2 design](docs/delivery/g2-design.md) (S4c, S5b and S6). The supported setup
assumes no Codex session is opened on the home folder, a folder above it or `/`, and that this
repository sits outside every folder opened in Codex. The learning hooks name a fixed trusted
interpreter and an absolute path, but they run the project's installed hook code: that code is as
protected as `.codex/hooks.json` itself (Codex keeps `<project>/.codex` read-only for a session
opened on the project, and the session guard refuses sessions that can write it from above),
not by a separate copy. A report that shows one of these limits is
worse than documented is welcome.
