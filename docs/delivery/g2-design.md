# G2 design: cross-provider read-only routing

- **Status:** revision 3. The first cross-provider review found eight blockers and proposed a
  simpler design, which is adopted. The second review confirmed seven fixed and raised three more,
  addressed below. Reviews are capped at two rounds; the S4 security review checks the result.
- **Goal:** the G2 exit in [the plan](plan.md).
- **Evidence:** the [routing table](../routing-table.md) and the dispatch spikes on `staging`.

## Constraints from the existing core

The Codex package's ledger binds every host run to a digest of its route. Each later lifecycle step
recomputes the route from the task contract, the installed policy and committed project
configuration, then compares digests. G2 therefore keeps route selection a pure function of those
inputs. Time-varying facts never change a route. Quota headroom and billing observations only gate
admission, and the gate's decision is bound to the run.

## Slices

Each slice is a feature branch with tests and a cross-provider review.

### S1: provider-qualified routes

- **Policy v5** (`config/routing-policy.json`):
  - `providers` lists, for each provider, its models with their allowed efforts and its roles per
    family. Codex keeps its existing models and roles; GPT-6.1 Sol is added, and
    `validated_client` moves to the current Codex client. Claude lists Opus 5.5, Sonnet 5.5 and
    Haiku 5.5 with `claude_agents_*` roles.
  - `provider_candidates` maps each **family and route key** to an ordered list of
    `{provider, model, effort}`. The route key is the one `_candidate` already selects: bounded,
    exploratory, judgment, synthesis, default, reconnaissance or hard end-to-end. The order comes
    from the routing table, cheaper-per-task first among comparable options.
- **Validation:** the policy loader validates every candidate as a provider–model–effort–role
  combination that exists for that provider, not just as members of a union.
- **Selection:**
  - Selection is the first candidate that passes the static eligibility checks: provider enabled
    and model allowed in committed project configuration; a role, or a mapped project agent of that
    provider's format, exists for the family; read-only sandbox.
  - If none passes, the route is **denied**. There is no fallback to Codex.
  - The same checks apply to the verifier, the legacy fallback, and any learning candidate the
    router applies afterwards.
- **Route output:** gains `provider`, `dispatch` and the digest of the provider configuration used.
  - `dispatch` is `native` or `bridge`.
  - Effort and model still come only from policy.
- **Primary identity:** taken from the trusted host client recorded by the lifecycle (`codex-host-v1`
  or a new `claude-host-v1`), never from the task. A task that names a primary must match it.
- **Codex-to-Claude dispatch:** allowed only when the project configuration marks it enabled. That
  depends on the owner's sandbox decision below.
- **Project configuration:** `.codex/orchestration/provider-routing.json` (version 1) lists enabled
  providers, allowed models per provider, and `codex_primary_may_bridge_to_claude`. It is
  size-capped, has closed keys, and must be committed and clean. Its bytes are digest-bound into the
  route, so a change after open fails replay.
- **Execution configuration:** extended to bind `claude-md` agent bytes, so Claude roles and mapped
  Claude project agents can open host runs. `.claude/agents/*.md` is discovered as the `claude-md`
  format, inert unless mapped.
- **Ledger:** closed model and role sets are validated per provider. Outcome and event records gain
  `provider`.

### S2: admission gate and dispatch bridge

- **Admission at host-run open.** It fails closed, and its decision is stored with the run.
  - Deny if `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` is set. Deny rather than scrub.
  - Deny unless the owner's billing attestation for the provider is recorded (no paid overage, no
    usage credits) and bound to the current account's fingerprint.
    - The fingerprint is a keyed digest from a non-spending identity read: the organisation ID
      and email from `claude auth status --json`, or the account ID from Codex's
      `account/rateLimits/read`.
    - Raw identity values are never stored.
    - `claude auth status` must also report the `claude.ai` login with a subscription.
  - Deny if the latest billing observation for this fingerprint shows overage usable.
    - **Bootstrap:** with no observation yet, a run is admitted on the attestation and the
      non-spending identity check alone. Every run's own usage event then refreshes the
      evidence, so an observation showing overage blocks the next run. For Claude, that means `isUsingOverage` false and an overage status of rejected
    or disabled. For Codex, the rate-limit read must show no usable paid credits.
  - Deny when the latest headroom record for the provider shows it exhausted. This is evaluated
    from the ledger, not from anything the task supplies.
  - The decision (pass, or the reason for denial) and the record digests it used are bound to the
    run. Replay checks the stored decision and does not re-evaluate time-varying facts.
- **Re-check at launch:** the environment and the account fingerprint are checked again
  immediately before the child process starts. A mismatch with the admitted evidence aborts the
  launch.
- **Scoped working directory.** A bridged child never runs in the repository root. The bridge
  exports the committed bytes of the assignment's read paths into a fresh temporary directory, and
  the child runs there. The spikes showed Claude's `dontAsk` mode denies reads, globs, greps,
  symlinks and `../` outside the working directory. Tests cover absolute paths, traversal and
  symlinks. Codex children use the same export as their working directory.
  - **Codex reads are not contained.** A spike showed Codex's read-only sandbox can still read
    files outside the working directory, with or without `sandbox_permissions=[]`. That is
    already true of every native Codex child; the package's accepted policy treats path
    declarations as task scope unless the host enforces them.
  - G2 keeps that baseline and states it in the security model.
  - Enforced read roots, by running `codex exec` inside an outer macOS Seatbelt profile, move to
    G3 alongside write containment.
- **Claude child:**
  - Flags: `--tools Read,Glob,Grep`, `--permission-mode dontAsk`, `--setting-sources project`, a
    generated settings file (hooks off, every built-in plugin off, `availableModels` pinned, no
    allow rules), `--strict-mcp-config`, `--no-session-persistence`, `stream-json`, and stdin
    closed.
  - Never `--bare`.
  - The run fails closed unless the init frame reports `apiKeySource: none` and the requested model.
- **Codex child:** `codex exec`.
  - Flags: `-s read-only`, `--ephemeral`, `--ignore-user-config`, the routed model and effort,
    `--json`, and the result schema as `--output-schema`.
  - A `model/rerouted` event marks the result unreviewed.
- **Observations:**
  - Each run's usage and its headroom and billing observations are written as ledger records.
  - Sources: Claude's `rate_limit_event` from the stream; for Codex, `account/rateLimits/read`
    before and after the run.
  - The app-server allow-list is `initialize` and `account/rateLimits/read` only.
- **Launch discipline:** a durable intent is written before launch, with a hard deadline, process
  group termination, no resend, and an ambiguous outcome fenced.
- **Ledger client:** a `bridge-v1` entry in `learning-clients.json` declares client-observed usage.
  Host runs for bridged children bind the bridge run ID.

### S3: orchestration skills for both primaries

The Codex skill (`.agents/skills/orchestrate-agents`) and a new Claude skill
(`.claude/skills/agent-orchestration`) both:

- route normally;
- spawn natively when `dispatch` is `native`, and call the bridge when it is `bridge`;
- keep the existing lifecycle: open, admission, bind, validate, verify, operational QA, record.

Built with these rules:

- **Host detection.** The CLI detects its host from the client's own environment markers and
  refuses when they are missing or contradictory. Markers are a consistency check, not proof.
  Admission, the bridge flag and the sandbox remain the authority.
- **Host ownership.** Each orchestration stores its owning host. Every lifecycle mutation refuses a
  different host. The bridge's execution client is never an owner.
- **Native Claude children.** The CLI returns the Agent-tool spawn parameters (agent type, model
  alias and effort) derived from the route, and the skill passes them verbatim. Their reads are
  advisory, because they run with the parent session's permissions. The Agent tool takes only a
  model alias, so the exact model is pinned by the client, not by us.
- **Claude backends.** A Claude-host open is denied when the environment or the settings the
  parent session loads switch it to an API key, a cloud backend or another configuration
  directory, or override the subagent model or effort.
- **Billing probe.** Native children emit no rate-limit event, so before a native Claude open the
  skill runs a probe: one minimal locked Haiku turn with no tools. The probe passes the same
  admission as a run first, shares the one-run bootstrap, and reuses evidence under five minutes
  old.
- **Claude thresholds.** Claude's rate-limit event reports window utilization. A Claude window at
  90% or more counts as exhausted, and at 75% or more as constrained, the same as Codex.
- **Installer.** The Codex package installs exactly one path under `.claude/`, the Claude skill, and
  refuses any existing file it does not already own, even with identical bytes.

### S3b: eligibility and observation gaps

A read-only audit of S1 and S2 against the plan found gaps that S4 would otherwise inherit:

- **Client version:** each identity observation records the client version. Admission denies a
  version outside the reviewed range committed in policy.
- **Codex plan:** admission requires an observed subscription plan type; absent, unknown and free
  plans are denied.
- **Claude limits:** every restrictive `rate_limit_event` in a run is recorded, so a rejection late
  in one run blocks the next admission until its reset.
- **Run-count backstop:** a per-provider cap on admissions per usage window, counted for the same
  account inside the admission transaction.
- **Run-bound observations:** headroom, billing and usage observations carry their run and phase
  (before or after). A failed reading is recorded as unavailable instead of being dropped.
- **Policy matches the table:** routine planning uses GPT-6.1 Sol at high effort.

### S4: security review, pilot install and exit

- An independent security review covers admission, the bridge, profiles, scoped directories and
  allow-lists.
- Install into a fresh worktree of the pilot project.
- Exit runs: a research and a review task to each provider, from a Claude primary and from a Codex
  primary.
- Tag toolchain v1.

## Deferred to G4

- Headroom tie-breaking between equally ranked candidates.
- Per-project candidate overrides.
- Quality-evidence ranking.

G2 records the headroom data these will need.

## Owner decision required before the G2 exit

A Codex primary can only launch a Claude child if its sandbox allows network. Options:

- **(a)** Enable network for the pilot project's Codex sandbox.
- **(b)** Keep network off and have an exec-policy rule auto-approve an escalation for the bridge
  command only. This needs testing in an interactive Codex session.

Until the owner decides, `codex_primary_may_bridge_to_claude` stays false.
