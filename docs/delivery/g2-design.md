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
  - Flags: `-s read-only`, `--ephemeral`, `--ignore-user-config`, `--ignore-rules` and
    `approval_policy="never"` (see S4c), the routed model and effort, `--json`, and the result
    schema as `--output-schema`.
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

### S3c: policy matches the table

`provider_candidates` now names the Codex model and effort explicitly for every row the table
covers, and a table-driven test fails CI if policy and table drift apart. Explicit candidates
apply in every project, including Codex-only projects without a provider configuration, so the
table also chooses the Codex model there. `routes` remains the fallback for a family without
candidates. Rows with no task family (documentation, mechanical edits, non-agentic transforms),
escalation steps and context-size conditions are not expressible yet; they belong to G4.

### S4b: Codex child tool lockdown

A live spike found that `codex exec`, even with user configuration ignored, exposes the account's
connector tools: GitHub writes, site deployment and environment variables. It also exposes web
search, image generation, goals and agent spawning. Connector calls run server-side, outside any
local sandbox. The bridge therefore:

- passes pinned feature disables, which remove the connectors, web search, image generation and
  goals;
- relies on `--ephemeral`, under which agent spawning fails before any sub-agent run;
- stops a child on any event or item type outside a short allowlist.

The allowlist only detects emitted, typed events. Pre-dispatch denial comes from the disables
and ephemeral mode, which were shown live only on codex-cli 0.164.0, so the reviewed Codex range
is pinned to that build. Widening it requires the live containment check to pass on the new
build.

### S4c: bridged child containment

- **No execpolicy rules.** `--ignore-user-config` skips only `config.toml`. Codex still loaded
  `${CODEX_HOME:-~/.codex}/rules/*.rules`, and a trusted project's `.codex/rules`, for a bridged
  child, and the S5b rule lets the primary-host commands (including `dispatch`) run outside the
  sandbox, matched by their arguments alone. A read-only bridged child could therefore have run
  them unsandboxed. The bridge now passes `--ignore-rules`. Before the durable intent it reads
  `codex exec --help` from the re-checked client (no model call) and refuses with
  `codex_exec_options_unavailable` unless `--ignore-user-config` and `--ignore-rules` are listed.
  It also pins `-c approval_policy="never"`, so no escalation request can be granted; codex-cli
  0.164.0 validates the value and the rendered prompt then says commands needing approval are
  rejected.
- **Rest of the user layer.** Inspected on codex-cli 0.164.0 with a scratch `CODEX_HOME` and
  `codex debug prompt-input` and `codex features list`, which make no model call. The user's
  `AGENTS.md` and skills (`$CODEX_HOME/skills`, `~/.agents/skills`) still reach a child's prompt,
  as do the export's own `AGENTS.md` and skills; they are instructions, not authority. No MCP
  server or plugin tool appears, an exported `.codex/config.toml` is not loaded, and hooks stay
  disabled. `skill_mcp_dependency_install`, on by default, can install and start an MCP server
  that a mentioned skill declares, and MCP servers run outside the sandbox, so the bridge
  disables it too.
- **Managed configuration.** System and managed Codex configuration applies despite
  `--ignore-user-config` and can add MCP servers or hooks. A Codex dispatch is refused with
  `codex_managed_config_present` when `/etc/codex` holds any TOML file (or cannot be read), when
  `~/.codex/managed_config.toml` exists, or, on macOS, when a `com.openai.codex` plist exists
  under `/Library/Managed Preferences/` (machine-wide or for the account), as Claude launches
  are refused on managed settings.
- **Process tree.** The child runs in its own session, but Codex starts helpers such as
  `codex-code-mode-host` in their own process groups, and TTY commands may get their own
  session. The rule is that killing an unrelated process is worse than leaving a sandboxed
  orphan, so tracking is conservative:
  - A fixed-path `ps` (`/bin/ps`, else `/usr/bin/ps`; `-U <uid> -o pid=,ppid=`) only lists
    candidate pids, every 0.25 s while the child runs. Every decision rests on one kernel record
    per process that gives its parent, process group, birth time and exit state together:
    `kinfo_proc` from `sysctl(KERN_PROC_PID)` on macOS (birth in microseconds; the record must
    name the pid asked for, and this process's own record must give its own parent and group),
    and `/proc/<pid>/stat` on Linux (birth in clock ticks).
  - A candidate is adopted only when its own record names a tracked parent and that parent's
    record, read afterwards, still has the tracked birth time; the candidate's birth time comes
    from the same record as its parent. A pid rejected earlier is readopted only the same way.
    The first snapshot, taken while the child is unreaped, already adopts what is visible. A
    shared process group never adopts.
  - The child is never reaped before cleanup: its exit is seen in its record. Then every live
    tracked process is killed with SIGKILL in up to five rounds, each rescanned, with a fresh
    record read before every signal. A group is killed only through a leader whose fresh record
    shows the tracked birth time and that it leads the group: the child itself (its pid cannot
    change hands while it is unreaped) or another tracked process. Nothing is stopped. Only then
    is the child reaped and its exit status kept.
  - An exit first seen after the deadline, for example because a snapshot blocked, is a timeout.
  - The deadline watchdog runs the same standard-library module as a script, with the child's
    pid and birth time. It verifies that identity, retrying for up to five seconds, then
    acknowledges while the bridge still holds the child unreaped; it never signals anything it
    could not verify. Without the acknowledgement the run is aborted (`watchdog_unavailable`).
    It then tracks the tree itself every 0.5 s and kills it at the deadline or once the child
    has exited, so a bridge crash still ends the tree.
  - Without a readable process table or kernel records the child is not launched
    (`process_tracking_unavailable`).
- **Residual risks.**
  - Deliberate lifetime escape: polling cannot contain a process that double-forks into a new
    session and loses its parent between two snapshots (within 0.25 s, or 0.5 s for the watchdog
    after a bridge crash). It is left running rather than risk an unrelated kill. It stays inside
    the child's read-only, network-disabled Codex sandbox; kernel-enforced membership (such as a
    Linux cgroup) would be needed to end it. The same applies to an orphan that a process
    starts in the instant before its own kill, outside the child's group.
  - Identity window: the birth time is microseconds on macOS and 10 ms clock ticks on Linux, and
    a pid could change hands between the last record read and the signal. Both require the pid
    space to wrap around in that interval.
  - Native Codex children share the primary's command rule (see S5b).

### S4: security review, pilot install and exit

- An independent security review covers admission, the bridge, profiles, scoped directories and
  allow-lists. It reviews the whole release, not slice diffs, with two reviewers from different
  vendors. The first G2 run found cross-slice and cross-project gaps that slice reviews had
  missed: billing state scoped per project, exports beyond the assignment's read grant, and
  billing not re-checked at launch.
- Install into a fresh worktree of the pilot project.
- Exit runs: a research and a review task to each provider, from a Claude primary and from a Codex
  primary.
- Tag toolchain v1.

### S5b: Codex host command boundary

- **Problem.** A Codex primary runs `cli.py` inside Codex's workspace-write sandbox. The
  sandbox denies connecting to the collector's local socket and writing the collector's
  runtime lock in the per-user state directory, so every collector-backed command failed
  (`filesystem operation failed`). `validate` and `learning-run-operational-checks` also
  refuse inside a sandbox, because the operational checks need their own nested one. The
  package assumed an approved unsandboxed command boundary but never provided one for a
  headless Codex primary.
- **Rule.** With the owner's consent, the wizard's Commands step writes one Codex command
  rule per project: a `prefix_rule` with `decision = "allow"`, which runs a matching
  command outside the sandbox without a prompt. The pattern is the absolute base interpreter,
  the isolation flags `-I -B -S`, the absolute `<root>/.codex/orchestration/cli.py`, then
  one allowlisted subcommand. The file carries `match` and `not_match` examples that Codex
  checks when it loads it: an owner command, the relative forms and the form without the
  flags must not match. It is written with mode 0600 and symlinks are refused. A new rule is
  published with a hard link, which never overwrites, so a rule another setup wrote in the
  meantime is reported as differing; identical content is left alone, and a differing rule
  is replaced only after the owner confirms.
- **Allowlist.** `PRIMARY_HOST_COMMANDS` in `codex_agents/host_commands.py`, tested to be
  real subcommands and disjoint from the owner commands: `validate`, `inventory`, `route`,
  `learning-route`, `validate-assignment`, `result-preflight`, `validate-result`,
  `learning-status`, the orchestration open, checkpoint, pause, resume, close and report
  commands, the host-run open, bind, observe, classify-abandoned and record commands,
  `dispatch`, `provider-observe`, `learning-run-operational-checks` and
  `learning-record-operational-qa`.
- **Why the user layer.** Codex loads `${CODEX_HOME:-~/.codex}/rules/*.rules` in every
  session. A project's `.codex/rules/` loads only when that project's `.codex/` layer is
  trusted, which is unreliable for worktrees.
- **Why absolute paths.** Inside the workspace, the sandbox keeps `.codex/`, `.agents/` and
  `.git` read-only, but the model can write elsewhere in the workspace and in temporary
  directories. A relative `cli.py` would let it plant a look-alike and run it unsandboxed.
- **Host command.** `host-command` is read-only and needs no collector, so it works inside
  the sandbox. It prints the exact `argv_prefix` and whether the rule is `installed`,
  `missing` or `stale` (it differs from what setup would write now). The Codex skill runs it
  first and stops unless the rule is installed. A collector-backed command that cannot
  reach the collector under a Codex host keeps its error and adds a hint.
- **Governing rule.** A rule-matched command runs outside the sandbox, so nothing it
  executes or loads may come from a sandbox-writable location, and nothing it writes and
  later trusts or hands to a child may live in one. Sandbox-writable means the workspace
  (except its top-level `.git`, `.codex` and `.agents`), `/tmp` and `$TMPDIR`. On codex-cli
  0.164 the per-user cache directory, `/var/tmp`, `~/Library/Caches` and the learning state
  were shown not writable from the workspace-write sandbox.
- **Interpreter.** The rule names the base interpreter, never a virtual environment's (a
  project `.venv` would let the model replace the executable or add `.pth` startup code),
  pinned as its realpath: no symlink on the way remains that a sandboxed session could
  later retarget, so a chain through a writable hop cannot redirect it, and a Python
  upgrade makes the rule stale until setup runs again. The resolved interpreter and its
  standard-library prefix are walked component by component: no component may be a
  symlink, belong to another user or be writable by another user, and neither may lie in
  the project or a temporary directory. One exception: on macOS a directory writable by
  the `admin` group is accepted, because Homebrew and python.org install that way and
  admin members can already administer the machine. `-I` ignores the environment and user
  site-packages and keeps the script directory off the path, `-S` skips site-packages
  entirely (no `.pth` file or `sitecustomize` runs; the CLI needs only the standard
  library and its own package), and `-B` writes no bytecode into `.codex`. The collector
  the CLI starts or restarts, and the bridge's deadline watchdog, get the same flags.
- **Project.** The generator refuses a project in a temporary directory, which a session
  can rewrite wholesale (including `cli.py`), and a CLI path with a symlink or a component
  another user could change. It refuses before writing, printing or reporting `installed`;
  the wizard checks the location with this repository's own code first and writes
  nothing.
- **Git plumbing only.** Every git call reached from the allowlisted commands uses only
  `rev-parse`, `ls-tree` and `cat-file`; committed blobs are compared with working-tree
  bytes read through no-follow opens, including an operational-check executable's mode.
  No call refreshes the index, so an fsmonitor, filters, textconv, an external diff,
  hooks and a pager configured in the repository never run. Each call also passes
  `--no-pager`, `--no-optional-locks`, literal pathspecs and `-c` overrides
  (`core.fsmonitor=false`, `core.hooksPath=/dev/null` and others), with
  `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null` and `GIT_TERMINAL_PROMPT=0`.
  A git directory or common directory inside the working tree (other than `<root>/.git`),
  or in a temporary directory while the project is not, is refused before any other call:
  that closes a linked worktree whose shared repository the model could rewrite. Plumbing
  can still fetch a missing object from a promisor remote, whose transport (`ext::` runs a
  command) a file the configuration includes could name, so `GIT_ALLOW_PROTOCOL` is empty
  (no transport, whatever `protocol.*.allow` says), `GIT_NO_LAZY_FETCH=1` is set, and a
  repository with partial-clone settings or a promisor remote is refused, read with
  `git config`, which runs nothing. On `/usr/bin/git` 2.54.0 (Apple Git-157) the empty
  protocol list and the lazy-fetch switch each stopped a planted `ext::` payload, and
  `file://` and local-path fetches report "transport not allowed".
- **Host-private scratch.** Bridge exports, control files (including Claude's settings),
  child and probe temporary directories live in an owner-only directory under the per-user
  cache directory on macOS (`$XDG_RUNTIME_DIR` elsewhere; dispatch refuses without one).
  Claude's workspace must stay outside home, where `~/.claude` would sit above it.
  The scratch base may neither lie inside nor contain the project or any temporary
  directory; dispatch and the probe refuse otherwise, on every platform.
  Operational-check snapshots and work directories, and provider clients' temporary
  directories, live under the learning state's `scratch` directory. Each directory is
  created through directory descriptors with no-follow opens at every component,
  owner-only, and removed afterwards; a planted symlink refuses. Later writes inside it
  use paths, which only the host process can reach.
- **Own-root invariant.** The rule fixes only the start of a command, so the arguments
  after the subcommand are the model's. An installed CLI therefore pins every allowlisted
  command, before any git, process or collector work: `--state-root` must resolve to the
  default learning state, `--project-root` and `--root` must resolve to the project that
  holds the running `cli.py`, and `--policy` is refused outright. The checked, resolved
  values replace the given ones, so a swapped symlink cannot redirect a later use, and a
  repeated option is checked at the value argparse keeps. Refusals name the option, never
  its value. No flag turns the check off, and host markers play no part.
  - **Why keyed to the installed CLI.** It applies when an install manifest sits next to
    `cli.py`. A sandboxed Codex session cannot create or remove that file, because
    `.codex/` is read-only there, and the rule only ever runs the installed `cli.py`. The
    source tree has no manifest and keeps these options for development and tests.
  - **Why the default state is safe to trust.** It follows `HOME` (or `XDG_STATE_HOME`
    on Linux) in the process environment. A command Codex runs through the rule is a
    fresh process with Codex's environment, and an environment-assignment prefix breaks
    the rule's argument match, so the model cannot move it. Tests that drive an
    installed CLI point `HOME` at a temporary directory instead of using an opt-out.
  - **What it closes.** Before it, `validate --root`, `route --root`, `learning-route`
    and the assignment and result checks ran git in a caller-chosen directory before
    any registration check (provider-routing and operational-check trust both read the
    repository). A model-built repository whose own `.git/config` sets
    `core.fsmonitor` runs a command on the first index refresh, which through the rule
    would run outside the sandbox. A model-built state directory would likewise have
    let `dispatch` start a bridged child without the owner's attestation. Both are
    unreachable now: every allowlisted command uses this project and the owner's state.
- **Residual risks.**
  - The project must be the Codex session's workspace root: Codex keeps `.codex` read-only
    only at the top of a writable root, and the rule runs that `cli.py`.
  - The interpreter check accepts admin-group-writable directories on macOS, and trusts
    the interpreter's standard library and dynamic libraries where they are installed.
  - A shared repository in a writable root other than the project and the temporary
    directories (one added to the session explicitly) is not detected; git then only
    reads its objects and refs, with every helper off.
  - The cache-directory and learning-state locations were verified unwritable only on
    codex-cli 0.164's macOS sandbox; `$XDG_RUNTIME_DIR` on Linux was not tested live.
    A crash can leave a private scratch directory behind; it stays owner-only.
  - Input files (task, assignment, grant, route, result) remain caller-chosen data,
    validated as before.
  - Owner commands stay unreachable from a sandboxed Codex primary; registration,
    attestation and the rule itself come from the owner's terminal. An owner who runs
    `scripts/attest.py --state-root` against an installed project is refused at
    `provider-observe`, because only the default state is accepted.
  - Native Codex children that a Codex primary spawns share its session, so the rule applies
    to them too: a native child could run the primary-host commands outside its own sandbox.
    It is still held to this project's root and the owner's state by the own-root invariant,
    and every run it could open or dispatch still needs ledger admission. The Codex skill
    says never to ask a child to run them. Bridged Codex children load no rules (S4c).
  - The Claude primary's permission rules are a separate follow-up.

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
