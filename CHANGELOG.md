# Changelog

## 0.2.0: cross-provider read-only routing (G2)

- **G2 v1 runtime hardening (0.8.9).** Fixes from two whole-release security reviews.
  - The Codex command rule no longer runs the project's `.codex/orchestration/cli.py`,
    which a session opened on a parent folder could rewrite. `install-codex-rule` copies
    exactly this package's runtime files (a constant of the installing code, never the
    project's manifest), verified byte for byte and against this package's recorded
    runtime, to an owner-only `~/.codex/agent-orchestration/<project>/<runtime>/` copy
    bound to its project, and the rule names that copy. The runtime digest, also the
    collector's identity in `runtime-releases.json`, now covers every file the copy holds
    (configuration and schemas included). Setup refuses a project whose
    `.codex/orchestration` holds anything else (a planted `socket.py`, a package, bytecode,
    an extension module), and the copy refuses an extra entry of its own before its folder
    is importable. Only the real `~/.codex` is supported: a `CODEX_HOME` elsewhere or a
    symlinked `~/.codex` is refused, and `host-command` reports it as `unsupported`. The copy serves only its bound project, only
    the primary-host commands, and only while the project's installed files still match
    it. A project without an install manifest gets no rule. `host-command` reports
    `installed` only when rule, copy and project all match (earlier rules are `stale`),
    and its `argv_prefix` names the copy. New owner command `remove-codex-rule
    --project-root <root>` removes the rule and the copy and prints
    `{"ready": true, "removed": [...]}` (an empty list when there is nothing, for any
    project, including one in a temporary folder).
  - The supported threat model is stated plainly in the wizard, README, design and Codex
    skill: while the rule exists, no Codex session may use the home folder, its parent or
    `/` as its workspace, or add `~` or `~/.codex` with `--add-dir`; home-installed tools
    such as Claude Code stay supported under that rule.
  - Every Python launched outside the sandbox (the rule's prefix, the collector, the
    deadline watchdog, the learning hooks and the Claude skill's commands) adds
    `-X pycache_prefix=/dev/null`: `-B` only stops writing
    bytecode, and CPython still loaded a planted `.pyc` from `__pycache__`. Rules without
    it are `stale`, and a copy holding any file its manifest does not list, bytecode
    included, fails verification.
  - Git runs from a real binary (Command Line Tools, Xcode or Homebrew on macOS, never
    the `/usr/bin/git` xcrun shim whose cache sits in `$TMPDIR`) outside the inspected
    repository, with `xcrun_nocache=1` for git, provider clients and operational checks.
  - A provider client is refused unless its resolved file passes the same location checks
    as the interpreter, outside the project and every temporary directory, before it
    runs.
  - A worker result kept for its verifier is deleted when its orchestration closes, and
    seven days after its outcome at most: each ledger start clamps older expiries (90 days
    before) and sweeps, the collector sweeps every minute, and an expired result is never
    read. Only digests stay.
  - Every Codex child is bridged (`codex exec --ignore-rules`, read-only), from a Codex
    primary too, so no child shares the primary's command rule. Codex routes that need
    write access are not eligible, and the native-only legacy fallback is refused for
    bridged routes.
  - `dispatch` fences a child result that does not validate and binds a keyed digest of
    the result it returns, with its terminal state, to the run; recording any other
    result, or dropping or inventing the fence, is refused.
  - Both bridged launches pass `--` before the prompt, so an objective can never be read
    as a client option.
  - Bridged children receive each required criterion's description, not only its id, and
    a bridged Codex child receives its routed role's developer instructions (as
    `-c developer_instructions=...`), refused unless they match the run's recorded
    execution configuration.
- **Owner-side hardening (v1 security review).**
  - `setup.sh` and `scripts/attest.py` keep every working file in a fresh owner-only folder in
    the per-user cache folder (macOS) or `$XDG_RUNTIME_DIR` (Linux, refused when unset), created
    through no-follow, ownership-checked descriptors; nothing goes to `/tmp` or `$TMPDIR`, where
    a sandboxed agent could swap the enrolled key, the statement or the install plan.
  - `attest.py` passes the billing grant on stdin, rebuilds the signing payload from the approved
    statement (the runtime's `authenticated_owner_record` construction) and refuses any other
    bytes, checks with `ssh-keygen -Y check-novalidate` that the signature covers them, and after
    enrolment refuses unless the configured owner key is the one it showed.
  - Every Python the wizard starts, `attest.py` included, is the real file behind `python3` on
    `PATH` (refused in a temporary folder, the project or a virtual environment) and runs with
    `-I -B -S`; `attest.py` imports only the standard library. The project's CLI runs only after
    its runtime files match this repository's copy byte for byte (same version, no extra
    modules), with `-X pycache_prefix=/dev/null` so a planted `__pycache__` file is never loaded.
  - Git on the project runs with `-c core.hooksPath=/dev/null -c core.fsmonitor=false`
    (`--no-optional-locks` for status); the wizard says its commit skips the project's hooks.
  - `scripts/install.py remove` first removes the Codex commands rule and its protected runtime
    copy through the project's `remove-codex-rule`; an installed CLI without that command makes
    it stop and name the rule file to delete. The Commands step now says every Codex session
    loads the rule, which runs a protected copy, and never to use the home folder's parent or `/`
    as a Codex workspace.
  - The installer merges Claude Code permission deny rules into `.claude/settings.json` for every
    owner-only CLI subcommand (the parser's commands minus the 23 primary-host ones) and `Edit`
    rules for `.codex/**`, `.agents/**`, `.claude/settings*.json` and `.git/**`, keeping user
    entries; `verify` reports missing rules and `remove` takes out only what install added. These
    rules are a policy layer, not a sandbox.
- **Bridged child containment (0.8.8).**
  - Bridged Codex children run with `--ignore-rules`: `--ignore-user-config` skips only
    `config.toml`, so a child still loaded the owner's execpolicy rules, and the setup-written
    rule would have let it run the primary-host commands, `dispatch` among them, outside its
    sandbox. A client whose `codex exec --help` lacks the option is refused before launch.
    `approval_policy="never"` is pinned and `skill_mcp_dependency_install` is disabled.
  - Codex system or managed configuration (`/etc/codex`, `~/.codex/managed_config.toml`, macOS
    managed preferences) refuses a Codex dispatch (`codex_managed_config_present`).
  - Cleanup reaches descendants outside the child's process group, conservatively: a
    fixed-path `ps` lists candidates, and a process is adopted only when its own kernel record
    (parent, group and birth time read together) names a tracked parent whose record still
    matches; every SIGKILL is preceded by a fresh record read, and the child's own group is
    killed before the child is reaped. The
    deadline watchdog verifies the child's identity before the run continues and tracks the
    tree too. A process that detaches between snapshots is left running in the sandbox rather
    than risk killing an unrelated one.
  - Native Codex children share the primary's session and its command rule; the design's
    residual risks and the Codex skill now say so.
- **Setup wizard retry.** A wrong owner-key passphrase no longer stops `scripts/attest.py`: it says
  "That passphrase did not unlock the key." and offers Try again or Exit, for the key check, the
  signing and the passphrase change (other failures still stop). In `setup.sh`, no failed step
  ends the wizard: after a failure the choices are Try again, Back to the main menu (the
  project menu) or Quit, the final summary also offers Back to the main menu, and only Exit or
  Quit ends the script. Saving a summary returns to the final menu, and going back starts the
  next attempt from a clean state.
- **Route-first model guidance.** The Codex skill and its routing reference no longer summarize
  per-family models, which had drifted from policy v5's provider candidates; they defer to the
  route's provider, model and effort.
- **Codex agent paths bind (0.8.7).** Codex 0.164 names a spawned child by its agent path
  (for example `/root/<task_name>`); `learning-bind-host-agent` accepts slash-separated paths of
  the same bounded tokens, and the Codex skill says to bind that path.
- **Setup wizard polish.**
  - The commit step lists and commits only files that really changed; with none it says
    "Nothing to commit" and skips the question. An existing install is described as an update.
  - `scripts/attest.py` finds the owner key at its default location without asking, and an entry
    that is not a file location is refused without being echoed. `--change-passphrase` (also in
    the wizard's existing-install menu) changes the owner key passphrase through `ssh-keygen -p`.
  - Key-location prompts are read with echo off, `ssh-keygen` never receives `SSH_ASKPASS`, the
    standalone passphrase change refuses an unsafe or temporary state folder and checks the key
    before changing it, and the commit step reports files that `.gitignore` hides and keeps a
    rename's deleted source.
  - The Codex profile step compares effective settings, so a profile with comments and the same
    settings is reported as matching, and a differing setting is named.
  - The saved failure summary defaults to the home folder and asks before saving into a Git
    working tree.
- **Provider-qualified routes.**
  - Policy v5 lists Codex and Claude models and roles.
  - Each family and route key has an ordered list of provider candidates, matching
    `docs/routing-table.md`, and a table-driven test fails CI on drift.
  - Selection is the first candidate that passes the static eligibility checks; there is no
    silent fallback.
  - Routes carry the provider, the dispatch path and the provider configuration digest, and
    replay exactly.
- **Project provider configuration.** `.codex/orchestration/provider-routing.json` enables
  providers and models, and controls whether a Codex primary may bridge to Claude. It must be
  committed and clean.
- **Admission gate.** Fails closed, and its decision is bound to the run. It denies:
  - API-key and alternate-backend environments;
  - an unreviewed client version;
  - an identity older than 30 minutes or a non-subscription plan;
  - a missing owner billing attestation;
  - stale, unknown or paid-usage billing evidence;
  - exhausted headroom (90% of a window, or a rejection);
  - a per-account run-count cap.

  Billing, headroom and bootstrap evidence apply account-wide, across projects.
- **Dispatch bridge.** Starts the other provider's client for a read-only child.
  - The workspace holds only the committed bytes of granted paths.
  - The Claude profile is locked.
  - The Codex profile is locked: account connectors, web search, image generation and goals are
    off, and an event allowlist detects anything else.
  - Every run has a durable intent, a hard deadline and no resend.
  - Billing is re-checked at launch, and a run stops on in-run overage or rejection.
  - Bridged runs bind and commit only their own dispatch.
- **Claude primary.** A `claude-host-v1` host client and a Claude orchestration skill, installed
  with the Codex package.
  - Host detection and per-orchestration host ownership.
  - Native Claude spawn parameters are computed by the CLI.
  - A gated billing probe supplies the evidence native Claude children cannot.
- **Observations.** Headroom, billing and usage records are bound to their run and phase. A failed
  reading is recorded instead of dropped.
- **Codex credits attestation.** An owner billing attestation for Codex may carry
  `codex_credits_spending_disabled: true`, which lets a reading of credits that are not unlimited
  be admitted, at admission and at the launch re-check. `unlimited` credits still deny.
  `spendControlReached` is now recorded and denies as `codex_spend_control_reached`.
- **Package version.** The Codex package is now 0.8.0. It had kept the imported 0.7.2, so an existing
  collector authorized for 0.7.2 refused the changed build as a conflict instead of upgrading.
- **Release guard.** A collector authorizes exactly one build per package version, so every runtime
  change needs a version bump. `packages/codex-agents/runtime-releases.json` records the runtime digest
  for each version, and a test fails when the code changes without a new version and entry. The
  package is now 0.8.1.
- **Collector bytecode.** The detached learning collector runs in isolated mode, which ignores
  `PYTHONDONTWRITEBYTECODE`, so it wrote `__pycache__` into the project's managed `.codex/` paths.
  It now starts with `-B`. The package is 0.8.2.
- **CI.** The checkout and Python setup actions move to releases that run on Node 24.
- **Verifier handoff and lifecycle fixes (0.8.3).** A verification task may set
  `verification_for_outcome` to the worker's `host_outcome_digest`; the router checks it is
  this orchestration's outcome from the named provider. A bridged verifier gets the worker's
  ledger-recorded result read-only at `.orchestration-input/worker-result.json`; a native
  verifier gets it from `learning-open-host-run`. `learning-record-host-result` prints the
  `host_outcome_digest` that operational QA takes, a bridged run's outcome carries the
  bridge's recorded usage, `provider-observe` shows `paid_usage_waived_by_attestation`, and
  both skills give the exact QA commands with every argument.
- **Claude verifiers pass operational QA (0.8.4).** QA accepted only the Codex verification
  role, so a native Claude verifier of Codex work could never pass. It now accepts the routed
  verification role of either provider, bound to the verifier run's admitted provider; a
  Claude verifier must be the other provider than the worker.
- **Review fixes (0.8.5).** A verifier's open now stores the worker outcome it was bound to and
  refuses an assignment whose context names any other; operational QA accepts only a verifier
  opened for its exact worker outcome. A verification-required result too large to keep as
  canonical JSON is refused before the run is consumed, instead of being recorded without
  the copy its verifier needs.
- **Codex host command boundary (0.8.6).** A Codex primary runs the CLI inside Codex's
  sandbox, which blocks the learning collector, so every collector-backed command failed.
  A new wizard step, Commands, offers a per-project Codex command rule in the user's rules
  layer. It lets only the primary-host subcommands, run with an exact interpreter, flags
  and project `cli.py`, run outside the sandbox; owner commands stay sandboxed.
  `host-command` reports that exact command prefix and whether the rule is installed, and
  the Codex skill runs it first and stops until the rule is in place. Because the rule
  cannot constrain later arguments, an installed CLI pins these commands to its own
  project, the default learning state and its installed policy, refusing any other
  `--project-root`, `--root`, `--state-root` or `--policy` before it runs git or reaches
  the collector. Nothing such a command runs or trusts comes from a sandbox-writable place:
  the rule names the resolved, trusted base interpreter with `-I -B -S` (never a virtual
  environment's, a project's or a temporary one), and the collector and watchdog it starts
  get the same flags; a project in a temporary directory never gets a rule; trust checks
  use git plumbing only, with every repository-configured helper and every transport off,
  and refuse a shared repository inside the working tree or in a temporary directory and
  a partial clone; bridge exports, control files and child, probe, operational-check and
  provider-client temporary directories move from the system temp directory to owner-only
  host directories that overlap no project or temporary directory. A new rule never overwrites one that
  another setup wrote meanwhile. A sandboxed collector failure now carries a hint. The
  result template and the bridged child's prompt state one evidence entry per criterion,
  at its most severe outcome, so neither a child nor a repair can soften a `fail`. The
  orchestration report's `runs.native_agent_bound` is now `agent_bound`, because it counts
  bridged runs too.

Security: two whole-release reviews by two vendors, over two rounds, found 14 defects; all were
fixed. A separate review of the Codex lockdown narrowed the reviewed Codex client range to the
build where containment was shown live.

## 0.1.0: import and one-command install (G1)

- **Packages imported.** The Codex and Claude agent packages are now developed here, in `packages/`.
  `PROVENANCE.json` records each file's source commit and hash, and lists the files changed after
  import, with reasons.
- **One-command installer.** `scripts/install.py` installs, verifies or removes both packages in a
  Git project.
  - Preview is the default, and nothing is applied unless both packages preview cleanly.
  - It refuses targets that are mid-merge, rebase, cherry-pick, revert, bisect or sequence, that
    have unmerged paths, or that are not the top of their working tree.
  - When applying, it also refuses uncommitted changes in managed paths.
  - It adopts an existing install without changing it, and rolls back if a later package fails.
- **Linked worktrees.** Both installers accept a linked worktree's `.git` marker. The Claude
  packager checks the marker without following it.
- **Public-repository guard.** An identifier scanner runs on every commit and in CI. It reports
  only the category and location of a finding, never the value.
- **CI.** Linux and macOS run the repository tests and both package suites. Failures are reported
  as annotations. The Codex package's operational-check sandbox is macOS-only, so those tests are
  skipped elsewhere.
- **Routing table.** `docs/routing-table.md` is the living, evidence-driven table the orchestrator
  routes by, with a changelog.

Verified by rehearsal on a clone of the pilot project:

- installing over its existing packages changed nothing;
- removing and reinstalling restored the identical Git tree;
- both packages verified ready.
