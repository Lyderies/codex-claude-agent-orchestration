---
name: review-security
description: Independently review a bounded design or change for concrete authority, secret, data-flow, sandbox, network, approval, spend, and MCP defects.
---

# Review security

Map trusted components, untrusted inputs, protected assets, authority grants, and
external effects for the assigned change. Trace concrete failure paths before reporting
a finding. Prioritize loss of authority control, secret exposure, unintended writes or
publication, network escape, approval reuse, budget races, incomplete cancellation,
and cleanup failure. Report exact evidence, severity, impact, and smallest correction.
Do not edit the artifact in the same assignment.

Read [mcp.md](references/mcp.md) when MCP servers or tools are in scope.
