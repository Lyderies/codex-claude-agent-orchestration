# Assignment and result contracts

## Worked example

Copy these forms before writing your first task, assignment and parent grant. They validate
as they stand, once the assignment's four `<...>` values and its `bounds` come from the route
that `learning-route` returned for the task.

The task. Its `family` is one of `inventory`, `research`, `planning`, `coding`, `security` or
`verification`. Leave `routing_evidence_refs` empty unless you hold a QA-accepted
reconnaissance outcome for this task; its only form is `outcome:<host_outcome_digest>`, never a
file path.

```json
{
  "id": "docs-inventory-1",
  "objective": "List every CLI subcommand that the user documentation names.",
  "family": "inventory",
  "complexity": "low",
  "risk": "low",
  "operation": "mechanical",
  "ambiguity": "known",
  "context_breadth": "narrow",
  "acceptance_strength": "deterministic",
  "routing_evidence_refs": [],
  "meaningful": true,
  "deterministic_validation": true,
  "independent_parts": 1,
  "parallel_safe": false,
  "failed_attempts": 0,
  "failure_events": [],
  "owned_paths": ["docs/using-it.md"]
}
```

The assignment. Its `context` holds repository paths and no prose: `dispatch` exports each entry,
so a sentence there is refused, usually with `export_path_not_granted`. Put every instruction in
`objective`. The one exception is a verifier bound to a worker outcome, whose `context` is exactly
`["<worker host_outcome_digest>"]`. Every path in `context` and `owned_paths` needs a `<path>:read`
entry in `permissions.filesystem`, on the path or a folder above it, and `owned_paths` must also be
in the task's.

```json
{
  "assignment_id": "docs-inventory-1-a1",
  "task_id": "docs-inventory-1",
  "objective": "List every CLI subcommand that docs/using-it.md names, one evidence entry per criterion.",
  "role": "<route role>",
  "model": "<route model>",
  "effort": "<route effort>",
  "attempt": 1,
  "meaningful": true,
  "owned_paths": ["docs/using-it.md"],
  "context": ["docs/using-it.md"],
  "permissions": {
    "tools": ["shell"],
    "filesystem": ["docs/using-it.md:read"],
    "network": false,
    "mcps": [],
    "external_effects": false
  },
  "expected_output": "The subcommand names, each with the line that names it.",
  "required_evidence": [
    {"id": "subcommands-listed", "description": "Every subcommand named in the file, with its line."}
  ],
  "verification_required": "<route verification_required>",
  "bounds": {
    "max_attempts": 2,
    "max_steers_per_attempt": 1,
    "max_no_progress_events": 2,
    "wait_seconds": 60,
    "timeout_seconds": 21600,
    "max_tool_calls": 30
  },
  "stopping_conditions": ["The file is read and every subcommand is listed."],
  "prohibited_actions": ["Changing any file."]
}
```

`verification_required` is a boolean: use the route's `true` or `false`, unquoted. No bound may
exceed the route's.

The parent grant, which is the most the child may have:

```json
{
  "tools": ["shell"],
  "filesystem": ["docs/using-it.md:read"],
  "network": false,
  "mcps": [],
  "external_effects": false
}
```

When validation prints `input validation failed` with a `field`, that names where to look, such
as `task.family` or `assignment.bounds.wait_seconds`; the value is never printed. A `field` that
names a whole object (such as `task`, `assignment`, `parent_grant` or `assignment.bounds`) means it
is not an object or has a key the contract does not allow. A `route` field means the route you
passed is not the one `learning-route` returned. No `field` means the failure is not in one of these
contracts: the input may be unreadable or not valid JSON, or the check may depend on this machine
or project, so report it instead of rewriting your input.

## The contracts

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
