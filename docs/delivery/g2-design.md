# G2 design: cross-provider read-only routing

- **Status:** draft for cross-provider review.
- **Goal:** the G2 exit in [the plan](plan.md).
- **Evidence:** the [routing table](../routing-table.md) and the dispatch spikes on `staging`.

## Constraints from the existing core

The Codex package's ledger binds every host run to a digest of its route. Each later lifecycle step
recomputes the route from the task contract, the installed policy and ledger state, then compares
digests. Provider selection must therefore be a deterministic function of those three inputs.
Anything time-varying, such as quota headroom, has to enter as a ledger record that the task
references. The existing `routing_evidence_refs` mechanism already does this.

## Slices

Each slice is a feature branch with tests and a cross-provider review.

### S1: provider-aware policy, router and ledger

- **Policy v5** (`config/routing-policy.json`):
  - `providers` lists each provider's models with their allowed efforts, and the Claude roles
    (`claude_agents_*`). Codex keeps its existing `models` and `roles`. GPT-6.1 Sol is added, and
    `validated_client` moves to the current Codex client.
  - `provider_candidates` maps each `family.stage` to an ordered candidate list. Each candidate has
    `provider`, `model`, `effort` and a `rank`, taken from the routing table. Equal ranks are joint
    first choices.
- **Task contract:**
  - An optional `primary_provider` (`codex` or `claude`, default `codex`) decides native versus
    bridge dispatch.
  - An optional `headroom_snapshot_ref` (`headroom:<sha256>`) refers to a ledger record.
- **Route output:** gains `provider` and `dispatch` (`native` when the child's provider equals the
  primary, otherwise `bridge`). It also gains `selection_rule` (`preference`, `headroom_tie` or
  `eligibility_fallback`) and `headroom_snapshot_digest`. Effort and model still come only from
  policy.
- **Eligibility, failing closed before ranking:**
  - the provider is enabled in the project's `provider-routing.json`;
  - the model is allowed there;
  - a role exists for the family;
  - the sandbox is read-only (G2 has no write-capable bridge children);
  - the referenced snapshot does not mark the provider exhausted.

  If no candidate is eligible, the route falls back to the existing Codex route and records
  `eligibility_fallback`.
- **Ranking:** lowest rank wins. Ties go to the provider whose snapshot shows more remaining
  headroom (`ample` beats `constrained` beats `unknown`). Remaining ties go to candidate order. G2
  has no quality-evidence step; that arrives in G4 through the existing candidate mechanism.
- **Headroom states:**
  - Codex: `exhausted` at 90% used or more, or when `ordinaryUsageAllowed` is false; `constrained`
    at 70% or more.
  - Claude: `exhausted` on a `rejected` status; `constrained` on `allowed_warning` or utilization
    of 0.7 or more.
  - Missing or stale observations are `unknown`.
- **Project configuration:** `.codex/orchestration/provider-routing.json` (version 1) lists enabled
  providers, allowed models per provider and an optional per-family override of candidates. It is
  validated like `project-agent-routing.json`: size-capped, closed keys, committed and clean for
  readiness.
- **Ledger:**
  - A `provider_headroom` record type is written by deterministic code only. It holds the provider,
    each window's used fraction, status, reset time and observed time, and is keyed by its content
    digest.
  - `trusted_headroom_snapshot` resolves a task's reference within the same project, checking
    freshness (30 minutes) at host-run open but not on replay.
  - Closed model and role sets become the union of the installed policy's providers. Outcome and
    event records gain `provider`.
- **Discovery:** `.claude/agents/*.md` agents are discovered as the `claude-md` format, inert
  unless mapped.

### S2: dispatch bridge

- **Command:** `cli.py dispatch --project-root --task --route --assignment --parent-grant` runs one
  bridged child and returns a result object for the normal `validate-result` path.
- **Launch discipline:** a durable intent is written before launch, with a hard deadline, process
  group termination, no resend, and an ambiguous outcome fenced.
- **Claude child:** the spike profile.
  - Flags: `--tools Read,Glob,Grep`, `--permission-mode dontAsk`, `--setting-sources project`, a
    generated settings file (hooks off, every built-in plugin off, `availableModels` pinned, no
    allow rules), `--strict-mcp-config`, `--no-session-persistence`, `stream-json`, and stdin
    closed.
  - The environment is scrubbed of API keys.
  - Runs fail closed unless the init frame reports `apiKeySource: none` and the requested model.
- **Codex child:** `codex exec`.
  - Flags: `-s read-only`, `--ephemeral`, `--ignore-user-config`, the routed model and effort,
    `--json`, and the result schema as `--output-schema`.
  - The served model is recorded when reported. A `model/rerouted` event is treated as unreviewed.
- **Usage:** usage and headroom come from the stream: Claude's `rate_limit_event`, and a Codex
  app-server `account/rateLimits/read` before and after. They are recorded with the run, and the
  observation is written as a `provider_headroom` record.
- **App-server allow-list:** the bridge uses `initialize` and `account/rateLimits/read` only. It
  never calls methods that spend or change account state.
- **Ledger client:** a new `learning-clients.json` entry (`bridge-v1`) declares client-observed
  usage. Host runs opened for bridged children bind the bridge run ID as the agent ID.

### S3: orchestration skills for both primaries

The Codex skill (`.agents/skills/orchestrate-agents`) and a new Claude skill
(`.claude/skills/agent-orchestration`) both:

- record headroom;
- route with `primary_provider`;
- spawn natively when `dispatch` is `native`, and call `dispatch` when it is `bridge`;
- otherwise keep the existing lifecycle: open, bind, validate, verify, operational QA, record.

### S4: security review, pilot install and exit

- An independent security review covers the bridge, profiles, eligibility and allow-lists.
- Install into a fresh worktree of the pilot project.
- Exit runs:
  - from a Claude primary: a research and a review task to each provider;
  - from a Codex primary: the same, once the owner decides the sandbox question below.
- Tag toolchain v1.

## Open owner decision

A Codex primary can only launch a Claude child if its sandbox allows network. Either network is
enabled for the pilot project's Codex sandbox, or an exec-policy rule auto-approves an escalation
for the bridge command only. The second option must first be verified in an interactive Codex
session. Until then, a Codex primary routes only to Codex children. The router marks a Claude
candidate ineligible when the primary is Codex and the project has not enabled this path.
