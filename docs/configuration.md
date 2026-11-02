# Configuration

The [setup wizard](setup-wizard.md) writes most of this for you. This page lists every file and option it
touches, for reference and for repairs. The commands that write them are in [manual setup](manual-setup.md).

## Project files

| File (in the project) | Purpose |
|---|---|
| `.codex/orchestration/provider-routing.json` | Enabled providers, allowed Claude models, and `codex_primary_may_bridge_to_claude`. Committed and clean; without it only Codex children are allowed |
| `.codex/orchestration/operational-checks.json` | The exact, hashed test and security commands operational QA may run |
| `.codex/orchestration/project-agent-routing.json` | Optional mapping of project agents to task families; setup writes an empty one if missing |
| [`docs/routing-table.md`](routing-table.md) (this repository) | The starting table; the policy mirrors it, and a test holds the policy to its order |

### `provider-routing.json`

Says which providers and Claude models children may use. Commit it in the project; setup's step 3
writes it.

```json
{
  "version": 1,
  "providers": {
    "codex": {"enabled": true},
    "claude": {"enabled": true, "models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]}
  },
  "codex_primary_may_bridge_to_claude": false
}
```

- `providers.<name>.enabled` turns a provider's children on or off. To stop a provider's children,
  disable it here.
- `providers.claude.models` lists the Claude models children may use. Models you have not enabled stay
  disabled.
- `codex_primary_may_bridge_to_claude` (default `false`) lets a Codex session start Claude children. It
  needs Claude children enabled and the orchestrator profile below.

### `operational-checks.json`

Names the exact commands operational QA may run, each with the SHA-256 of its executable and
interpreter. A project that has never been set up needs one before the first install; the
[recipe](manual-setup.md#operational-checks) builds it from two small, committed wrapper scripts.

- The top level has `version` (1), `deterministic_checks` (your tests) and `security_checks` (for example
  a secret scanner).
- Each check has `check_id`, `driver` (`python-wrapper`), `argv`, `executable_sha256`,
  `interpreter_sha256`, `runtime_tree` and `timeout_seconds`.
- The schema is `packages/codex-agents/native-package/.codex/orchestration/schemas/operational-checks.schema.json`.
- Operational QA runs these commands inside macOS's sandbox and fails closed on other systems.

### `project-agent-routing.json`

Optional. Maps the project's own agents to task families. When the project has none, the wizard
installs `{"version": 1, "roles": {}, "verifiers": {}}`.

### Files the installer writes

- Commit the installed files, including `.claude/settings.json`, which holds the Claude Code deny rules
  for the owner-only commands. Your existing settings are kept.
- Never commit `.codex/hooks.json` (the learning hooks, rendered with absolute paths to this checkout and
  the trusted Python) or `.codex/orchestration/local-install.json`. The installed `.codex/.gitignore`
  ignores both, and each clone gets its own.

## Machine files

| Path | Purpose |
|---|---|
| `~/.local/state/codex-agents` | Your orchestrator records: the ledger, the owner key record and the collector's files. Never committed |
| `~/.ssh/codex-agents-owner` | The passphrase-protected owner key that signs billing attestations; `attest.py` creates it if you have none |
| `~/.codex/orchestrator.config.toml` | The optional Codex profile with sandbox network on, for `codex -p orchestrator` (step 7) |
| `~/.codex/rules/agent-orchestration-<digest>.rules` | The optional Codex commands rule (step 8) |
| `~/.codex/agent-orchestration/<project digest>/<runtime digest>/` | The owner-only protected copy of the orchestrator that the rule runs |

The profile (mode 0600) contains the following; ordinary sessions keep network off, and your main
`config.toml` is never edited:

```toml
sandbox_mode = "workspace-write"

[sandbox_workspace_write]
network_access = true
```

## Environment

| Variable | Effect |
|---|---|
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | Must not be set; admission fails closed on an API key in the environment |
| `XDG_STATE_HOME` | Moves your records to `$XDG_STATE_HOME/codex-agents`, only when it points inside a hidden folder of your home such as `~/.local/state`; any other value is refused |
| `CODEX_HOME` | Leave it unset or pointing at `~/.codex`; the Codex rule works only in your account's real `~/.codex` |
| `NO_COLOR` | `NO_COLOR=1` gives the wizard plain output |
