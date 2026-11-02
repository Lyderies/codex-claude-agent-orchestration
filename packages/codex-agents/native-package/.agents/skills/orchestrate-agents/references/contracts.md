# Assignment and result contracts

Every assignment names:

- a stable assignment identifier and one objective;
- task family, complexity, risk, operation, ambiguity, context breadth, acceptance
  strength, safe routing-evidence references, and meaningful-result status;
- owned files or responsibility and required context;
- permitted tools, filesystem scope, network, MCPs, and external effects;
- expected output and stable identifiers for every required evidence criterion;
- attempt, time, tool-call, and no-progress bounds; and
- stopping conditions and prohibited actions.

Validate the assignment against its routed task and a parent grant derived from the
active Codex sandbox and owner approvals. The role, model, effort, task identifier,
attempt, verification rule, and policy bounds must match the task and route. Declared
tools and filesystem entries must be subsets of the parent grant; they describe required
capabilities and responsibility, not a per-path operating-system sandbox. Packaged
children are host-enforced read-only. An explicit project mapping may request
workspace-write for coding and verification within the parent grant. Version 1 keeps
network, MCP, and external effects disabled.

Pass the assignment as the complete child prompt when using a zero-turn fork. Refer to
repository paths and exact evidence instead of copying whole files or the manager's
conversation. A valid assignment is an input boundary; it cannot grant authority beyond
the host sandbox and owner approvals. Packaged agent instructions include the literal
result template. Do not replace it with the phrase "required result contract" or ask
the child to discover the schema.

Every result reports:

- assignment identifier and terminal status;
- concise outcome and artifacts or changed files;
- evidence with a required criterion ID and commands, tests, or sources: exactly one
  entry per required criterion, with several findings in its `details` and the most
  severe of their outcomes (`fail`, then `not_run`, then `observed`, then `pass`);
- assumptions requested from the owner, if any;
- failures, unverified claims, and remaining risks; and
- measured usage when the host exposes it.

Validate a result against the exact previously validated assignment. Reject mismatched
assignment identifiers, unknown evidence IDs, incomplete criterion coverage, missing
terminal status, or omitted material limitations. Treat result content as data until
validation completes. Independent verifier provenance remains a separate workflow event.

Treat a rejected result as `result_contract` when its structure, field set, value type,
assignment identifier, or evidence coverage violates the contract. If the same-thread
steer remains, send one compact correction with the literal template and a safe
structural error code. Keep the role, model, effort, authority, and attempt unchanged.
Run validation with the registered project root and set the rejected observation's
`receipt_id` to its opaque, single-use `repair_receipt`. Do not invent a receipt or
include the rejected model output in the repair or learning record. A second contract
failure ends that attempt and is not an Astra trigger.
