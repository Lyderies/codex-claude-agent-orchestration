# Codex–Claude agent orchestration

Let a Codex or Claude coding session delegate bounded sub-tasks to the best-suited Codex or
Claude child agent. The choice is deterministic: verified quality first, then usage cost, then
subscription quota headroom. Metered billing is never used.

**Status:** pre-alpha. Nothing here is ready to install yet. See the
[delivery plan](docs/delivery/plan.md) for the objective and goals.

## What it will do

- **Route sub-tasks across providers.** Research, planning, coding, review and verification
  sub-tasks go to a Codex or Claude child, picked by code rather than by the model.
- **Contain children.** Read-only children first. Write-capable children work in isolated
  worktrees, and each change is verified by the other provider before it is integrated.
- **Learn from evidence.** Every run is recorded in a local ledger. Routing preferences change
  only when paired runs or a frozen evaluation set show a quality-first improvement, and only
  with the owner's approval.
- **Install with one command,** into any Git project.

## Principles

- Deterministic code and committed project configuration hold all authority. Model output never
  grants permissions.
- Only existing subscription logins are used. API keys, usage credits and paid fallback are never
  used.
- Run records stay on your machine and are never committed.

## Development

```sh
git config core.hooksPath .githooks          # enable the identifier guard on commit
python3 scripts/check_identifiers.py         # scan tracked files
python3 -m unittest discover -s tests -v     # run the tests
```

This repository is public. The identifier guard blocks commits that contain personal or machine
identifiers, or that use a non-noreply commit email.

## License

[Apache License 2.0](LICENSE). See [NOTICE](NOTICE).
