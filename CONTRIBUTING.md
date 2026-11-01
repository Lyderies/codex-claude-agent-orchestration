# Contributing

Thanks for your interest. Please open an issue before sending a pull request, so we can agree on the
change first. Report security problems privately as described in [SECURITY.md](SECURITY.md), never in a
public issue.

## Branches

- `main` holds releases only.
- `version/<semver>` is where development for the next release happens. Each one is cut from `main`.
- `feature/<name>` branches come off the current version branch and merge back into it once their checks
  and any required review pass.
- The owner merges a version branch into `main` after the full regression run and CI pass.
- `planning` is a separate branch, with no shared history, for plans, design working documents and
  research. See [Planning material](#planning-material).

## Checks

Turn on the repository's hooks once, then run the checks before every pull request:

```sh
git config core.hooksPath .githooks          # identifier guard on every commit
python3 scripts/check_identifiers.py         # scan tracked files
ruff check .
python3 -m unittest discover -s tests -v
(cd packages/codex-agents && python3 -m unittest discover -s tests)
(cd packages/claude-agents && PYTHONPATH=src python3 -m unittest discover -s tests)
```

CI runs the identifier guard, Ruff and all three suites on Linux and macOS.

- Every runtime change needs a Codex package version bump and an entry in
  `packages/codex-agents/runtime-releases.json`.
- Changes to authority, containment or billing logic need an independent security review before
  release, recorded in the CHANGELOG. Everything else needs tests, Ruff and a clean identifier scan.
- Describe user-visible changes in [CHANGELOG.md](CHANGELOG.md).

## No identifiers

This repository is public. Never commit personal names, email addresses, hostnames, home paths or
machine pins, and keep run records and ledgers out of it (they live in `~/.local/state/codex-agents`).
Commit with a GitHub `noreply` address.

`scripts/check_identifiers.py` scans tracked files, and the pre-commit hook scans staged content and your
commit identity. Findings report only a category, path and line, never the matched text. Terms specific
to you can go in a private denylist outside the repository, one per line:
`~/.config/codex-claude-agent-orchestration/identifier-denylist.txt`, or the file `$AO_IDENTIFIER_DENYLIST`
names.

## Documentation

User and contributor documentation lives in [docs/](docs/README.md). The
[security model](docs/security-model.md) and SECURITY.md's known limits must agree; update both when a
limit changes.

## Planning material

Plans, design working documents and research live on the `planning` branch, not on `main` or the version
branches. Check it out beside your clone:

```sh
git fetch origin planning
git worktree add ../planning planning
```

That branch has no scripts of its own; run the identifier scan there from your code checkout:
`(cd ../planning && python3 "$OLDPWD/scripts/check_identifiers.py")`.

## License

Contributions are accepted under the [Apache License 2.0](LICENSE) (section 5), the project's license.
