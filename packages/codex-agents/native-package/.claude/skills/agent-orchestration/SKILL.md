---
name: agent-orchestration
description: Route and supervise subagents from a Claude Code primary through the installed orchestration lifecycle, spawning native read-only Claude children, bridging read-only Codex children, or bridging contained Claude write children that return a patch; keep simple sequential work in one agent.
---

# Agent orchestration (Claude primary)

Keep the primary agent as the decision owner and final synthesizer. Run each command
below as `python3 .codex/orchestration/cli.py <command> --project-root <exact-root>`
from the project root (`validate` takes `--root <exact-root>` instead). The CLI detects the Claude host from Claude Code's own
environment and refuses `host_client_unknown` if it cannot; never set or unset host
markers, never set `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`, and never pass `--bare`.

1. **Readiness.** Run `learning-status` and `validate --root <exact-root>`; continue only
   when both `package_ready` and `project_ready` are true. Run
   `learning-open-orchestration` once per objective and keep its handle.
2. **Route.** Write the task contract and run `learning-route --task <file>`. Model,
   effort, agent and sandbox come only from the route; never choose or substitute them.
   Use one agent for simple or sequential work; delegate only independent, specialist
   or verification work.
3. **Assign.** Write one bounded assignment with stable evidence IDs, then run
   `validate-assignment` and `result-preflight` with the task and parent grant. Give the
   child the returned template.
4. **Observe and open.** Run `provider-observe --provider <route provider>`; before any
   Claude child run `provider-observe --provider claude --billing-probe` instead,
   because Claude admission needs billing from the last 30 minutes; it reuses recent
   evidence and is refused, like a run, when admission would fail. Then run
   `learning-open-host-run` with the task, route, assignment, parent grant and
   orchestration handle. A denial returns a `reason_code`, for example
   `claude_backend_not_subscription` or `claude_model_override_present`: report it to
   the owner and do not retry around it. Every lifecycle step must run from the host
   that opened the orchestration (`host_client_mismatch` otherwise).
5. **Run the child.**
   - `dispatch: native` (a Claude child): spawn with the Agent tool using the open
     result's `native_spawn` verbatim: `subagent_type`, `model` = `model_alias` and
     `effort`. Never map or change these yourself. Then run
     `learning-bind-host-agent --run-handle <handle> --agent-id <returned agent ID>`.
     Native children run with this session's permissions, so their reads are
     `read_containment: advisory`; state that in any report.
   - `dispatch: bridge` (a Codex child, or any write child): do not spawn. Run
     `dispatch` once with the task, route, assignment, parent grant and the exact
     `--run-handle`, and use its `result`. Never dispatch a handle twice or retry a
     fenced outcome.
   - A route with `sandbox_mode: workspace-write` is always `dispatch: bridge`, even to a
     Claude child; never give a native child write tools. Its assignment must own the
     paths it may change and grant them as `<path>:write`, with exactly the tools
     `Read`, `Glob`, `Grep`, `Edit` and `Write`. The child edits a contained copy of the
     committed files, never this checkout. A successful `bridge_report` has
     `ownership: passed`, `changes`, `patch_digest` and `envelope_digest`; the patch and
     verifier envelope are stored by the ledger. Do not apply the patch yourself: the
     integration step is not available yet, and it will require
     `write_verification_state` to be `accepted` (step 7). Any other `ownership` value
     is a failed run with no patch. Write routes run only on macOS and only to Claude children; a
     denial says `write_children_unsupported_platform` or
     `codex_write_children_unavailable`.
6. **Validate and record.** Run `validate-result` on the child's result, allowing at
   most one structural repair with the returned receipt. Always finish with
   `learning-record-host-result`, even for a failed or timed-out child. For a bridged
   run, pass `bridge_report.terminal_state`, when set, as the observation's
   `terminal_state`, and treat `reviewed: false` as unverified.
7. **Verify.** Send every meaningful result to the route's verifier: create a
   verification task with `verification_for_family` and `verification_for_provider` set
   to the worker's family and provider, route it, and run it through steps 3 to 6.
   - A write result (`ownership: passed`) must be verified by the other provider: a
     Claude write child is verified by a read-only Codex child through the bridge. Add
     `verification_for_write` with the worker's `bridge_report.bridge_run_id` and
     `patch_digest` to that verification task. Routing it anywhere else, or with no
     eligible Codex verifier, is denied `write_verifier_unavailable`; opening it for a
     failed, unrecorded or different patch is refused.
   - Do not name the envelope in the verifier's assignment: `dispatch` places it at the
     reserved path `.orchestration-input/envelope.json`, which no assignment may name,
     and tells the verifier to review the patch there and state it as one `artifacts`
     entry `reviewed_patch_digest:<patch_digest>`. Do not put the digest in the
     objective.
   - `dispatch` records the verifier's own verdict (`bridge_report.write_verification`)
     only when it states exactly the bound digest, and the recorded host result must be
     that same result. Check `learning-write-verification-state --bridge-run-id <worker
     bridge run>`: only `accepted` (a completed, positive, other-provider verdict on the
     current patch and none negative or blocked) lets integration proceed in a later
     release; `missing`, `rejected` and `blocked` do not.
8. **Operational QA.** Run `learning-run-operational-checks` with both host outcome
   digests and pass its `check_receipt` to `learning-record-operational-qa`. The routed
   verifier of either provider is accepted. For a write result the receipt's
   `write_binding` names its `patch_digest`; its `tested_tree_digest` stays null until
   integration runs the checks on the patched tree.
9. **Close.** Report failed, blocked and unverified work plainly, then run
   `learning-close-orchestration` with the real terminal state. Claude Code has no
   native goal counters, so omit `--goal-tokens-used` and `--goal-time-used-seconds`
   rather than estimating them.

The Codex skill `.agents/skills/orchestrate-agents/SKILL.md` and its `references/`
explain routing, contracts and supervision in more depth.
