# Codex–Claude agent orchestration

Let a Codex or Claude coding session delegate bounded sub-tasks to the best-suited Codex or
Claude child agent. The choice is deterministic: verified quality first, then usage cost, then
subscription quota headroom. Metered billing is never used.

**Status:** pre-alpha. Read-only cross-provider routing works and is being piloted; write-capable
children are in development. See the [delivery plan](docs/delivery/plan.md) for the objective and goals.

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

## Setup

You need macOS or Linux, Python 3.11 or later, Git, and the Codex and Claude Code clients signed in
with their subscriptions. API keys are refused.

**Before you run it:** a project that has never been set up needs a prepared
`operational-checks.json`, which names the exact test and security commands, with their hashes,
that operational QA may run. See the recipe under "Advanced: manual setup" below. The wizard asks
for the file's path. A future wizard step will generate it; that is planned, not done yet.

From the root of this repository, run one command:

```sh
./setup.sh [/path/to/project]
```

It is a step-by-step terminal wizard (arrow keys, Enter, Space to toggle, `q` to quit) that:

1. checks your machine and both sign-ins;
2. lets you choose the project and previews the install;
3. lets you choose which providers and Claude models children may use;
4. installs, verifies and, if you agree, commits the files in the project (it never pushes). It
   stages exactly the files the installer reported plus `provider-routing.json`, and the summary
   says `committed`, `nothing to commit`, `not committed (you chose to commit yourself)` or
   `commit failed` and lists anything left uncommitted. Only files that really changed are listed,
   and when there are none it skips the commit question. The commit skips the project's Git
   hooks;
5. registers the learning collector;
6. hands the terminal to the billing attestation, which only you can sign;
7. optionally creates the Codex orchestrator profile;
8. when Codex sessions may orchestrate the project, offers the Codex commands rule, which lets
   only this project's everyday orchestrator commands run outside Codex's sandbox (owner
   commands stay blocked). Every Codex session on the machine loads the rule; it runs a
   protected copy of the orchestrator in the Codex settings folder, so a session opened on a
   parent folder cannot change what it runs. Never start Codex with your home folder's parent
   or `/` as its workspace. Delete the rule file, or run the remove command, to undo it;
9. summarises the result and how to use it.

Run it again on an installed project to update, verify or re-attest. It needs an interactive
terminal and refuses to run piped or scripted. Full command output goes to a private log,
shown only if something fails. Set `NO_COLOR=1` for plain output. In Claude Code, use the
`agent-orchestration` skill afterwards. In Codex, start with `codex -p orchestrator` if the optional
orchestrator profile (step 7) was created, or if `~/.codex/orchestrator.config.toml` already sets
`network_access = true` under `[sandbox_workspace_write]` (only that setting is read); otherwise
start plain `codex`. Either way, a Codex session can orchestrate only once the Codex commands
rule (step 8) exists, because Codex's sandbox blocks the orchestrator's background service, and
only when the session starts in the project's root folder. Paths
accept a leading `~`, `$HOME`, or surrounding quotes, and the wizard offers the `$HOME` match
when a path with a lost `~` does not exist.

### How setup keeps your authority away from the agents

The AI agents this sets up run as your user, so the setup tools assume an agent may have
changed anything it can write: the project's working tree and the temporary folders.

- **Trusted Python, isolated.** Every Python the wizard starts is the real file behind the
  `python3` on your `PATH`, never a symlink, a virtual environment's copy, or one in a temporary
  folder or the project, and it runs with `-I -B -S` so nothing in the working folder, the
  environment or site-packages loads. Use a system Python such as Homebrew's or python.org's.
- **Private working files.** Logs, the install plan, the key being enrolled and the statement
  being signed live in a fresh owner-only folder in your per-user cache folder (macOS) or
  `$XDG_RUNTIME_DIR` (Linux; setup refuses without it), never in `/tmp` or `$TMPDIR`.
- **Only this release's orchestrator runs.** Before running the project's orchestration CLI,
  setup and `attest.py` check its runtime files against this repository's copy byte for byte
  (same version, identical files, no extra modules) and refuse otherwise; it then runs isolated
  and never reads cached bytecode from the project.
- **You sign only what you approved.** `attest.py` sends the statement on standard input,
  rebuilds the exact bytes to sign from your answers, refuses to sign anything else, checks the
  signature covers those bytes, and after enrolling a key checks that the configured key is the
  one it showed you.
- **No repository hooks.** Git commands on the project run with
  `-c core.hooksPath=/dev/null -c core.fsmonitor=false`.
- **Claude primaries cannot run owner commands.** The installer merges Claude Code permission
  deny rules into the project's `.claude/settings.json` (and records what it added in
  `.claude/agent-orchestration-permissions.json`): two `Bash` rules for each owner-only CLI
  subcommand (registration, billing attestation, owner keys and grants, backups and deletion,
  canaries, promotion and rollback, upgrade reconciliation, the Codex rule commands and every
  other command that is not one of the 23 primary-host commands), in the forms
  `Bash(*.codex/orchestration/cli.py <command>)` and `Bash(*.codex/orchestration/cli.py <command> *)`,
  plus `Edit` rules for `.codex/**`, `.agents/**`, `.claude/settings*.json` and `.git/**`.
  Existing settings are kept; `verify` reports missing rules and `remove` takes out only the
  entries install added. These rules are a policy layer on Claude Code's tools, not a sandbox: a
  command spelled another way (for example through `bash -c` or a script that starts the CLI
  itself) is not matched, so do not give a Claude primary more trust because of them.

### Advanced: manual setup

The wizard runs these commands. The installer also needs the project's
`.codex/orchestration/project-agent-routing.json` and `operational-checks.json`, passed with
`--project-agent-routing` and `--operational-checks` on the first install.

**Operational checks recipe.** In the project, write two small wrapper scripts (one runs your
tests, one runs a secret scanner), commit them, then compute the hashes. The wrapper
must be committed, because the installer checks that the file matches a clean commit. The security
example uses `gitleaks`, which must be installed (`brew install gitleaks`, or download it from the
project's release page); any equivalent secret scanner works the same way.

```sh
cd /path/to/project
printf '#!/usr/bin/python3\nimport subprocess, sys\nsys.exit(subprocess.call(["python3", "-m", "unittest", "discover"]))\n' > qa-check
printf '#!/usr/bin/python3\nimport subprocess, sys\nsys.exit(subprocess.call(["gitleaks", "detect", "--no-banner", "--redact"]))\n' > qa-security
chmod 700 qa-check qa-security
git add qa-check qa-security && git commit -m "Add QA check wrappers"
INTERP=$(shasum -a 256 /usr/bin/python3 | cut -d' ' -f1)   # sha256sum on Linux
check() { # check ID WRAPPER
  printf '{"check_id": "%s", "driver": "python-wrapper", "argv": ["./%s"], "executable_sha256": "%s", "interpreter_sha256": "%s", "runtime_tree": null, "timeout_seconds": 600}' \
    "$1" "$2" "$(shasum -a 256 "$2" | cut -d' ' -f1)" "$INTERP"
}
printf '{"version": 1, "deterministic_checks": [%s], "security_checks": [%s]}\n' \
  "$(check tests qa-check)" "$(check security qa-security)" > ../operational-checks.json
```

Pass `../operational-checks.json` to the wizard when it asks, or to the installer with
`--operational-checks`. The format is the one used by `tests/test_install.py` and described by
`packages/codex-agents/native-package/.codex/orchestration/schemas/operational-checks.schema.json`.

1. **Install** into the top of a Git project. Without `--apply` the command only previews:

   ```sh
   python3 -I -B -S scripts/install.py install --target /path/to/project --apply
   python3 -I -B -S scripts/install.py verify --target /path/to/project
   ```

   Commit the installed files in the project, including `.claude/settings.json` and
   `.claude/agent-orchestration-permissions.json`. To uninstall, run
   `python3 -I -B -S scripts/install.py remove --target /path/to/project --apply`: it first removes
   the Codex commands rule through the project's `remove-codex-rule` command (an older installed
   version without that command makes it stop and name the rule file to delete), then the
   permission entries it added, then both packages.
2. **Enable providers.** Commit `.codex/orchestration/provider-routing.json` in the project. It
   says which providers and models children may use. Without it, only Codex children are
   allowed.
3. **Register the learning collector** once, from the project:

   ```sh
   python3 -I -B -S .codex/orchestration/cli.py learning-register --project-root "$PWD"
   ```

4. **Attest billing.** This is a one-time human step:

   ```sh
   python3 -I -B -S scripts/attest.py --target /path/to/project
   ```

   No child runs until you confirm that each account cannot be charged beyond its subscription.
   The command explains each question in plain language:
   - It creates an owner key if you don't have one. Give it a passphrase, so no program,
     including an AI agent, can sign in your name.
   - It asks whether pay-as-you-go extra usage and Codex credit spending are turned off.
   - It has you sign each answer.

   If the owner key is already configured, it finds the key file at `~/.ssh/codex-agents-owner`
   by itself and only asks for a file location when it cannot (never for the passphrase there).
   To change the key's passphrase, choose "Change the owner key passphrase" in `./setup.sh`, or run
   `python3 -I -B -S scripts/attest.py --change-passphrase`.

   Run it again whenever your billing settings change.
5. **Optional: let a Codex session orchestrate Claude children.** Codex runs commands without
   network by default, so a Codex primary cannot reach Claude. Create
   `~/.codex/orchestrator.config.toml`:

   ```toml
   sandbox_mode = "workspace-write"

   [sandbox_workspace_write]
   network_access = true
   ```

   Then start orchestrating sessions with `codex -p orchestrator`. Ordinary sessions keep network
   off. Without this profile, start plain `codex`.
6. **Let a Codex session reach the orchestrator.** Codex's sandbox blocks the learning
   collector, so a Codex primary needs a Codex command rule that lets only this project's
   everyday orchestrator commands run outside the sandbox. From the project:

   ```sh
   python3 .codex/orchestration/cli.py install-codex-rule --project-root "$PWD"
   ```

   It writes `${CODEX_HOME:-~/.codex}/rules/agent-orchestration-<digest>.rules` and prints
   its path; `--print` shows the rule without writing it. The rule names the resolved base
   interpreter of the `python3` that ran it, with the isolation flags `-I -B -S`, so use the
   one your Codex sessions find first on `PATH`, and run it again after a Python upgrade. It
   refuses an interpreter inside the project, in a temporary directory or writable by
   another user, and a project in a temporary folder. Start Codex sessions in the project's
   root folder, where Codex keeps `.codex` read-only. Delete the file to undo it.

## Development

```sh
git config core.hooksPath .githooks          # enable the identifier guard on commit
python3 scripts/check_identifiers.py         # scan tracked files
python3 -m unittest discover -s tests -v     # run the repository tests
(cd packages/codex-agents && python3 -m unittest discover -s tests)
(cd packages/claude-agents && PYTHONPATH=src python3 -m unittest discover -s tests)
```

This repository is public. The identifier guard blocks commits that contain personal or machine
identifiers, or that use a non-noreply commit email.

## License

[Apache License 2.0](LICENSE). See [NOTICE](NOTICE).
