---
name: verify-result
description: Independently verify a meaningful research, planning, coding, security, or release result against named acceptance criteria and reproducible evidence.
---

# Verify result

Start from the assignment and acceptance criteria, not the implementer's confidence.
Inspect the exact artifact revision. Run applicable existing checks in the stated
environment and record commands, versions, output, and skipped checks. For research,
open the cited primary sources and test the material claims. For plans, trace steps to
accepted requirements and expose hidden decisions. For code, test observable behavior
and relevant boundaries. Do not repair failures in the verification assignment.

Return `pass`, `fail`, `blocked`, or `not_run` for each criterion. Overall readiness is
`pass` only when every required criterion passes. Use [evidence.md](references/evidence.md)
for the record shape.
