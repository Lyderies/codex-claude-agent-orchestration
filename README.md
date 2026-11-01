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
   protected copy of the orchestrator in `~/.codex` that sessions opened on a project cannot
   change. Never open Codex on your home folder, its parent or `/` (or add `~` or `~/.codex`
   with `--add-dir`) while the rule exists. Only the real `~/.codex` is supported, not a
   `CODEX_HOME` elsewhere or a symlinked `~/.codex`. Delete the rule file, or run the remove
   command, to undo it;
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
changed anything it can write: the temporary folders, and any folder a Codex session was
opened on, which may be a project or a folder above it (a nested project's tree, `.git` and
`.claude` included). Sessions are never opened on your home folder, its parent or `/`.
**Keep this repository's folder outside every folder you open in Codex**, so no session can
change the code that runs with your authority.

- **Trusted Python, decided before it runs.** Every Python the wizard starts is the real file
  behind the `python3` on your `PATH`, checked before it ever runs: never a symlink, a virtual
  environment's copy, macOS's `/usr/bin/python3` shim, one in a temporary folder or the
  project, or one outside the runtime's trusted installation folders (system folders,
  Homebrew, python.org, the Command Line Tools, `/Applications`, Nix, or a hidden top-level
  folder of your account's home folder such as `~/.pyenv` or `~/.local`), so a sibling install
  like `/srv/work/tools` is refused. Owner tools and the runtime use one list. It runs with
  `-I -B -S -X pycache_prefix=/dev/null`, so nothing in the working folder, the environment,
  site-packages or a bytecode cache loads. Use a system Python such as Homebrew's or
  python.org's.
- **Private working files.** Logs, the install plan and a snapshot of the operational-checks
  file you chose (the preview and the install both read that snapshot) live in a fresh
  owner-only folder in your per-user cache folder (macOS) or `$XDG_RUNTIME_DIR` (Linux; setup
  refuses without it, or when it is under your home folder), never in `/tmp` or `$TMPDIR`.
- **Owner commands run this repository's code.** Registration, the collector status, the
  billing attestation and the Codex rule commands run this repository's own orchestrator CLI
  with `--project-root`, never the project's installed copy. The steps that depend on the
  install first check that it is this release, byte for byte, with nothing extra (no module,
  package, bytecode or link of any name) in `.codex/orchestration`.
- **You sign only what you approved.** `attest.py` rebuilds the exact bytes to sign from your
  answers and refuses anything else. The statement goes to `ssh-keygen` on standard input, the
  signature comes back on standard output and is checked against those bytes, and the key being
  enrolled and the signature reach the orchestrator through pipes: nothing touches disk. After
  enrolling, the owner key record must hold exactly one key, the one it showed you.
- **Git plumbing only.** Git is a real binary at a fixed location (never macOS's
  `/usr/bin/git` shim, never one on a search path an agent could change). Setup and the
  installer never run `status`, `add` or `commit` on the project: they read its state with
  `ls-files` and `ls-tree` and compare file bytes themselves, and commit with
  `hash-object -w --no-filters`, `update-index`, `write-tree`, `commit-tree` (never signed)
  and `update-ref` with the old value, so no filter, textconv, hook, signing program, pager or
  maintenance can run whatever the project's settings say. Every Git process also runs with
  `-c core.hooksPath=/dev/null -c core.fsmonitor=false -c commit.gpgSign=false
  -c tag.gpgSign=false -c gc.auto=0 -c maintenance.auto=false`, no network transport
  (`GIT_ALLOW_PROTOCOL=""`) and no lazy fetching (`GIT_NO_LAZY_FETCH=1`); a partial clone or a
  promisor remote is refused. As a second layer, setup still stops when the project's own
  settings name a program Git would start (a filter, diff or merge driver, pager, editor, SSH,
  signing or credential helper, or an include); commit by hand in that case.
- **Claude primaries cannot run owner commands.** The installer merges Claude Code permission
  deny rules into the project's `.claude/settings.json` (and records which ones it added in
  your learning state folder, outside the project, so nothing in the project can claim a rule
  for it): two `Bash` rules for each owner-only CLI
  subcommand (registration, billing attestation, owner keys and grants, backups and deletion,
  canaries, promotion and rollback, upgrade reconciliation, the Codex rule commands and every
  other command that is not one of the 23 primary-host commands), in the forms
  `Bash(*.codex/orchestration/cli.py <command>)` and `Bash(*.codex/orchestration/cli.py <command> *)`,
  plus `Edit` rules for `.codex/**`, `.agents/**`, `.claude/settings*.json` and `.git/**`.
  Existing settings are kept; `verify` reports missing rules and `remove` takes out only the
  entries that record says install added (an entry it did not record is never removed). The
  new settings file is written in that owner-only state folder and renamed into place, after
  checking that the project's copy has not changed since it was read. These rules are a policy layer on Claude Code's tools, not a sandbox: a
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
   python3 -I -B -S -X pycache_prefix=/dev/null scripts/install.py install --target /path/to/project --apply
   python3 -I -B -S -X pycache_prefix=/dev/null scripts/install.py verify --target /path/to/project
   ```

   Commit the installed files in the project, including `.claude/settings.json` (an earlier
   version's `.claude/agent-orchestration-permissions.json` is migrated to your learning state
   and deleted). To uninstall, run
   `python3 -I -B -S -X pycache_prefix=/dev/null scripts/install.py remove --target /path/to/project --apply`:
   it first removes the Codex commands rule and its protected copy with this repository's own
   `remove-codex-rule` (whatever version the project has installed), then the permission
   entries it added, then both packages.
2. **Enable providers.** Commit `.codex/orchestration/provider-routing.json` in the project. It
   says which providers and models children may use. Without it, only Codex children are
   allowed.
3. **Register the learning collector** once, from the root of this repository, with this
   repository's own orchestrator (owner commands never run the project's copy):

   ```sh
   python3 -I -B -S -X pycache_prefix=/dev/null packages/codex-agents/native-package/.codex/orchestration/cli.py learning-register --project-root /path/to/project
   ```

4. **Attest billing.** This is a one-time human step:

   ```sh
   python3 -I -B -S -X pycache_prefix=/dev/null scripts/attest.py --target /path/to/project
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
   `python3 -I -B -S -X pycache_prefix=/dev/null scripts/attest.py --change-passphrase`.

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
   everyday orchestrator commands run outside the sandbox. From the root of this repository:

   ```sh
   python3 -I -B -S -X pycache_prefix=/dev/null packages/codex-agents/native-package/.codex/orchestration/cli.py install-codex-rule --project-root /path/to/project
   ```

   It writes `~/.codex/rules/agent-orchestration-<digest>.rules` and a protected copy of the
   orchestrator's runtime files under `~/.codex/agent-orchestration/`, and prints both;
   `--print` shows the rule without writing it. The rule runs that copy, never the project's
   own `cli.py`, with the resolved base interpreter of the `python3` that ran it and the
   isolation flags `-I -B -S -X pycache_prefix=/dev/null`, so use the one your Codex
   sessions find first on `PATH`, and run it again after a Python upgrade or a package
   update. It refuses an interpreter inside the project, in a temporary directory or
   writable by another user, a project in a temporary folder, a project whose
   `.codex/orchestration` holds files the package does not install (such as bytecode), and
   a Codex home other than the real `~/.codex`. Every Codex session loads the rule, so never
   open Codex on your home folder, its parent or `/` while it exists. Undo it with the same
   command and `remove-codex-rule --project-root /path/to/project`.

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
