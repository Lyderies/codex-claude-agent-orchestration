---
name: orchestrate-agents
description: Route and supervise read-only Codex and Claude children (bridged, through the orchestrator CLI) for multi-step research, planning, security, or verification work when delegation may improve quality or time; keep simple sequential work in one agent.
---

# Orchestrate agents

Keep the primary agent as the decision owner and final synthesizer.

Codex's sandbox blocks the learning collector, so lifecycle commands run outside it only
through the owner's Codex command rule. Every Codex session on this machine loads that
rule, and it runs a protected copy of the orchestrator in `~/.codex` that a session
opened on a project cannot change. It is safe only while every Codex session is opened on
a project itself: never on a folder that contains a set-up project, the orchestrator's
setup folder, the home folder, its parent or `/` (nor adds `~` or `~/.codex` with
`--add-dir`): if this session's workspace is one of those, stop and tell the owner. Before any other
CLI call, run `python3 -I -B -S -X pycache_prefix=/dev/null .codex/orchestration/cli.py
host-command --project-root <exact-root>`. If its `codex_rule` is not `installed`, stop
and tell the owner to run `./setup.sh` (Codex commands step), with its `reason` when it
says `unsupported`; do not retry or work around it. If its `session_guard` is not `ok`,
stop and report that reason to the owner word for word: the orchestrator refuses every
command from a session that can write the home folder, a folder above it, `~/.codex`, the
orchestrator's setup folder, a tool folder, the learning state, the orchestrator's private
working folders, a folder that contains this project, or this project's `.codex`,
`.agents` or `.git` as a folder of its own, or that runs without its sandbox or with
`--ephemeral`, and only reopening Codex on the project folder fixes it. Otherwise run every later CLI command
as exactly its `argv_prefix` (the absolute base interpreter, the isolation flags
`-I -B -S -X pycache_prefix=/dev/null`, then the absolute `cli.py` of the protected copy
in `~/.codex`, never the project's own `.codex/orchestration/cli.py`) followed by the
subcommand and its arguments: one command per call, with no shell compounds (`&&`, `;`,
pipes, redirections or substitutions) and no environment-variable prefixes. Any other form
stays sandboxed and fails. Below, every command, named alone or as `cli.py <command>`,
runs with that prefix. A failure whose `hint` names the Codex sandbox means the prefix was
not used exactly; one saying the installed runtime differs from its protected copy means
the owner must run `./setup.sh` again. Lifecycle commands belong to the primary. Every
child of a Codex primary is bridged: `dispatch` starts it outside this session, read-only,
in a private export, and a Codex child loads no rules (`--ignore-rules`), so no child can
use the command rule. Never spawn a native Codex child with your own collaboration tools:
it would share this session and its rule. Use `dispatch` for every child; if a route ever
says `dispatch: native`, stop and report it.

1. Read the applicable project instructions and accepted decisions. Do not fill a
   material open choice with an assumption.
2. Before a meaningful route, run `learning-status` for the exact project root. The
   command recovers a stopped collector for a registered project. If registration is
   missing, report it to the owner: `learning-register` is an owner command that
   `./setup.sh` runs, and it stays sandboxed from a Codex session. Do not dispatch a child
   while learning is unavailable, and do not describe the failure as disabled learning.
   Also run `cli.py validate --root <exact-root>`. Do not begin managed orchestration
   unless both `package_ready` and `project_ready` are true. A package-only pass does not
   prove routing or QA readiness.
   At the start of a multi-step objective, run `learning-open-orchestration` once and
   retain its opaque handle until the objective ends. Every managed child must be linked
   to this boundary. A missing boundary is a learning failure, not an optional omission.
3. Classify the task using the installed routing policy. State the operation,
   ambiguity, context breadth, acceptance strength, and safe routing-evidence
   references. Risk never selects model, effort, or topology or widens authority. High
   and critical tasks must remain meaningful and independently verified. Pass
   the task contract to
   `cli.py learning-route --project-root <exact-root>`; do not choose
   a more powerful or cheaper model merely from its name. Run the CLI and consume its
   JSON; do not inspect runtime source or schemas during ordinary routing. The route's
   `role` or explicitly mapped `agent_type`, model and effort are what `dispatch` runs;
   never choose or edit them. Discovery without an explicit project mapping never
   authorizes an agent. The legacy GPT-5.6 Sol fallback (`--fallback-from-run-handle`)
   applies only to a native child the host rejected, so it never applies to a Codex
   primary: a bridged child whose model is unavailable ends fenced and is not retried.
   Never substitute Terra or edit the returned route.
   If the returned stage is `reconnaissance`, run only the returned read-only
   researcher route under its normal managed lifecycle. Its assignment may inspect
   enough repository evidence to replace `unknown` ambiguity or context breadth; it
   must not implement the objective. Independently verify that result, then create a
   new task contract with the same task identifier, the observed task-shape values,
   and the QA-accepted host outcome as `outcome:<host_outcome_digest>`, then route again. The ledger
   must resolve it to that project, task, family, and reconnaissance run. Do not execute
   the objective from a reconnaissance route.
4. Use one agent when the task is simple, sequential, or already fits the primary
   context. Delegate only independent work, specialist work, context isolation, or
   independent verification.
5. Give each subagent one bounded assignment using the assignment contract. Include
   ownership, permitted tools and paths, expected evidence, stopping conditions, and
   prohibited actions. Give every required evidence criterion a stable ID. Record the
   maximum child authority from the active parent sandbox and owner approvals; never
   invent it. Validate the assignment with its routed task and parent grant using
   `cli.py validate-assignment --project-root <exact-root>` before dispatching; like
   `result-preflight`, it validates against the project's effective policy, so a
   cross-provider route validates only with `--project-root`. Every child of a Codex
   primary is read-only: a route that needs `workspace-write` is denied until write
   containment exists. Send every completed change to independent verification.
6. Before each open, run `cli.py provider-observe --project-root <exact-root>
   --provider <route provider>`; for a Claude route add `--billing-probe`, because Claude
   admission needs billing from the last 30 minutes and the probe reuses recent evidence. Then call `learning-open-host-run` with the exact task,
   route, assignment, parent grant, project root, and orchestration handle. Do not
   dispatch if this command fails. It runs provider admission; a denial returns a
   `reason_code`. Report it to the owner instead of retrying, and never set a provider
   API key to get past it. Each Codex billing record that `provider-observe` prints
   carries `paid_usage_waived_by_attestation`, true when the owner's attestation makes
   admission treat that credits reading as not usable; it is display only. Every route
   is `dispatch: bridge`: do not spawn. Run `cli.py dispatch` once with the same task,
   route, assignment and parent grant and the exact `--run-handle`, then continue at
   step 8 with its `result`. `dispatch` builds the child's prompt (the objective, the
   result template and its rules) and binds its own child to the run, so there is
   nothing to bind. A refusal before launch (`launch denied: <reason_code>`) writes no
   dispatch; for a Claude route refused `billing_unverified` (billing over 30 minutes
   old), run the billing probe and dispatch the same handle again. A `dispatch` that
   fails after launch leaves no result to record: pause the orchestration, classify that
   run abandoned (`host_confirmed_gone`), and resume. Put everything the
   child needs in the assignment's objective, context and owned paths: it sees only
   the exported files, never this conversation.
7. Track observable state: active, waiting on a named dependency, paused, blocked,
   completed, failed, timed out, interrupted, stale, or cancelled. A progress message is not proof of
   correctness. Steer only with new task-relevant information; interrupt on scope drift,
   repeated no-progress work, or exhausted bounds.
8. `result-preflight` with the exact assignment, task, and parent grant shows the
   literal template and `consistency_rules` that `dispatch` gives the child; it is
   non-mutating. Only an authoritative host terminal fact may report `timed_out`: for a
   bridged run that is `bridge_report.terminal_state`; never copy a child-authored
   timeout claim into `terminal_state`.
   `dispatch` validates the child's result itself: a result that does not validate, or
   that is too large to keep for its verifier, comes back fenced as a failure
   (`bridge_report.outcome` names why). The ledger binds a keyed digest of the returned
   `result` and its terminal state to the run, so record exactly that result, unedited,
   with `bridge_report.terminal_state` (when set) as the observation's `terminal_state`;
   any other result, or a dropped or added terminal state, is refused. There is no
   repair for a bridged result. Run `cli.py validate-result --project-root <exact-root>`
   on it if you want the check, but never change it.
   Never classify a result-contract failure as semantic or reasoning failure.
   Keep execution state, semantic verdict, and admission separate. A completed review
   may validly return a negative verdict. Always finish the opened host run with
   `learning-record-host-result`, including for a failed, timed-out, interrupted,
   cancelled, or blocked child. Submit `result_attempts` as one accepted structural
   attempt. Never invent a receipt or run binding.
   Keep the `host_outcome_digest` that `learning-record-host-result` prints; it is the
   only outcome identifier that verification and operational QA accept (never the
   `out:` record ID).
   Send every meaningful result to the independent verifier from the route. Create a
   verification task whose `verification_for_family` and `verification_for_provider`
   name the original task family and provider and whose `verification_for_outcome` is
   the worker's `host_outcome_digest`; route it normally, and preserve its returned
   agent, model, effort, and sandbox. Give the verifier assignment the worker's
   `required_evidence`, `context: ["<worker host_outcome_digest>"]`, and the source
   files to check in `owned_paths` (listed in the task's `owned_paths` too, with
   `<path>:read` permissions). The context must be exactly that one digest, or the
   open is refused, and operational QA accepts only a verifier opened with
   `verification_for_outcome` for that worker. The worker's result reaches the verifier
   only from the ledger: the bridged verifier finds it read-only at
   `.orchestration-input/worker-result.json` in its export. The ledger keeps that result
   only while the orchestration is open (and at most seven days), so verify before
   closing. The router refuses an outcome that is unknown, from another provider or
   family, or from another orchestration. Open, dispatch, and finish the verifier
   through the same host lifecycle.
   Then run `learning-run-operational-checks --project-root <exact-root>
   --host-outcome-digest <worker digest> --verifier-host-outcome-digest <verifier
   digest>`. It may run only the exact commands in the committed, clean project file
   `.codex/orchestration/operational-checks.json`; it uses no shell, inherited secrets,
   prompt content, or model-selected command. Then run
   `learning-record-operational-qa --project-root <exact-root> --host-outcome-digest
   <worker digest> --verifier-host-outcome-digest <verifier digest> --check-receipt
   <check_receipt> --evaluator-assignment <worker assignment file> --evaluator-result
   <worker result file> --verifier-assignment <verifier assignment file>
   --verifier-result <verifier result file>`; every argument is required, and the files
   are the exact assignments and results you validated and recorded. QA passes only when both results
   satisfy the same named criteria without unverified claims, all checks pass, and both
   runs have durable provenance. Trust a passing operational QA result. Raise owner
   attention only for failed or missing checks, verifier disagreement, missing evidence,
   security concerns, or an unavailable lifecycle record. Operational QA cannot approve
   a canary, promotion, release, or authority change.
9. Synthesize only supported findings. Report failed, blocked, and unverified work
   plainly. Record failed output attempts with one typed reason and safe evidence
   references, never raw prompts, output, secrets, or unrestricted tool data. Take the
   provider, model and effort from the route `learning-route` returns; the policy's
   ordered provider candidates per family decide them, not this text. Risk never raises
   model or effort. No claimed failure history causes model escalation. Project-bound
   escalation from verifier history remains unavailable until the router can resolve
   the references to actual independent verifier records. Eligible alternatives are
   evaluation candidates, not permission to substitute them during live work. Each
   route names its `provider` and `dispatch`, which is always `bridge` for a Codex
   primary. For a bridged result, `bridge_report.terminal_state`, when set, is the
   authoritative host terminal fact: pass it as the observation's `terminal_state`.
   Treat `reviewed: false` as an unverified result. Never dispatch the same run handle
   twice or retry a fenced outcome.
   Stop after the configured attempts; do not retry by silently raising model, effort,
   permissions, tools, network, or spend.
10. After each completed worker-verifier QA pair and before an intended pause, read the
    native goal counters when available and pass their exact `tokensUsed` and
    `timeUsedSeconds` values to `learning-checkpoint-orchestration`. Do not checkpoint
    more frequently. Persist the pause with `learning-pause-orchestration` and resume
    with `learning-resume-orchestration`; neither resets deadline or checkpoint history.
    Classify a pending binding abandoned only after pausing and only from a confirmed
    closed reason. At objective completion, read the counters again and pass them to
    `learning-close-orchestration`. Do not estimate them. Close with the real terminal
    state and return the generated statistics. The report counts each host run once,
    reports model, effort, agent, QA, repairs, no-progress events, and durations, and
    keeps any unsupported per-child usage as `null`/unknown.

Do not pre-read every reference. Read [routing.md](references/routing.md) only when the
router output needs explanation, [contracts.md](references/contracts.md) when a
contract fails validation, and [supervision.md](references/supervision.md) when an agent
stalls, fails, or needs steering.
