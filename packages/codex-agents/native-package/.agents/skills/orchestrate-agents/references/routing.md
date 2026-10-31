# Routing

Use the versioned policy at `.codex/orchestration/config/routing-policy.json` through
the installed CLI. The policy is quality-first and produces a candidate route from
observable task fields. Model labels are not evidence that a task will pass.

Use a single-agent route for simple or sequential work. Use parallel workers only when
assignments are independent. Packaged children are read-only. Explicit project
mappings may select workspace-write coding and verification agents within the active
parent grant. Use the primary agent as manager when one owner must preserve requirements
and synthesize results.

The accepted version 5 routing policy lists, for each family and route key, an ordered
list of provider candidates (Codex and Claude models with their efforts); the route names
the first candidate that passes the static eligibility checks. Follow the route's
provider, model and effort rather than any summary. GPT-5.6 Sol/high remains the sole
legacy fallback, available only through the trusted, finite, single-use receipt
lifecycle after a native GPT-6 host rejection.

Every task supplies operation, ambiguity, context breadth, acceptance strength, and
safe routing-evidence references. Unknown ambiguity or context breadth routes to a
read-only Luna/high reconnaissance stage. The manager verifies that result, then
reroutes the clarified task; reconnaissance never implements the original objective.

Risk never selects a model, effort, or topology or widens authority. High and critical tasks must
be meaningful and retain independent QA plus operational security
checks. Passing routine QA does not require owner review. Astra evidence must be an
`outcome:<sha256>` reference resolved by the ledger to QA-accepted reconnaissance for
the same project, task identifier, and family. Candidate alternatives are recorded
only for matched evaluation. They cannot replace the selected live route; the sole
exception is the policy-pinned GPT-5.6 Sol fallback after the ledger validates and
consumes its exact host-unavailability receipt.

Result-contract, schema, formatting, transient, corrected-input, authority, semantic,
and reasoning failure history does not trigger model escalation. Project-bound
escalation from verifier history is unavailable until the route can resolve each
reference to an actual independent verifier record. Raw prompts, outputs, secrets, and
unrestricted tool data are never failure evidence. Escalation never grants more tools
or authority.

Version 5 also lists Claude models and roles and, for each family and route key, an
ordered list of provider candidates. The first candidate that the committed project
configuration allows is selected; if none is allowed, routing is denied rather than
falling back. `.codex/orchestration/provider-routing.json` is optional. Without it,
only Codex is enabled, so every route is Codex with `native` dispatch. A Codex primary
reaches Claude only when that file sets `codex_primary_may_bridge_to_claude`. Each route
records its `provider`, its `dispatch` (`native` or `bridge`) and the digest of the
provider configuration it used. The verifier prefers the other provider, then the
worker's own. A verification task that sets `verification_for_provider` to the worker's
provider is routed by the same rule, so it matches the advertised verifier, including
under the legacy fallback. Routing requires the project to be a Git repository: when
the provider configuration is absent, defaults apply only if `HEAD` is unborn or does
not track it, and any other Git outcome blocks routing.
Learning candidates cannot move a route to another provider.

The account catalog of the policy's `validated_client` recognizes `low`, `medium`,
`high`, `xhigh`, and `max` for active models, while initial routes use medium or high.
Native routing excludes `none` and `ultra`.

## Host client

The lifecycle detects its primary from the host's environment: Codex shells set
`CODEX_THREAD_ID` or `CODEX_SESSION_ID`, and Claude Code sets `CLAUDECODE=1` with
`CLAUDE_CODE_SESSION_ID`. Codex markers without `CLAUDECODE` mean `codex-host-v1` (a
Codex primary); Claude markers without Codex thread markers mean `claude-host-v1` (a
Claude primary). Both or neither refuse with `host_client_unknown`, and an explicit
`--host-client` must match. The host that opens an orchestration owns it: every later mutation (open, bind,
observe, abandon, checkpoint, pause, resume, close, dispatch and record) from another
host is refused with `host_client_mismatch`, and legacy recording without a run handle
is a Codex-host path only. A Claude-host open is denied
`claude_backend_not_subscription` when the session environment or the user, project,
local or managed Claude settings point at a metered backend or another credential. A
native Claude open is also denied `claude_model_override_present` when a model or
effort override is set, and it returns `native_spawn` (agent, model alias and effort)
computed from the route. Claude's billing evidence for native children comes from
`provider-observe --provider claude --billing-probe`, one minimal locked Haiku turn
whose rate-limit events are the only thing recorded, merged into one conservative
reading (highest status, latest reset, billing at its most restrictive). The probe spends
a little, so it is admitted like a run first (no API key or alternate backend, a fresh
identity, a `claude.ai` subscription, an owner attestation, no paid usage and no
exhausted window; older clean billing is enough, since the probe refreshes it), its
decision is stored, and it shares the single per-account bootstrap allowance with runs.
Account billing evidence under five minutes old is reused (`probe: "reused"`) instead of
probing again; otherwise a probe for the same account in the last five minutes, in any
project and whatever it returned, refuses with `probe_cooldown`. Each event is parsed on
its own, and the merge of every valid event is stored. When the probe aborts, times out,
overflows or reports an invalid event, only the merge's restrictive parts are stored
(billing not shown off, or a non-ample window), so an incomplete probe never refreshes
clean evidence; the probe still reports failure. Claude windows use the Codex thresholds: 90 percent is exhausted and 75 percent
constrained. The markers only catch accidental
misuse; they are not proof of identity. Admission, the bridge flag and the read-only sandbox are the
authority gates. The Claude primary's skill is `.claude/skills/agent-orchestration`,
installed with this package.

## Command boundary

A Codex primary runs the CLI inside Codex's workspace-write sandbox, which denies the
learning collector's local socket and the writes its state needs, so collector-backed
commands fail there (`filesystem operation failed`, plus a `hint` under a Codex host).
`./setup.sh` (Codex commands step) offers, with the owner's consent, one Codex command
rule per project in the user's rules layer:
`${CODEX_HOME:-~/.codex}/rules/agent-orchestration-<12 hex digits>.rules`, named by a
digest of the resolved project root. It allows, outside the sandbox and without a prompt,
exactly the resolved base interpreter (never a virtual environment's, and refused when it
or its standard library is in the project, in a temporary directory, behind a symlink or
writable by another user), the isolation flags `-I -B -S`, the absolute project `cli.py`,
then one of the primary-host subcommands (`PRIMARY_HOST_COMMANDS` in
`codex_agents/host_commands.py`). A project in a temporary directory never gets a rule.
Owner commands (registration, attestation, owner keys, grants, backups, canaries,
promotion, rollback, upgrade reconciliation and `install-codex-rule` itself) are not
listed and stay sandboxed. `host-command --project-root <exact-root>` reads only and needs
no collector. It prints `argv_prefix` and `codex_rule`: `installed`, `missing`, or `stale`
when the file differs from what setup would write now, as after a Python or package
change, or is a symlink. The rule leaves every argument after the subcommand to the CLI,
so for these commands an installed CLI refuses a `--project-root` or `--root` other than
its own project, a `--state-root` other than the default learning state, and any
`--policy`, before doing any other work. Commands it runs outside the sandbox never use a
sandbox-writable location: trust checks read the repository with git plumbing only (no
index refresh, fsmonitor, filters, diff drivers, hooks, pager, transport or lazy fetch)
and refuse a git directory inside the working tree or in a temporary directory and a
partial clone; the collector and watchdog start with `-I -B -S`; bridge exports, control
files and child temporary directories live in an owner-only directory under the per-user
cache directory on macOS (`$XDG_RUNTIME_DIR` elsewhere); operational-check snapshots and
provider clients' temporary directories live under the learning state. A Claude primary
does not use the rule. Native Codex children share the primary's Codex session, so the
rule matches their commands too: a native child could run the primary-host commands
outside its own sandbox, still held to this project and the owner's state by the same
refusals and to ledger admission. Bridged Codex children start with `--ignore-rules` and
load no rule.

## Provider admission

`learning-open-host-run` admits a run for its route's provider before opening it, and a
verifier is admitted when its own run is opened. The checks run in this order, and the
first failure denies with a stable `reason_code`:

1. `provider_api_key_present`: `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` is set.
2. `admission_policy_unavailable`: the installed routing policy has no usable admission
   settings for the provider (`providers.<p>.client_versions` and
   `max_admissions_per_window`).
3. `provider_identity_missing`: no `provider-observe` identity in the last 30 minutes.
4. `client_version_unreviewed`: the client version that identity recorded (from
   `claude --version` or `codex --version`) is missing, unparsable, or outside the
   policy's reviewed range, `minimum` inclusive to `maximum_exclusive`.
5. `billing_attestation_missing`: no owner-signed attestation for that account
   fingerprint, recorded with `learning-record-billing-attestation`.
6. `claude_subscription_missing`: for Claude, the login is not `claude.ai` with a
   subscription. `codex_plan_not_subscription`: for Codex, the plan type is absent,
   unknown, `free`, or a usage-based or consumption-priced plan.
7. `paid_usage_usable` or `billing_unverified`: any billing observation in the last day
   shows paid usage usable or overage in use, or the latest cannot show it is off.
   Both providers need a billing reading from the last 30 minutes. Claude has no
   non-spending billing source, so one bootstrap run per account (across every
   project) may open on the identity and attestation alone; after that, Claude needs a
   recent run's rate-limit event or a billing probe. Billing, headroom and the bootstrap
   are the account's, read from every project of the ledger; a decision cites only its
   own project's records, and attestations stay per project. A project holding a live
   account restriction (paid usage in the last day, an unreset exhausted window, a probe
   cooldown or admissions that may still count under a cap) cannot be deleted
   (`project_holds_account_restrictions`).
   Codex reports credits as `hasCredits`, `unlimited` and `balance`, which cannot show
   that spending is off, so credits count as paid usage by default. An owner-signed
   attestation for the same account fingerprint may add
   `codex_credits_spending_disabled: true` (Codex grants only, never false). Then credits
   count as not usable if the reading shows `hasCredits: true`, `unlimited: false`, and
   `spendControlReached` is not `true`, at admission and at the launch re-check.
   `unlimited: true` and an attestation without the field still deny. The older
   attestation form stays valid. `codex_spend_control_reached`: any Codex reading in the
   last day reports `spendControlReached: true`; this denies whatever the attestation says.
8. `provider_exhausted`: the most restrictive headroom that has not reset yet is
   exhausted. That is Codex at 90 percent or more on any window, or ordinary usage not
   allowed, and Claude with a rejected or full window. A later favourable observation
   never clears a restriction that still applies.
9. `window_run_cap_reached`: the account already has `max_admissions_per_window`
   passing run admissions for the provider in the current five-hour window, counted
   across every project in the ledger. The window starts five hours before a recorded
   five-hour reset still ahead (the earliest such start among live readings from every
   project, so a later reading never frees places), or is the last five hours when no reset in the next
   five hours is known. Billing probes do not count. The count and the decision are
   taken under the ledger's write lock, on the ledger's clock read under that lock.

The ledger evaluates admission itself, from its own records and its own environment,
and stores the decision and the digests of the records it used with the run. No caller
can supply a decision. A collector-bound Codex session is admitted once, when it first
binds. Every result commit, including that legacy path, requires the run's stored
passing decision and never re-evaluates time-varying facts.

Observations are written only by `provider-observe`, which runs the provider client
itself, and by the bridge. The bridge's rate-limit event recorder accepts events only
while a Claude run is open, stamps them with the ledger's clock, and keeps each one that
is more restrictive than the run's earlier events, so a rejection late in a run blocks
the next admission until it resets. Only an earlier event at least as restrictive in
status can cover a window. At 64 records the run's restriction is merged into one
record (the highest status with the latest reset, a window without a reset counting
for a day from its reading), the run is stopped, and later
events are refused. A failed event is recorded as unavailable. The bridge's own observations carry the run and a
phase (`before` or `after` launch; in-run events are `during`), a reading that fails is
stored as `observation_unavailable`, and the run's client-observed usage is stored as
`provider_usage`. Provider clients are found only
on a fixed trusted path (`/opt/homebrew/bin`, `/usr/local/bin`, `/usr/bin`, `/bin` and
`~/.local/bin`), never through the caller's `PATH`. They run with a minimal environment,
and the identity record keeps the client's real path and SHA-256 locally for audit. No producer, hook or collector
client can append them. Identities are stored only as keyed fingerprints, and labels
such as plan and subscription are allow-listed, so anything else is stored as null.
Every project, including a Codex-only one, needs an attestation before host runs open.

## Bridge dispatch

`dispatch` runs one opened, admitted host run whose route has `dispatch: bridge` as a
read-only child of the route's provider, and prints the child's `result` with a
separate `bridge_report`. It refuses a native route, a writable sandbox or grant, an
assignment that does not validate, a run handle without a passing admission for the
route's provider, a route, task or assignment that differs from the opened run, a
provider API key, and a run handle that was already dispatched.

- The child never runs in the repository. The committed `HEAD` bytes of the
  assignment's `context` and `owned_paths` are exported into a private directory under
  the resolved system temp directory, which is removed afterwards. Paths must be plain
  literals: pathspec magic (a leading `:`, or any of `*?[\`) refuses, git runs with
  literal pathspecs, and a listed path outside the request refuses. Every exported path
  must be covered by an assignment filesystem permission (`<path>:read` or
  `<path>:write`, covering that path and anything beneath it by whole path components);
  otherwise `export_path_not_granted` refuses. Symlinks, submodules, missing paths,
  traversal, absolute paths and oversize exports refuse.
- `CLAUDE.md`, `CLAUDE.local.md`, `.claude/` and `.mcp.json` are never exported: naming
  one refuses, and directory exports drop them. A Claude child is refused with
  `context_boundary_violation` if any directory from its workspace up to `/` holds
  `CLAUDE.md`, `CLAUDE.local.md` or `.claude`; there is no exception for `~/.claude`,
  which is why exports live outside home. Enterprise managed settings, and on macOS the
  Claude Code managed-preference plists under `/Library/Managed Preferences` (global or
  for the account), refuse a Claude launch with `managed_settings_present`, because they
  apply despite `--setting-sources project` and the init frame cannot show them; the
  plists also refuse Claude-host opens and probes.
- The account is observed again immediately before launch and must match the admitted
  fingerprint and the admitted client version, which must still be in the reviewed
  range; the child is launched from the client that re-check read. Billing, headroom
  and the Codex plan are re-checked account-wide too, including that fresh reading
  (`launch denied: <reason_code>`, stored as a denial); billing must be from the last
  30 minutes, unless the run was admitted as the Claude bootstrap and the account has
  no billing yet, so a late dispatch probes first. The run cap is not counted again. A durable `dispatch_intent` is then written, and the bridge run ID is bound as
  the run's agent; there is no second launch for that run. A bridged run binds no other
  agent ID, and its result cannot be committed without that `dispatch_intent`.
- The child runs in its own session. The tighter of the route's and the assignment's
  `timeout_seconds` is a hard deadline, and the tighter `max_tool_calls` sets Claude's
  `--max-turns`. On timeout, on a contract violation, on an over-long line and after it
  exits, its process group and every descendant tracked while it ran are killed before
  the child is reaped, so an exit first seen after the deadline counts as a timeout.
  Descendants in other groups or sessions are tracked by ancestry every 0.25 s: `ps`
  lists candidates, and adoption and every signal rest on the process's own kernel
  record (parent, group and birth time read together), so an unrelated process is never
  killed; one that detaches and loses its parent
  between two snapshots is left running inside the child's sandbox. Without a readable
  process table the child is not launched (`process_tracking_unavailable`). `dispatch`
  kills the tree on SIGTERM, SIGINT or SIGHUP. A detached watchdog verifies the child's
  identity before the run continues (`watchdog_unavailable` otherwise) and kills the
  tree at the deadline even if the bridge itself is killed. Any outcome the bridge
  cannot prove (timeout, crash, a broken stream or no parseable result) returns a failed
  or timed-out result, never success.
- Claude children run with only Read, Glob and Grep, `dontAsk` permissions, generated
  settings with hooks and built-in plugins off and the model pinned, no MCP servers and
  no session persistence. The run fails closed unless the init frame reports no API
  key, the requested model, no plugins, no MCP servers and only those tools. Every
  `rate_limit_event` goes to the ledger, which keeps the more restrictive ones. A kept
  reading showing paid usage or overage stops the child (`paid_usage_observed`), and so
  does a rejected status (`provider_rejected`). Any model other than the requested one
  in an assistant frame or the result's `modelUsage` (a fallback) leaves the result
  unreviewed with an unknown verdict. `read_containment` is `enforced`.
- Codex children run `codex exec` read-only and ephemeral, between non-spending
  observations taken before and after. They load no user configuration and no
  execpolicy rules (`--ignore-user-config --ignore-rules`), so the owner's command rule
  never lets a child run a command outside its sandbox; a client whose `codex exec
  --help` lacks either option is refused before launch (`codex_exec_options_unavailable`).
  `approval_policy="never"` is pinned, so no escalation can be granted. Codex system or
  managed configuration (a TOML file in `/etc/codex`, `~/.codex/managed_config.toml`, or
  a `com.openai.codex` managed-preferences plist on macOS) refuses a Codex dispatch
  (`codex_managed_config_present`), since it applies despite `--ignore-user-config`.
  The launch disables account connectors, plugins, multi-agent, image generation,
  goals, browser and computer use, hooks and skill MCP dependency installs
  (`--disable <feature>`, never `code_mode_host`) and web search
  (`web_search="disabled"`). The feature disables plus `--ephemeral` are the pre-dispatch denial (on
  codex-cli 0.164.0, `spawn_agent` fails before any sub-agent run because no rollout
  exists), so the reviewed Codex range is pinned to 0.164.0 and widening it needs a
  live containment check on the new build. The event stream is allowlisted only to
  detect emitted, typed events: only the events `thread.started`, `turn.started`,
  `turn.completed`, `turn.failed`, `error` and `item.started/updated/completed`, and
  the item types `agent_message`, `reasoning`, `command_execution`, `file_change`,
  `todo_list` and `error`, are accepted. Anything else (an MCP, web search or
  collaboration item, an unknown event, or a non-string type) kills the child with
  `codex_tool_not_allowed:<type>` and no result is accepted. Codex structured output needs every key
  required, so the bridge derives a strict variant of the installed result schema
  (optional keys become nullable) for each dispatch and removes those optional nulls
  from the reply before the result is validated. A reroute or a different
  model marks the result unreviewed with an unknown verdict. Codex reads are not contained by the
  sandbox, so `read_containment` is `advisory`.
- A verification task may set `verification_for_outcome` to a worker's
  `host_outcome_digest` (the SHA-256 of the recorded outcome, as
  `learning-record-host-result` prints it; operational QA takes the same value, and
  routing evidence wraps it as `outcome:<digest>`). It needs `verification_for_family`
  and `verification_for_provider`. `learning-route`, `learning-open-host-run` and
  `dispatch` refuse it unless the outcome is a host outcome of this project from a
  non-verification run of that family, admitted for that provider, in an active
  orchestration, and, at open and dispatch, the verifier's own orchestration. The
  worker result is read from the ledger and checked against the keyed result digest
  the outcome cites; a primary never supplies it. For a bridged verifier, the
  assignment's `context` names the digest (that entry is not exported as a path),
  and the bridge writes the result read-only to `.orchestration-input/worker-result.json`
  after the export and tells the child to review it. `.orchestration-input` is
  reserved: an assignment `context`, `owned_paths` or filesystem permission whose
  first component folds to it, in any letter case, refuses, and the directory must
  not already exist in the export. A native verifier gets the result as
  `verification_input` from `learning-open-host-run`.
- Operational QA accepts the routed verifier of either provider
  (`codex_agents_verification_worker` or `claude_agents_verification_worker`). The
  verifier's role and model must be those of the provider its run was admitted for,
  and a Claude verifier must be the other provider than the worker; a Codex verifier
  of Codex work stays valid for a Codex-only project. The verifier must have been opened
  with `verification_for_outcome` naming QA's worker outcome: the open stores that
  binding with the run (a `verification_binding` lifecycle record) and refuses an
  assignment whose `context` is not exactly `[<that digest>]`.
- A result whose route requires verification must fit the 1 MiB bound as canonical JSON,
  which escapes non-ASCII text; a larger one is refused before the run is consumed, so
  the run can still record a result that fits. A result is never recorded without its
  kept file.
- Bridged runs use the `bridge-v1` ledger client. Records hold only digests, counts and
  typed codes, never prompts or file contents. A host result whose route requires
  verification is kept, as its validated JSON, in an owner-only file under the
  learning state's `artifacts/` directory (at most 1 MiB), registered under the keyed
  result digest; it expires and is deleted with its outcome and with the project. A
  bridged run's recorded `provider_usage` becomes its outcome usage
  (`client_observed`): Claude cache reads and writes are added to the input total,
  and counts the client did not report stay unknown.

