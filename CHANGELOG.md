# Changelog

## 0.3.0 (in progress): write-capable children (G3)

- **Contained workspace.** A write child runs in a workspace exported from one commit, with no
  `.git`, and with protected agent and context names refused at any depth and in any letter
  case. After the child stops, the bridge snapshots the workspace without following links,
  inventories it, and checks ownership: only the effective write set may change, with no special
  files, no hard links and no permission changes beyond the executable bit. A self-contained,
  digest-bound patch and a verifier envelope carry the result. The patch only applies onto a
  verified tree at the source commit.
- **Claude write children.** Every write route goes through the bridge. Claude write children run
  with exact tools and absolute, workspace-anchored `Edit` and `Write` rules, plus deny rules for
  protected names.
  - Results count only once the child's processes are proven gone.
  - A write plan fixed before launch binds what can be recorded.
  - Patch and envelope are stored atomically and verified on every read.
  - Expired runs are fenced in the ledger before their workspaces are swept.
  - Codex write routes, and every write route off macOS, are refused for now.

The Codex package is 0.9.0.

## 0.2.0: cross-provider read-only routing (G2)

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
