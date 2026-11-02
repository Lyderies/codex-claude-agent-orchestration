# Packages

The two agent packages this orchestrator builds on, imported once and developed here from now on.

| Package | Contents |
|---|---|
| `codex-agents/` | The Codex orchestration package (`native-package/`): agents, skills, hooks, deterministic routing, contracts, the learning ledger and operational QA, with its installer and operational scripts in `scripts/` and tests in `tests/` |
| `claude-agents/` | The Claude agent package: roles, contracts, validation, the installer, and the stream and usage parsers in `src/claude_agents/`, with tests in `tests/` |

[`PROVENANCE.json`](PROVENANCE.json) records the source commit and a SHA-256 hash for every
imported file, plus the files changed after import and why. The upstream repositories were the
authors' private development repositories. They are now frozen, and their research, evaluation
records and documentation were not imported.

Run each package's tests from its own directory:

```sh
(cd codex-agents && python3 -m unittest discover -s tests)
(cd claude-agents && PYTHONPATH=src python3 -m unittest discover -s tests)
```
