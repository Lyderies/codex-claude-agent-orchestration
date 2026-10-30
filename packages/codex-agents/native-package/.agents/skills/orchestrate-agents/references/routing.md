# Routing

Use the versioned policy at `.codex/orchestration/config/routing-policy.json` through
the installed CLI. The policy is quality-first and produces a candidate route from
observable task fields. Model labels are not evidence that a task will pass.

Use a single-agent route for simple or sequential work. Use parallel workers only when
assignments are independent. Packaged children are read-only. Explicit project
mappings may select workspace-write coding and verification agents within the active
parent grant. Use the primary agent as manager when one owner must preserve requirements
and synthesize results.

The accepted version 4 routing policy is:

- GPT-6 Luna/high for reconnaissance, bounded mechanical or implementation work with
  deterministic or executable acceptance, and bounded deterministic verification;
- GPT-6 Sol at medium or high for exploration, diagnosis, planning, cross-system
  synthesis, architecture, security, and judgment-heavy independent verification;
- Astra/medium only for evidenced end-to-end work that is extreme-complexity,
  high-ambiguity, and cross-system; and
- GPT-5.6 Sol/high as the sole legacy fallback, available only through the trusted,
  finite, single-use receipt lifecycle after a native GPT-6 host rejection.

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

The validated 0.156.1 account catalog recognizes `low`, `medium`, `high`, `xhigh`, and
`max` for active models, while initial routes use medium or high. Native routing
excludes `none` and `ultra`.
