# Delivery plan: cross-provider agent orchestration

- **Status:** in execution since the owner started it on 2026-10-31. Scope changes since then
  are listed under "Decisions and open questions".
- **Updated:** 2026-10-31.
- **Scope:** development-time orchestration. A primary Codex or Claude session delegates bounded
  sub-tasks to Codex or Claude child agents.

## Objective

Any primary agent session, Codex or Claude, can delegate a bounded sub-task to the best-suited
Codex or Claude child. The child is chosen deterministically: verified quality first, then
usage cost (how much of that subscription's allowance the task uses), then quota headroom. One command installs the whole system into
a project. The system measures its own routing while we use it to build itself.

The objective is met when all of these hold:

1. One installer command adds the orchestrator to a project, and removes it cleanly.
2. From either primary, read-only and write-capable sub-tasks reach either provider through a
   contained, deterministic route. Each run is recorded in the ledger.
3. Routing decisions come from recorded quality and usage evidence, not model names. Every
   decision can be replayed and explained.
4. Metered billing is never eligible.
5. The system was used to build itself from the first working version onward, and its records
   informed at least one routing decision.

## Approach

This repository is the single source of truth. The Codex orchestrator and the Claude agent
package already work and have been verified in a private pilot project. We import their
installable code and tests once, then build everything here. That code covers:

- task routing;
- assignment and result contracts;
- the child lifecycle and verification;
- the learning ledger;
- headless launch and output parsing for both clients;
- installers.

Each imported file records its source commit. Research and evidence records stay out. The
original private packages are frozen as history. The orchestrator is installed into the pilot
project from this repository.

The work is mostly integration. The genuinely new pieces are:

- a provider choice inside the existing router and ledger;
- a dispatch bridge that lets one provider's session start the other provider's child;
- write-capable children with containment.

## Working rules

- **Authority:** deterministic code and committed project configuration own provider, model,
  tool, filesystem and budget choices. Model, tool or MCP output never grants authority. A
  child's grant can only narrow its parent's.
- **Billing:** only the owner's existing subscription logins are used. Metered APIs, usage
  credits and automatic paid fallback are never eligible. Models the owner has not enabled stay
  disabled.
- **Selection:** quality first. If admitted evidence shows one option clearly better for a task
  type, it wins regardless of cost. When quality is equivalent or not yet known, the option
  with the lower usage cost wins, then more quota headroom, then lower latency. Until cost is
  measured, the starting table stands in for it. The same rule chooses between models within
  one provider. It is lexicographic, not a weighted sum. Unknown headroom never counts as
  ample.
- **Public repository:** no personal or machine identifiers. Ledgers, prompts and run state live
  in a user-local state directory and are never committed. An identifier scanner blocks commits
  and CI.
- **Stable toolchain:** a pinned release N builds N+1. In-development code never routes changes
  to itself.
- **Proportionate review:** changes to authority, containment or billing logic get an independent
  security review before release. Everything else gets tests and Ruff.
- **Autonomous execution:** once the owner approves this plan, the primary agent works through
  the goals continuously. It pauses only for decisions that need the owner:
  - merging a version branch into `main`;
  - account, authentication or billing actions, including re-registering the learning collector;
  - a security review it cannot resolve;
  - a material change to authority or architecture.

  While a decision is pending, it continues with other work.
- **Evidence loop:** the primary routes its own sub-tasks by the routing table from G1 onward.
  - In G1 it uses Claude subagents and read-only Codex runs, and logs each run by hand.
  - From G2 the ledger records runs automatically.
  - At every feature merge, and at least every 2–3 hours of work, it reviews the new evidence
    and updates the table under the rules in [routing-table.md](../routing-table.md).
- **Branching:** release and feature branches.
  - **Release branch:** each goal gets a `version/<semver>` branch cut from `main`. The owner
    merges it into `main` after the full regression run and CI pass.
  - **Features:** each work item is built on a `feature/<name>` branch cut from the version
    branch. It merges back only after its tests, Ruff, the identifier scan and any required
    review pass.
  - **Research:** reports live only on `staging`, which is the version branch plus research.
    The version branch is merged into `staging` regularly; `staging` never merges into a
    version branch or `main`.
- **Portability:** add no new POSIX-only code. CI runs on Linux and macOS; Windows support is
  parked (see the parking lot).

## Goals at a glance

Effort is an estimate in focused working sessions.

| Goal | Feature set | Dogfooding | Exit | Effort |
|---|---|---|---|---|
| G1 | Import and one-command install | Manual, logged by hand | Imported tests pass here; installer reproduces the pilot's current install | 1–2 |
| G2 | **MVP:** cross-provider read-only routing | Starts at exit | Both primaries reach both providers read-only through recorded, replayable routes | 2–4 |
| G3 | Write-capable children | Read and write sub-tasks | Contained write children, verified by the other provider | 2–3 |
| G4 | Evidence-driven routing | Full, with a paired sample | First evidence-backed routing decision | 1–2, then ongoing |

## G1: Import and one-command install

**Features**

- **Import** the installable code and tests from both packages at recorded commits:
  - the orchestration library, agent definitions, skills, hooks, policy, schemas and installer;
  - the Claude package, launch and stream parsers, packaging and fixtures.

  Scrub the imported files and scan them for identifiers.
- **Public basics:** README, LICENSE, and a short SECURITY policy.
- **Identifier guard:** `scripts/check_identifiers.py`, run as a pre-commit hook and in CI on
  Linux and macOS. Owner-specific denylist entries live in a private file outside the
  repository.
- **One installer command:** it wraps the two existing installers, with preview by default plus
  `verify` and `remove`.
  - **Refusals:** an in-progress merge, rebase or cherry-pick; dirty managed paths;
    collisions; symlinks.
  - **Linked worktrees:** supported.
  - **Existing installs:** a target that already has the earlier packages is adopted without
    churn.
- **Ledger continuity:** existing ledger data and its format are kept. Because the collector
  build changes, the owner re-registers it.
- **Freeze note:** a "development has moved" note in each private package README.

**Exit**

- The imported test suites pass in this repository.
- CI is green, and the scanner rejects a seeded identifier.
- In a disposable clone of the pilot project, installing reproduces the current installed files
  byte for byte. Remove followed by reinstall leaves unmanaged files byte-identical.

## G2: MVP, cross-provider read-only routing

**Features**

- **Provider-aware router and ledger:**
  - a provider field on routes, assignments, results and ledger records;
  - model and role lists loaded from policy data instead of fixed code lists;
  - Claude models and roles in the policy;
  - Claude agent definitions discoverable for project mapping.
- **Task-based starting table** from [routing-table.md](../routing-table.md), labelled as hypotheses.
  Where two options are expected to be comparable, the table prefers the one expected to use
  less allowance. That expectation is also a hypothesis until G4 measures it.
- **Current models:** the policy moves from the earlier GPT-6 Sol to GPT-6.1 Sol and validates
  against the current Codex client.
- **Dispatch bridge** built from the already-tested launch code:
  - **Claude:** `claude -p` with a locked profile.
    - Hooks, plugins and MCP off.
    - The model pinned, no fallback model, read tools only.
    - Never `--bare`, which switches to API-key billing.
  - **Codex:** `codex exec` with a read-only sandbox and ephemeral session, user config ignored,
    and schema-checked output.
- **Launch discipline:** a durable intent is recorded before launch, with a hard deadline, no
  resend, and an ambiguous send fenced from rerouting.
- **Eligibility, failing closed:**
  - subscription authentication confirmed from client metadata;
  - no API-key environment variables;
  - client version within the reviewed range;
  - provider enabled in project configuration;
  - headroom not exhausted.
- **Usage observations:** every run records the signals that cost and headroom depend on.
  - Codex: the percentage of each usage window used, before and after the run.
  - Claude: tokens, model, and the rate-limit status, type and reset time.
- **Signal-based limits:**
  - New children stop for that provider above about 90% for Codex, or on a rejection, until
    the reset time.
  - A per-window run count is a backstop, mainly for Claude.
- **Orchestration skills** for both kinds of primary: they handle bridge routes through the same
  lifecycle as native children (validate, verify, operational QA).
- **Spikes, resolved inside this goal:**
  - whether a Codex primary can launch `claude` from inside its sandbox, and the narrowest way
    to allow it;
  - read-tool containment for Claude children;
  - which usage and headroom signals each client reports, and whether Codex usage can also be
    queried directly.
- **Pilot install:** installed into a fresh worktree of the pilot project.

**Exit**

- From a Codex primary and from a Claude primary, a research task and a review task reach each
  provider through routes recorded in the ledger.
- Recorded routes replay exactly.
- Unknown billing, exhausted headroom, metered billing, and an API key in the environment are each
  denied.
- An independent security review returns GO.
- The release is tagged as dogfood toolchain v1.

**Dogfooding:** from here on, read-only sub-tasks go through toolchain v1: research, plan review,
code review and verification. That applies both in this repository and in the pilot project.

## G3: Write-capable children

**Features**

- **Child worktrees:** each write assignment gets its own worktree outside the repository.
- **Ownership check after the run:** changes outside the owned paths, symlinks, oversized files,
  or edits to agent or orchestration files are rejected.
- **Write profiles:**
  - Claude: `Edit` and `Write` limited to the owned paths.
  - Codex: workspace-write inside the child worktree.
- **Integration:** the primary integrates a child's diff only after verification by the other
  provider and the project's checks pass. Cancellation and cleanup are tested.
- **Sandboxed shell for Claude children:** a separately reviewed sub-slice, with network off.

**Exit**

- An independent containment security review returns GO.
- One coding sub-task is completed by each provider, verified by the other, and integrated.
- Toolchain v2 is released.

**Dogfooding:** implementation sub-tasks can now be delegated to either provider.

## G4: Evidence-driven routing

**Features** (built on the existing learning loop)

- **Paired runs** on a budgeted sample of real read-only tasks, requested explicitly. Comparison
  runs pause when Codex passes about 70% of a window, or Claude issues any warning.
- **Frozen evaluation set:** a small held-out set per task family, with matched settings.
- **Separate tiers:** the three evidence tiers are stored and reported separately.
- **Backtest:** replay recorded runs against a candidate preference table.
- **Usage-cost model:** estimate how much of each subscription's allowance a task uses, per
  provider, model and task type. Both clients report window utilization: Codex as percentages from
  its rate-limit read, Claude as fractions in each run's rate-limit event, at 1% granularity. Cost
  per task comes from window deltas, with Claude's coarser resolution stated.
- **More task families:** documentation, mechanical edits and non-agentic transforms have rows in
  the starting table but no task family yet, so they route as their nearest family until added.
- **Cost- and headroom-aware ranking:** a material quality advantage first, then usage cost,
  then headroom.
- **Promotion:** quality must be no worse, with at least 20 admitted cases per slice. Promotion
  needs owner approval and can be rolled back.

**Exit:** a first evidence-backed change to the starting table, or an evidenced decision not to
change it, approved by the owner.

## Dogfooding guard rails

| Risk | Mitigation |
|---|---|
| A router bug corrupts its own fix | Stable toolchain: a pinned release N builds N+1 |
| Selection bias, where the router never tries the other provider | Paired runs on a budgeted sample, plus the frozen evaluation set |
| Narrow, small sample | Evidence tiers kept separate; ordinary dogfood records never change routing on their own |
| Quota use | Live usage signals pause comparison runs, then stop new children for that provider; a run count is a backstop |
| Delegation overhead on simple work | Simple sequential work stays in one agent; the ledger records overhead |
| Write-capable children before containment is proven | Read-only until G3's containment review passes |

**Evidence tiers:**

1. **Operational:** ordinary dogfood runs.
2. **Paired:** the same task on both providers, graded by an independent verifier or by
   deterministic checks.
3. **Frozen evaluation:** held-out cases, required before a routing change is promoted.

## Design reference

### Dispatch paths

| Primary | Child | Path |
|---|---|---|
| Codex | Codex | Native subagent, unchanged |
| Codex | Claude | Bridge runs `claude -p` (child worktree for writes) |
| Claude | Codex | Bridge runs `codex exec` (child worktree for writes) |
| Claude | Claude | Bridge for writes; the native subagent only for read-only work |

### Starting table

The live routing table is in [routing-table.md](../routing-table.md). It holds each task's model
and effort, alternative, escalation, estimated usage and minutes, confidence, the first paired
tests, and a changelog of every change with its evidence.

### Selection rule

1. The router classifies the task: family, stage and verifier.
2. Provider candidates come from project configuration, intersected with its grants.
3. A hard eligibility filter fails closed.
4. Candidates are ranked by quality: an evidenced, material advantage wins outright. Among
   candidates of equivalent or unknown quality, the lower usage cost wins, then more headroom,
   then lower latency. The starting table stands in for cost until G4 measures it.
5. The verifier comes from the other provider when one is eligible.
6. The ledger binds the route, the rule that decided it, and the digests of the headroom
   observation and policy, so the route replays exactly.
7. A route never changes after a send.

## Parking lot (not needed for the objective)

- Native Windows support and a Windows CI job.
- Operational QA on Linux. The Codex package runs operational checks inside macOS Seatbelt
  (`sandbox-exec`) and fails closed elsewhere, so those tests are skipped off macOS.
- Moving more of the learning ledger's internals into smaller modules.
- Contributor documentation beyond the README.

## Decisions and open questions

- **License (decided):** Apache-2.0. The copyright notice names the project's authors, not a
  person.
- **Usage limits (decided approach; thresholds checked in G2):** limits follow live usage
  signals rather than fixed run counts (see G2). If a provider reports no signal, its headroom
  is treated as unknown and handled cautiously.
- **Paired-run mode moved from G2 to G4 (2026-10-31):** a gap audit of G2 found it unbuilt. G2's
  exit does not need it, and G4 already owns paired runs, so the explicit paired mode and the
  comparison-run pause it governs now ship with G4. Until then, paired tests are run by hand and
  logged as before.
- **Cost (researched in G4):** the owner expects Codex to use noticeably less allowance than
  Claude for comparable work. Quality stays the first rule; cost decides only between options
  of equivalent quality, once it is measured.
