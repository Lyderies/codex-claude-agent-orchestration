# Supervision

Use host thread state and events as health evidence. Do not infer health from polished
text or correctness from successful completion.

- **Waiting:** require a named dependency or approval.
- **Stale:** no relevant event or artifact change within the assignment's bound.
- **No progress:** repeated actions or restated conclusions without new evidence.
- **Scope drift:** work leaves the assigned objective, files, tools, or authority.

On stale or no-progress work, inspect once, send one focused correction when new
information can help, then interrupt if the bound is still exceeded. Retry only when a
transient failure or corrected input makes success plausible. Each retry is a new
attempt with its own budget and evidence. Never duplicate a write attempt when its
completion is uncertain.

A result-contract, schema, or formatting failure may use the one per-attempt steer as a
compact same-thread repair. Preserve the role, model, effort, permissions, tools, and
attempt; send the literal result template plus a safe structural error code. Do not send
or store the rejected output. If the repaired result is invalid, end the attempt. A
contract repair is not a semantic failure and cannot cause an Astra route.
