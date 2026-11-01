# Setup wizard reference

`./setup.sh [/path/to/project]`, run from the root of this repository, is a terminal wizard: arrow keys
move, Enter selects, Space toggles and `q` quits. It needs an interactive terminal and refuses to run
piped or scripted, because the billing attestation is a human step. Full command output goes to a
private log, shown only if something fails. `NO_COLOR=1` gives plain output. Paths accept a leading `~`,
`$HOME` or surrounding quotes. The commands each step runs are listed in [manual setup](manual-setup.md).

## Steps

1. **Checks.** The OS (macOS or Linux; on Linux, verify reports not ready because operational checks need
   macOS), bash, a trusted `python3` 3.11 or later, a trusted Git, both sign-ins, and that no
   `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` is set. Each client is found and run only as the orchestrator
   runs it (from `/opt/homebrew/bin`, `/usr/local/bin`, `/usr/bin`, `/bin` or `~/.local/bin`, its file
   checked first); one elsewhere on your `PATH` is reported, never run. It also scans Codex's session
   records from the last 14 days and warns, with Continue or Quit, about a session that could write this
   repository, your orchestrator records, your home folder or a folder above them.
2. **Project.** The top folder of a Git working tree. Nothing changes; it previews the install. A project
   that has never been set up needs a prepared `operational-checks.json` (see the
   [recipe](manual-setup.md#operational-checks)); the wizard asks for its path and works from a private
   snapshot of it. An existing install offers: update the install and walk through every step, verify the
   install only, re-run the billing attestation only, change the owner key passphrase, choose another
   path, or quit.
3. **Providers.** Codex children, Claude children and which Claude models, and "Let Codex sessions
   orchestrate Claude children". An existing `provider-routing.json` can be kept.
4. **Install.** Installs, writes `provider-routing.json`, and offers to commit exactly the files the
   installer reported plus `provider-routing.json`, listing only files that really changed (with none it
   says "Nothing to commit" and skips the question). The commit uses Git plumbing only and is never
   pushed. The summary says `committed`, `nothing to commit`, `not committed (you chose to commit
   yourself)` or `commit failed`, and lists anything left uncommitted, including files `.gitignore` hides.
   Then it verifies the install.
5. **Collector.** Registers the project with the one local learning collector shared by every project on
   the machine, and starts it. Your orchestrator records (the ledger, the owner key record and the
   collector's files) live in `~/.local/state/codex-agents` (or `$XDG_STATE_HOME/codex-agents`); on macOS
   the first registration after this update moves them there from
   `~/Library/Application Support/codex-agents` and says so once.
6. **Billing.** Hands the terminal to `scripts/attest.py`; the wizard never touches its screens.
7. **Profile.** Only when Codex sessions may orchestrate Claude children: creates
   `~/.codex/orchestrator.config.toml` with network on, for sessions started with `codex -p orchestrator`.
   An existing profile is compared by its effective settings and never overwritten.
8. **Commands.** Only when Codex sessions orchestrate the project (Codex children, or bridging to Claude):
   offers the Codex commands rule ("Yes, allow these commands"). Every Codex session on the machine loads
   it, and it runs a protected copy of the orchestrator in `~/.codex`. A stale rule is left unchanged
   unless you choose "Replace it". A Codex home other than the real `~/.codex` is reported as
   unsupported, with its reason and the fix.
9. **Done.** A status line per step, how to start in each client, and the remove command.

After a step that did not finish, the choices are Try again, Skip for now (in a full run), Back to the
main menu or Quit. The final screen offers Exit or Back to the main menu, and saving a summary of what
failed when something did; the summary defaults to your home folder and the wizard asks before saving it
inside a Git working tree. Only Exit or Quit ends the wizard.

## Where to open Codex

Open Codex on the project itself; this repository is only for running `./setup.sh`. Never open Codex on
a folder that contains a set-up project, this repository, your home folder, a folder above it or `/`, and
never add `~`, `~/.codex` or `~/.local` with `--add-dir`. A folder above a project is never safe: Codex
runs a nested trusted project's `.codex/config.toml` and hooks outside the sandbox, and keeps a project's
`.codex`, `.agents` and `.git` read-only only when the project is the top of the session's folder, so a
session opened above it could plant them. Before each command the rule runs, the orchestrator reads the
calling session's own Codex record (only its settings, never the conversation) and refuses a session that
runs without its sandbox, left no record (`--ephemeral`), was opened on a folder that contains the
project, or can write your home folder, a folder above it, `~/.codex`, this repository, your records in
`~/.local/state/codex-agents`, the private working folders or a tool folder (`~/.local`, `/opt/homebrew`
and the like, including one added with `--add-dir`). In a Codex session, `host-command` prints that verdict
as `session_guard`. A refused session's commands stop, but what it already wrote stays.

## How setup keeps your authority away from the agents

The AI agents this sets up run as your user, so the setup tools assume an agent may have changed anything
it can write: the temporary folders, and any folder a Codex session was opened on, which may be a project
or a folder above it (a nested project's tree, `.git` and `.claude` included).

- **Trusted Python, decided before it runs.** Every Python the wizard starts is the real file behind the
  `python3` on your `PATH`, checked before it ever runs: never a symlink, a virtual environment's copy,
  macOS's `/usr/bin/python3` shim, one in a temporary folder or the project, or one outside the runtime's
  trusted installation folders (system folders, Homebrew, python.org, the Command Line Tools,
  `/Applications`, Nix, or a hidden top-level folder of your account's home folder such as `~/.pyenv` or
  `~/.local`), so a sibling install like `/srv/work/tools` is refused. Owner tools and the runtime use
  one list. It runs with `-I -B -S -X pycache_prefix=/dev/null`, so nothing in the working folder, the
  environment, site-packages or a bytecode cache loads.
- **Clients and basic tools.** The wizard runs with `/bin/bash` and finds its basic tools only in the
  system folders, so a program planted in an earlier `PATH` entry (say `~/Work/tools/bin`, which a session
  opened on `~/Work` could write) never runs; your `PATH` is used only to find `python3`, which is checked
  as above. The Codex and Claude sign-in checks go through the runtime's own client lookup: the client is
  found only in `/opt/homebrew/bin`, `/usr/local/bin`, `/usr/bin`, `/bin` or `~/.local/bin`, its real file
  must pass the same location checks as the interpreter before it runs, and it runs with the runtime's
  minimal environment (no `CODEX_HOME` or API keys) in a private working folder. A client that fails is
  reported in plain words and never run.
- **Private working files.** Logs, the install plan and a snapshot of the operational-checks file you
  chose live in a fresh owner-only folder in your per-user cache folder (macOS) or `$XDG_RUNTIME_DIR`
  (Linux; setup refuses without it, or when it is under your home folder), never in `/tmp` or `$TMPDIR`.
- **Owner commands run this repository's code.** Registration, the collector status, the billing
  attestation and the Codex rule commands run this repository's own orchestrator CLI with
  `--project-root`, never the project's installed copy. The steps that depend on the install first check
  that it is this release, byte for byte, with nothing extra (no module, package, bytecode or link of any
  name) in `.codex/orchestration`.
- **You sign only what you approved.** `attest.py` rebuilds the exact bytes to sign from your answers and
  refuses anything else. The statement goes to `ssh-keygen` on standard input, the signature comes back on
  standard output and is checked against those bytes, and the key being enrolled and the signature reach
  the orchestrator through pipes: nothing touches disk. After enrolling, the owner key record must hold
  exactly one key, the one it showed you. The private key is used only from your own `~/.ssh` (your home
  folder as the system account database names it): no symbolic link anywhere on its path, `~/.ssh` yours
  and writable by no one else. That is checked again before every use, so no other program can swap the
  file between the checks and the signing; a key anywhere else is refused.
- **Git plumbing only.** Git is a real binary at a fixed location (never macOS's `/usr/bin/git` shim,
  never one on a search path an agent could change). Setup and the installer never run `status`, `add` or
  `commit` on the project: they read its state with `ls-files` and `ls-tree` and compare file bytes
  themselves, and commit with `hash-object -w --no-filters`, `update-index`, `write-tree`, `commit-tree`
  (never signed) and `update-ref` with the old value, so no filter, textconv, hook, signing program, pager
  or maintenance can run whatever the project's settings say. Every Git process also runs with hooks, the
  file-system monitor, signing and automatic maintenance off, no network transport
  (`GIT_ALLOW_PROTOCOL=""`) and no lazy fetching (`GIT_NO_LAZY_FETCH=1`); a partial clone or a promisor
  remote is refused. As a second layer, setup still stops when the project's own settings name a program
  Git would start (a filter, diff or merge driver, pager, editor, SSH, signing or credential helper, or an
  include); commit by hand in that case.
- **Claude primaries cannot run owner commands through Claude Code's tools.** The installer merges deny
  rules into the project's `.claude/settings.json` and records which ones it added in your orchestrator
  records folder, outside the project, so nothing in the project can claim a rule for it: two `Bash` rules
  for each owner-only CLI subcommand (every command that is not one of the 23 primary-host commands), in the
  forms `Bash(*.codex/orchestration/cli.py <command>)` and `Bash(*.codex/orchestration/cli.py <command> *)`,
  plus `Edit` rules for `.codex/**`, `.agents/**`, `.claude/settings*.json` and `.git/**`. Existing
  settings are kept; `verify` reports missing rules and `remove` takes out only the entries that record
  says install added. The new settings file is written in that owner-only folder and renamed into place,
  after checking that the project's copy has not changed since it was read. These rules are a policy layer
  on Claude Code's tools, not a sandbox: a command spelled another way (for example through `bash -c` or a
  script that starts the CLI itself) is not matched, so do not give a Claude primary more trust because of
  them.
