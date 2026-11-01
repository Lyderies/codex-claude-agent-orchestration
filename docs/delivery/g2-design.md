# G2 design: cross-provider read-only routing

- **Status:** revision 4, plus S6. The first cross-provider review found eight blockers and
  proposed a simpler design, which is adopted. The second review confirmed seven fixed and raised
  three more, addressed below. Reviews are capped at two rounds; the S4 security review checks the
  result. Two whole-release reviews of the v1 candidate returned NO-GO; S6 records the fixes, and the
  result is the release candidate for dogfood toolchain v1 (release 0.2.0, Codex package 0.8.10).
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
- **Codex-to-Claude dispatch:** allowed only when the project configuration marks it enabled (setup
  step 3). The Codex session then needs sandbox network, which the opt-in orchestrator profile gives
  (setup step 7); see "Codex primary network" below.
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
- spawn natively when `dispatch` is `native`, and call the bridge when it is `bridge` (since S6,
  only a Claude primary's Claude children are native);
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
  - Native Codex children shared the primary's command rule; since S6 every Codex child is
    bridged, so none does.

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
  the isolation flags `-I -B -S` (since S6, `-I -B -S -X pycache_prefix=/dev/null`), the
  absolute `cli.py` (since S6, that of the owner-only
  runtime copy, not `<root>/.codex/orchestration/cli.py`), then one allowlisted
  subcommand. The file carries `match` and `not_match` examples that Codex
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
  library and its own package), and `-B` writes no bytecode into `.codex`; since S6,
  `-X pycache_prefix=/dev/null` also stops Python reading cached bytecode. The collector
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
    `.codex/` is read-only there, and the rule only ever runs an installed `cli.py` (since
    S6, the protected copy, whose project comes from its binding). The source tree has no
    manifest and keeps these options for development and tests.
  - **Why the default state is safe to trust.** It followed `HOME` (or `XDG_STATE_HOME`
    on Linux) in the process environment; since S6 it is `XDG_STATE_HOME` or the account
    database's home folder, on macOS too (S6, learning state location). A command Codex
    runs through the rule is a fresh process with Codex's environment, and an
    environment-assignment prefix breaks the rule's argument match, so the model cannot
    move it. Tests that drive an installed CLI point `XDG_STATE_HOME` (and `HOME`) at a
    temporary directory instead of using an opt-out.
  - **What it closes.** Before it, `validate --root`, `route --root`, `learning-route`
    and the assignment and result checks ran git in a caller-chosen directory before
    any registration check (provider-routing and operational-check trust both read the
    repository). A model-built repository whose own `.git/config` sets
    `core.fsmonitor` runs a command on the first index refresh, which through the rule
    would run outside the sandbox. A model-built state directory would likewise have
    let `dispatch` start a bridged child without the owner's attestation. Both are
    unreachable now: every allowlisted command uses this project and the owner's state.
- **Residual risks.**
  - The project had to be the Codex session's workspace root: Codex keeps `.codex`
    read-only only at the top of a writable root, and the rule ran the project's `cli.py`.
    S6 moves the rule's target to an owner-only copy in the Codex home.
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
  - Native Codex children that a Codex primary spawned shared its session, so the rule
    applied to them too. Since S6 a Codex primary spawns no native children.
  - The Claude primary's permission deny rules shipped with the owner-side v1 hardening (see
    the CHANGELOG). They are a policy layer on Claude Code's tools, not a sandbox: a command
    spelled another way (`bash -c`, or a script that starts the CLI) is not matched.

### S6: v1 hardening

Two independent whole-release reviews of the v1 candidate returned NO-GO, and focused
reviews of the runtime boundary and of the bridge and ledger followed. This records the
runtime fixes and the owner-tool rules they depend on; the full owner-side list (setup,
attestation, the installer) is in the CHANGELOG under "Owner-side hardening", "Owner-side review
fixes" and "Owner-side round 2".
The owner's standing rule holds: the orchestrator runs without per-command approvals, and
safety comes from redundant layers.

- **Supported threat model.** The rule runs commands outside the sandbox for every Codex
  session on the machine. It is safe only while no Codex session uses the home folder, its
  parent or `/` as its workspace, and none adds `~` or `~/.codex` as a writable root with
  `--add-dir`. Such a session could write `~/.codex` (the rule and its copy) and every
  tool installed under the home folder (a Python under `~/.pyenv`, Claude Code under
  `~/.local`), which the rule then runs. The wizard's consent screen, the README and the
  Codex skill state it in those words. Open Codex on the project itself; never on a
  folder that contains a set-up project, the clone, your home folder, its parent or `/`.
  The reason for the first: Codex runs a nested trusted project's `.codex/config.toml`
  and `.codex/hooks.json` outside the sandbox, and keeps a project's `.codex`, `.agents`
  and `.git` read-only only when the project is the top of a writable root, so a session
  opened on a folder above the project can plant those files (and edit
  `.codex/orchestration/**` or `.git/config`), and no package code can stop that. Sessions
  opened on any other folder (one that holds none of these) are supported, so a program
  the rule's commands run, and anything they trust, must not sit where such a session
  could reach it.
- **Session workspace guard.** The workspace rule above no longer rests on the owner
  alone. Before every primary-host command, the protected copy identifies the calling
  session from `CODEX_THREAD_ID` (strict UUID form; a command the rule runs is a fresh
  process whose environment the model cannot set) and finds exactly one record for it in
  the account's `~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<id>.jsonl` (account home
  from the account database, every folder and the file opened without following symlinks
  and owner-checked, newest day folders first, at most 400). It reads only two Codex 0.164
  records, never conversation content: `session_meta` (the first line: `cwd`,
  `runtime_workspace_roots`) and the last `turn_context` (read backwards in bounded steps,
  at most 32 MiB from the end: `sandbox_policy.type` and
  `file_system_sandbox_policy.entries`). It refuses, with plain messages:
  - a session without its sandbox (`danger-full-access`) or of an unknown type;
  - no record (`codex exec --ephemeral`), several, a missing or malformed `CODEX_THREAD_ID`,
    or any record shape it does not know (the format is pinned to Codex 0.164 and fails
    closed on change);
  - any writable folder (every `path` entry with write access, `slash_tmp` as `/tmp`,
    `tmpdir` as the command's `TMPDIR`, and, except in a read-only session, every runtime
    workspace root) that is `/`, the account home or a folder above it, or that equals,
    contains or lies inside `~/.codex` (and so the copy), the orchestrator clone that
    installed the copy (recorded in its binding, format 2), a trusted installation folder,
    any hidden top-level home folder, the learning state as the commands use it
    (`$XDG_STATE_HOME/codex-agents`, only inside a hidden folder of HOME, or the account's
    `~/.local/state/codex-agents`, with its `scratch`), or a host-private scratch container (the bridge's
    `codex-agents-scratch` and the owner tools' `agent-orchestration-owner`, in the
    per-user cache folder on macOS or `$XDG_RUNTIME_DIR` on Linux); and a writable
    `special` root or home entry, or one it cannot place.
  - a writable folder that strictly contains the copy's bound project, or that is (or
    lies inside) the project's `.codex`, `.agents` or `.git`. The project itself stays
    allowed: the record lists `<root>/.git`, `.agents` and `.codex` as read entries for
    a workspace-write session, which Codex keeps read-only at the top of every writable
    root (on the synthetic Codex 0.164 fixtures; the real record was not re-read for this
    change). Because the guard runs before every rule-matched command, `dispatch`
    included, a session that could write the bridge's control files or exports never
    starts a bridged child.
  - Containment is decided component by component, for each path both as written (no
    symlink followed) and as it resolves now: two components are the same folder when
    their file-system identities (device and inode) match, and a component that does not
    exist is compared by name ignoring case. So a protected folder renamed and replaced by
    a symlink still protects its recorded path (and its new target), and a different
    spelling on a case-insensitive volume (`~/work` for `~/Work`) is the same folder; when
    unsure, the guard refuses.
  `host-command` reports the verdict as `session_guard` (`ok` or the reason) without
  changing `ready` or `codex_rule`, and the Codex skill stops on anything but `ok`. The
  wizard's first screen runs `codex-session-scan` from the clone, with the same parsing:
  records changed in the last 14 days (at most 200) in the folder Codex uses (`CODEX_HOME`,
  else `$HOME/.codex`), warning, with Continue or Quit, about a session that could write the
  clone, `$HOME` or the account home, or a folder above them, the learning state (the new
  folder, and the old macOS one while it exists), or a folder that strictly contains a
  set-up project ("Codex was opened on a folder that contains a set-up project (<name>);
  open Codex on the project itself"). Set-up projects are those the runtime copies'
  bindings name and the ledger's active registrations (read-only; skipped when
  unreadable; the installer's ownership records hold only a digest of the project path).
  Each finding carries `holds` (`home`, `clone`, `state` or `project`) and a plain
  `message`. The Claude primary's project CLI is unaffected.
- **Learning state location.** Until this change the default state on macOS was
  `~/Library/Application Support/codex-agents`, a non-hidden home subtree: a session
  opened on `~/Library` (for a project under it) could write the ledger, the digest key,
  the collector authorization and `owner-allowed-signers`, and same-user modes do not stop
  that. The default is now `~/.local/state/codex-agents` on every platform, with the home
  folder from the account database, never HOME (`host_paths.default_state_root`). The
  collector's socket path there is shorter than before.
  - **XDG_STATE_HOME.** An absolute value moves the state to `$XDG_STATE_HOME/codex-agents`
    only when it is canonical as written (no symbolic link at any component) and lies
    strictly inside a hidden top-level folder of HOME (such as `~/.local/state`); anything
    else refuses every command, the guard included: "XDG_STATE_HOME must be inside a hidden
    folder in your home, such as ~/.local/state; unset it or move it". A value such as
    `~/State` would put the ledger and the owner's signer in a folder a permitted session
    may be opened on. A relative value is ignored, as the XDG specification says. The
    check is anchored on HOME, not the account database, on purpose: HOME is the owner's
    own environment, which no model sets for a rule-matched command, a hook or an owner
    tool (an environment-assignment prefix breaks the rule's match), and tests isolate a
    scratch home with `HOME=<scratch>` and `XDG_STATE_HOME=<scratch>/.local/state`.
  - **Folders above the state.** Every existing folder between the home folder and the
    state (`~/.local`, `~/.local/state`) must be a real folder, the owner's and writable by
    no one else, or every command refuses; missing ones are created owner-only (0700) and
    checked again before the state itself is created.
  - **No second hard link.** Every state file the store checks (database and journals,
    digest key, owner allowed-signers) refuses a link count other than one, and the signer
    and digest key are also checked on the descriptor they are read from: a hard link in a
    project would otherwise keep write access to the file. The allowed-signers file must be
    exactly one canonical line of at most 4096 bytes, read whole (normal use, the staged
    copy and the move): `ssh-keygen` reads the whole file, so a valid-looking over-long
    first line must not hide a second key behind the part that was checked.
  - **One-time move.** Only the owner's `learning-register` (the wizard's collector step,
    run from the clone) moves the old folder, only while it is the only one, and only with
    the owner's approval: `--move-legacy-state <sha256 of the old owner-allowed-signers, or
    none>`. A session opened on `~/Library` could have created the old folder where none
    existed, so the wizard first shows (attest.py `legacy-state`) when it was created, the
    owner key its signer file names and whether that is the key in
    `~/.ssh/codex-agents-owner.pub`, asks "Move these records to the protected folder?",
    and passes the digest it showed. Without the flag the move refuses; the digest is
    checked against the old file and again against the fresh staged copy before anything
    is renamed, so a file changed after the review refuses. It checks
    it (owner-only, no symlink, outside Git), refuses if the new socket path would exceed
    the AF_UNIX limit (103 bytes on macOS, 107 on Linux) and prepares the new parent
    folders as above. Then, with the collector stopped and its maintenance lock held
    (`CollectorMaintenance`, as for registration) and the ledger's writer lock taken:
    - a file with a second hard link, a symbolic link or a special file in the old folder
      refuses with a plain message, before anything is copied;
    - every file is copied into fresh inodes (`O_EXCL|O_NOFOLLOW`, 0600, folders 0700) in
      an owner-only staging folder beside the new location, the ledger through SQLite's
      backup API page by page (it can be hundreds of megabytes) and checked with
      `quick_check`; the socket, lock files and transient `scratch` are not copied;
    - the staged copy passes the usual checks (owner, modes, link count, digest key,
      signer format, database files) and is renamed into place in one step;
    - the old folder is renamed beside itself to `codex-agents.moved-<UTC yyyymmddThhmmssZ>`,
      inert, and `state_moved` names it so the wizard can say it may be deleted.

    A rename keeps the old inodes, so a hard link or a writable descriptor taken on the
    old files before the move would have kept write access to the new state; the copy
    leaves those pointing at the old copy only. On any failure the staging folder is
    removed and the old folder is left as it was (a collector that was running is
    restarted there); if setting the old folder aside fails, the published copy is taken
    back. Registration then re-authorizes and starts the collector at the new folder. Both
    folders present refuses with a plain message; only the new one is normal. The old
    folder is found where the old code put it (HOME's `Library/Application Support`).
  - **Everything else fails closed.** Any other use of the default state (every command,
    the protected copy, the learning hooks, `learning-configure-owner-key`) refuses while
    only the old folder exists, telling the owner to run `./setup.sh` (collector step), so
    nothing starts a second, empty state beside it. Once moved, the old location is not
    special: the guard does not protect it.
  - **Owner tools.** `host_paths.owner_state_root()` is the folder that holds the owner's
    state now: the old macOS folder while it is the only one, else the default. The
    installer's ownership records and the attestation helper use it, so the install step,
    which runs before the collector step, never creates the new folder beside the old one.
- **Trusted installation folders.** The interpreter the rule names (and its standard
  library), every provider client and git must resolve under one of
  `host_paths.TRUSTED_INSTALL_ROOTS` (`/usr/bin`, `/bin`, `/usr/libexec`, `/usr/local`,
  `/opt/homebrew`, Linuxbrew's default prefix, `/Library/Frameworks/Python.framework`,
  `/Library/Developer/CommandLineTools`, `/Applications`, `/nix/store`) or in a hidden
  top-level folder of the account's home folder (`~/.local`, `~/.pyenv`, `~/.codex`),
  besides the existing checks (canonical, owned by the user or root, writable by no one
  else, outside the project and temporary directories, never the macOS `/usr/bin/python3`
  shim). A Linux system Python's prefix, `/usr` itself, is accepted. An allowlist rather
  than "outside the home folder", because a sibling such as `~/Work/tools` or
  `/srv/work/tools` could be part of a permitted session's workspace. The list is one
  constant that the owner tools import too, so both sides apply the same rule.
- **Protected rule target.** The S5b rule named `<project>/.codex/orchestration/cli.py`.
  A session opened on a parent folder, or one where `.codex` is absent, can rewrite that
  file, and the user-layer rule would then run it outside the sandbox.
  - `install-codex-rule` (owner) now copies the runtime to
    `~/.codex/agent-orchestration/<project digest>/<runtime digest>/`, mirroring the
    project layout. The copy holds exactly this package's runtime files
    (`codex_agents/runtime_files.py`: `cli.py`, every `codex_agents` module, the
    configuration and the schemas), a manifest of exactly those files and a binding
    (`.codex/orchestration/runtime-binding.json`: format, project root, runtime digest,
    version). The file set is a constant of the code doing the install, never the
    project's install manifest, which a parent-folder session could extend with a
    planted module and its digest (a top-level `socket.py` would shadow the standard
    library on the copy's import path).
  - The runtime digest covers every file the copy holds, and it is also the collector's
    runtime identity: `COLLECTOR_RUNTIME_DIGEST`, `validate_collector_runtime` and
    `runtime-releases.json` use the same set. Signed release data
    (`config/learning-releases.json`) is not code; the copy reads its project's.
  - Before publishing, the project's runtime must pass its manifest digests and equal
    this package's recorded runtime and version, and its `.codex/orchestration` may hold
    nothing but the package's files and the project's own configuration: a planted module,
    a package, bytecode or an extension module refuses. The learning hooks and the Claude
    skill now launch Python with the isolation flags, so neither writes bytecode there.
    A project without an install manifest gets neither copy nor rule.
  - The copy is staged in an owner-only sibling created through no-follow descriptors
    (`open_private_directories`), verified byte for byte against the project's files, and
    renamed into place, so the rule never names a partial copy.
  - The rule keeps the `[interpreter, isolation flags, cli]` shape and names the copy's
    `cli.py`; its `not_match` examples include the project's own `cli.py` and the old
    prefix. After the rule names a copy, the project's older copies are removed.
  - **No bytecode read.** `-B` only stops Python writing bytecode; CPython 3.14 still
    loaded a planted timestamp `.pyc` from `__pycache__` beside a module. The isolation
    flags are now `-I -B -S -X pycache_prefix=/dev/null` for the rule, `argv_prefix`, the
    collector, the deadline watchdog and the hooks. A rule without the new flag is `stale`.
  - The copy's CLI first checks, before its directory goes on the import path, that the
    tree holds exactly the files its manifest lists, so an extra module there is never
    imported. It then reads its binding: the copy must sit at exactly the path the binding
    implies. Its `BUNDLE_ROOT` is the bound project, so the own-root invariant pins
    `--project-root` and `--root` to the project, never to the copy's location. It runs
    only the primary-host commands (and `learning-serve`, the collector it starts), and
    only while the project's runtime files are byte for byte the copy.
  - `host-command` reports `installed` only when the rule file is a fresh render and the
    copy exists, is bound to this project, holds nothing extra and equals the project's
    runtime; `missing` without a rule file; `unsupported` (with a `reason`) for a Codex
    home other than the real `~/.codex`; otherwise `stale`, including a rule naming the
    project's `cli.py` and a project without a manifest. Its `argv_prefix` names the copy.
  - `remove-codex-rule --project-root <root>` (owner) deletes the rule and every copy for
    the project, without following symlinks, and prints
    `{"ready": true, "removed": [<paths>]}` (empty when there was nothing). It needs only
    the resolved project path, so it works for a temporary project and after the package
    files are gone; it also removes a rule an earlier version wrote to another
    `CODEX_HOME`. The installer's remove step calls it.
  - **Only the real `~/.codex`.** A session opened at the home folder has `~/.codex` as
    its workspace root's `.codex`, which Codex keeps read-only, and a session opened on a
    project cannot write it at all; the rules directory sits there too, so the copy adds
    no new exposure. The home folder is the account's, from the system account database
    (`pwd`), never HOME alone: a HOME or `CODEX_HOME` that disagrees with it, a `~/.codex`
    that is a symlink, a home folder reached through a symlink and a `~/.codex` another
    user owns or can change are all refused with a plain reason.
- **Real git, no xcrun shim.** On macOS `/usr/bin/git` is an xcrun shim that consults a
  lookup cache under `$TMPDIR`, which the sandbox can write. `git_executable()` resolves a
  real binary: the Command Line Tools git, then the active developer directory's (from
  xcode-select's root-owned link), Xcode's, then Homebrew's; `/usr/bin/git` on Linux.
  Each is pinned as its realpath and must pass `trusted_location_problem` with the
  temporary roots and the repository being inspected untrusted; none passing refuses.
  `xcrun_nocache=1` is set for git, provider clients and operational checks. `ps`,
  `getconf`, `sandbox-exec` and `ssh-keygen` are real binaries; a darwin `/usr/bin`
  interpreter (the `python3` shim) is refused for the rule.
- **Trusted provider executables.** `resolve_client` followed the fixed search path
  through symlinks but only hashed the target. Before anything executes, the resolved
  client must now pass the same location and ancestor checks, with the temporary roots and
  the registered project root untrusted (`client_unavailable` otherwise), so a link into
  a writable checkout is never run.
- **Worker-result retention.** S5 kept a verification-required result's raw JSON for its
  verifier with the default 90-day retention. It now lives only while its orchestration is
  open, and at most seven days after its outcome was recorded. Closing the orchestration
  deletes it; every ledger start clamps each kept result's expiry to its outcome's
  ingestion time plus seven days (which shortens results an earlier version kept for 90)
  and deletes what is past it; the persistent collector runs the same sweep every minute;
  and a read past the limit refuses even before a sweep. A registration is removed only
  after its file is gone, so a failed deletion stays registered (and expired, so never
  read) and is retried at the next sweep. Each collector request must arrive whole within
  five seconds, so a slow sender cannot postpone a sweep. Project deletion removes it as
  before. Only the outcome's digests and closed metadata stay.
- **Codex children are bridged.** A native Codex child shares the Codex primary's session
  and therefore its command rule. Routing now dispatches every Codex child through the
  bridge (`codex exec`, read-only, `--ignore-rules`), from either primary; only a Claude
  primary spawns natively, and only Claude children. Bridged Codex-from-Codex uses the
  same `dispatch` path as a Claude primary, which already runs outside the sandbox
  through the rule. Consequences: a Codex route that needs `workspace-write` is never
  eligible until write containment exists, and the legacy GPT-5.6 Sol fallback, which
  answers a native host's model rejection, is refused for a bridged route.
- **Bridged children get the whole assignment.** A child's prompt carries the objective,
  each required criterion's id and description (the template alone names only ids), and
  the result template. A Codex child also gets the routed role's developer instructions as
  `-c developer_instructions=...`, which codex-cli 0.164.0 renders as a developer message
  (checked with `codex debug prompt-input`, which makes no model call). They are read from
  the role's agent file (packaged or mapped) and refused unless the file and the
  instructions hash to the run's recorded execution configuration, so a bridged child
  runs the configuration the ledger binds.
- **Bridged results are bound.** `dispatch` validates the child's result and fences one
  that does not validate or would be too large to keep (`invalid_result`,
  `result_too_large`). It then records a `dispatch_result` lifecycle record: a keyed
  digest of the result it returns, never its text, with the fenced, reviewed and
  terminal-state facts. `commit_host_result` for a bridged run requires exactly that
  result and terminal state, so a primary cannot record a made-up verifier result or
  drop or invent a fence.
- **Options end before the prompt.** Both bridged launches and the billing probe pass
  `--` before the prompt, which starts with the assignment's objective. Without it,
  `codex exec` read an objective such as `-cfoo=bar` as a configuration override
  (checked on codex-cli 0.164.0 with no credentials); Claude's option parser treats `--`
  the same way (its operands after `--` are never options).
- **Owner tools (setup, attestation, the installer).**
  - Owner commands (registration, status, attestation, `host-command`,
    `install-codex-rule`, `remove-codex-rule`) run this repository's own CLI with
    `--project-root`, never the project's `.codex/orchestration` code; the clone must
    therefore sit outside every folder opened in Codex (the wizard's first screen and the
    README say so). Steps that depend on the install still require it to be this release,
    byte for byte, with nothing extra in `.codex/orchestration`.
  - A collector that setup starts through `learning-register` runs the clone's
    `cli.py learning-serve`; authorization is by version and runtime digest, so a primary
    that later restarts it runs its own (protected) copy of the same runtime.
  - The interpreter is judged before it runs, against `host_paths.TRUSTED_INSTALL_ROOTS`
    and `install_location_problem` (setup.sh keeps a bash copy of the list, kept equal by a
    test), plus the temporary-folder, project, virtual-environment and xcrun-shim checks.
  - Git on the project is plumbing only: state from `ls-files`/`ls-tree` and byte
    comparison, commits through `hash-object -w --no-filters`, `update-index`, `write-tree`,
    `commit-tree --no-gpg-sign` and `update-ref` with the old value, with
    `GIT_ALLOW_PROTOCOL=""`, `GIT_NO_LAZY_FETCH=1` and `repository_layout_problem` refusing
    partial clones; the repository-helper check stays as a second layer.
  - The installer's record of the deny rules it added lives in the owner's learning state
    (`installer/claude-settings-<project digest>.json`), never in the project; an entry not
    recorded there is never removed. When an owner-only CLI subcommand is retired, add its
    name to `scripts/install.py` `HISTORICAL_OWNER_ONLY` so its old deny rules are still
    recognised as the installer's.
  - The owner key: `attest.py` takes a private key only from the account's `~/.ssh`, with no
    other hard link, and each operation that uses it (the passphrase check with the
    derivation, signing, the passphrase change) first reads it once through a no-follow
    descriptor into a fresh 0600 file in a new owner-only folder of the owner's scratch
    container, runs every `ssh-keygen` step on that copy only and removes it when the
    operation ends. A program that kept a descriptor or mapping on the key's inode (opened
    through a link in a folder it could write before the key was moved) cannot change what
    was checked before it is used. The passphrase change re-encrypts the copy, checks it with
    the new passphrase, and only then writes it to a fresh 0600 file in `~/.ssh` that is
    synced and renamed over the key, so the old inode is replaced rather than edited: a
    retained descriptor on it then only ever writes to a file the tools no longer use.
  - Residual: the settings file is staged in that owner folder and renamed into place after
    re-comparing the destination's bytes; an edit landing in the sub-millisecond window
    between that comparison and the rename is overwritten (accepted). When the state folder
    is on another file system, the file is staged beside the destination under a random
    name instead.
- **Learning hooks rendered per checkout.** Codex 0.164 runs every hook command on the host,
  outside the sandbox, as `<session shell> -c "<command>"` (zsh, bash or sh; `$SHELL -lc` or
  `/bin/sh -lc` when no shell is known), in the session's working directory, which may be a
  subfolder, with no project-root variable for project hooks
  (`codex-rs/hooks/src/engine/command_runner.rs` `build_command`;
  `core/src/session/mod.rs` `build_hooks_config`). The static file ran `python3` from `PATH`
  and found the hook with `git rev-parse`.
  - The installer renders `.codex/hooks.json` (`hook_config.render_hooks`): the same nine
    events, async set and timeout 3, each running
    `/usr/bin/env xcrun_nocache=1 '<interpreter>' -I -B -S -X pycache_prefix=/dev/null
    '<root>/.codex/orchestration/learning_hook.py' --checkout '<root>' --client-version
    codex-hooks-v1`. The
    interpreter is the one the installer runs under, which `./setup.sh` selected, checked as
    for the rule (`interpreter_path`, `executable_location_problem`, not the xcrun shim). Each
    path is single-quoted and may not hold a quote, a backslash or a control character, so
    sh, bash, zsh and fish read the same words.
  - The file holds absolute paths, which would leak a home path into a pushed project and
    break every other clone, so it is never committed. No documented project-root variable
    exists (option a), so the package's committed `.codex/.gitignore` ignores it and
    `.codex/orchestration/local-install.json` (the recorded interpreter and the file's digest)
    in every clone, without writing into `.git`. The installer owns both through that local
    record: update and remove replace or delete them only at the recorded digest, and an
    update replaces 0.8.x's committed static file, whose deletion the wizard's plumbing commit
    records while the rendered file stays. `project_changes` never reports an untracked one.
  - Linked worktrees and nested repositories: Codex 0.164 loads a linked worktree's hooks
    from the matching folder of its main checkout (`config/src/loader/mod.rs`
    `root_checkout_hooks_folder_for_dir`), so events from `.worktrees/<name>` run the main
    checkout's rendered command. `--checkout` pins the checkout it was rendered for, and
    `learning_hook.py`'s first action, with the standard library only and before importing
    any of the package, is `belongs_to_checkout`: the payload's `cwd` must exist and,
    resolved strictly, be the checkout or lie inside it, and the nearest `.git` entry (folder
    or file, read with `lstat`) walking up from it must be the checkout's own. Only a missing
    `.git` counts as absent: any other error, such as a folder whose search permission a
    session removed, refuses, so an inaccessible worktree boundary is never walked past. A linked worktree (inside or outside the checkout), a
    nested repository or anything else is a silent no-op (`{}` for `Stop` and
    `SubagentStop`) that imports nothing, starts no collector and records nothing; an
    accepted event binds the pinned checkout, so a session in a subfolder records for it.
    No registration check exists short of the collector (the only one, a read-only ledger
    query, would open the database on every event), so the checkout guard is what keeps a
    foreign event from starting the collector. A separate clone gets its own file when the
    wizard installs it there.
  - Verify (`validation._validate_learning_hooks`, a project error) keeps every structural
    check, then requires the local record, re-checks the recorded interpreter, and compares
    the file byte for byte with `render_hooks(interpreter, root)`; the old `$(git rev-parse)`
    form, an edited command, a moved project or an untrusted interpreter says to run
    `./setup.sh`. Codex trusts each hook by its hash, so after an install or update the
    wizard says "In Codex, open /hooks and trust the orchestrator's learning hooks"; the
    installer never writes trust.
  - `hook_config.py` is runtime code (validation imports it). `learning_hook.py` stays
    package data: hooks run the project's installed hook, not the copy. Once installed, the
    project's `.codex/orchestration` is protected like `hooks.json` itself (Codex keeps
    `<project>/.codex` read-only for a session opened on the project, and the guard refuses
    sessions that could write it from above), except from a process that was already
    writing the project before the install, when `.codex` did not yet exist and so was not
    excluded (see the residual risks). Nothing on the hook's path uses `PATH` or git;
    `ensure_collector` starts the collector with `sys.executable` (the trusted interpreter)
    and `xcrun_nocache=1`.
  - Planted modules: the installer (install and update) refuses before writing anything,
    and verify reports it, when `.codex/orchestration` holds any entry other than this
    package's runtime and data files, the project's own files and files the install
    manifest owns ("<project>/.codex/orchestration holds files the orchestrator does not
    install: …; remove them and run ./setup.sh again"). The hook and the project's
    `cli.py` append their folder to `sys.path` instead of putting it first: run with `-I`,
    the script's folder is not on the path, so a planted `socket.py` can never shadow the
    standard library. The wizard tells the owner to trust the hooks in Codex only after
    `check-runtime` passes (the installed runtime is this release, byte for byte, with
    nothing extra), and reminds them at the start of the install step to close every Codex
    session in the project first.
  - **v1.1 candidate:** run the hooks from a guard-protected owner copy (as the rule runs
    the CLI) rather than from the project's installed code.
- **Native admission (accepted residual).** A Claude primary's native children run in the
  primary's own session and allowance; admission for them is accounting and policy, not
  a spend boundary, and the native tool consumes no admission capability. A primary that
  ignores a denial spends only its own session's allowance. Codex children cannot be
  routed natively any more.
- **Residual risks.**
  - The session guard checks the calling session's own record, which lives in
    `~/.codex`: a session opened on `/` or `/Users` (or given `~/.codex` with `--add-dir`)
    can write that record and forge its settings before running a command, as it can the
    copy and the rules. The guard stops the ordinary mistake (Codex opened on the home
    folder, a parent of the clone, or with `--add-dir ~/.local`), not a session that
    already writes `~/.codex`. The record format is Codex 0.164's; a change fails closed. A session opened on a trusted installation folder itself (say
    `/opt/homebrew` or `~/.pyenv`) could change the tools there too; nobody opens one
    there, and no check can tell.
  - A Codex primary can still spawn a native Codex child with its own collaboration tool,
    ignoring the route; that child shares the primary's session and rule. Routing never
    asks for one and the Codex skill forbids it, but nothing at the spawn boundary stops
    it. Accepted: the primary already holds the rule's authority itself, and such a child
    is still held to this project and the owner's state by the own-root invariant and
    every run it opens to ledger admission.
  - A session opened on a folder that contains a set-up project is refused by the guard,
    but the guard only refuses commands: such a session can still write the project's
    `.codex` (its hooks and `config.toml`, which Codex runs outside the sandbox when the
    project is trusted), `.codex/orchestration/**` and `.git/config` directly, and no
    package code can stop that. The wizard warns about recent ones, and the documentation
    says to open Codex on the project itself.
  - The learning hooks run the project's installed `learning_hook.py` (and the modules it
    imports), not the protected copy. Close every Codex session in the project before
    installing or updating: a process started in a sandbox before the install, when
    `.codex` did not yet exist and so was not excluded from its writable root, can still
    rewrite that code afterwards, and nothing in the package can detect a change made after
    the last check. A rendered hooks file names the interpreter's
    realpath: a Python upgrade that moves it breaks the hooks until `./setup.sh` runs again
    (verify reports it). Codex re-trusts hooks only by hash, so the owner must re-trust them
    after each render.
  - A session that could write the old macOS state folder before the owner's collector
    step moved it could have changed that state, or created it where none existed; the
    owner reviews its creation time and owner key before approving the move, which then
    carries the contents over as they are (in fresh files, so no link or descriptor it kept
    reaches them).
  - A workspace-write Codex session in the project can rewrite `.claude/settings*.json`
    (its hooks and deny rules) and `.claude/skills/**`, which Claude Code loads and runs on
    the host; the installer's deny rules are not a boundary against that session. Verify
    warns when `.claude/settings.json` defines hooks, which the installer never writes.
  - The old copy (`codex-agents.moved-<time>`, including the old digest key, which is the
    one the new state keeps using) stays readable where it was until the owner deletes
    it; the wizard's collector step names it and says it can be deleted.
  - The XDG_STATE_HOME check trusts HOME, the owner's environment, as the anchor.
  - The scan is a warning: it follows only records from the last 14 days, and the set-up
    projects it knows are the runtime bindings and the ledger's registrations.
  - The copy is verified against the project and this package's recorded runtime, not
    against an owner signature: an owner who installs a modified package gets that
    package copied.
  - Raw worker results stay on disk for the orchestration's life (at most seven days, and
    up to a minute past that until the collector's sweep), owner-only but readable by any
    sandboxed session, as all host files are.
  - Native Claude children run with the primary's permissions, so their reads are advisory
    (S3); Codex reads are not contained until G3 (S2).

## Deferred to G4

- Headroom tie-breaking between equally ranked candidates.
- Per-project candidate overrides.
- Quality-evidence ranking.

G2 records the headroom data these will need.

## Codex primary network

A Codex primary can only launch a Claude child if its sandbox allows network. The options were:

- **(a)** Enable network for the pilot project's Codex sandbox.
- **(b)** Keep network off and have an exec-policy rule auto-approve an escalation for the bridge
  command only.

What ships is a scoped form of (a). A project opts in with `codex_primary_may_bridge_to_claude`
(setup step 3, off by default), and only sessions started with `codex -p orchestrator` get sandbox
network, from `~/.codex/orchestrator.config.toml` (`sandbox_mode = "workspace-write"`,
`network_access = true`; setup step 7). Ordinary sessions keep network off, and the user's main
`config.toml` is never edited.
