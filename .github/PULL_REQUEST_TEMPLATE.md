## What and why

<!-- One or two sentences, and the issue this closes. -->

## Checklist

- [ ] Based on the current `version/<semver>` branch, not `main`
- [ ] `ruff check .` and all three test suites pass (see CONTRIBUTING.md)
- [ ] `python3 scripts/check_identifiers.py` passes: no names, emails, hostnames, home paths or machine pins
- [ ] Runtime change: Codex package version bumped and `runtime-releases.json` updated
- [ ] Authority, containment or billing change: independent security review requested
- [ ] Docs and CHANGELOG updated where users would notice
