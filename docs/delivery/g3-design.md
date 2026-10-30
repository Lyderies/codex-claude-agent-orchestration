# G3 design: write-capable children

- **Status:** revision 3, the primary's decision after two cross-provider review rounds
  (GPT-6.1 Sol, high).
  - Round 1 returned REVISE with sixteen findings. Round 2 found ten resolved and six partly
    resolved, and added one blocker: event monitoring is not pre-dispatch denial. This revision
    closes the six partial findings and the blocker.
  - The review cap is reached. The S3 whole-release security review checks the result.
- **Goal:** the G3 exit in [the plan](plan.md). Contained write children, verified by the other
  provider, are integrated by the primary.
- **Evidence:** the G2 dispatch spikes and the G3 containment spikes on `staging`.
- **Platform:** write children run on macOS only, the platform where the containment was proven.
  Elsewhere every write route is denied.

## Principles carried over from G2

- Routes stay a pure function of the task contract, the installed policy and committed project
  configuration. Admission stays bound to the run.
- A child's grant only narrows its parent's. Model, tool and MCP output never grants authority.
- A write child starts from committed bytes. Its changes reach the repository only through the
  primary, after verification, as one recorded patch.

## Every write child goes through the bridge

A native write child (a Claude primary's Agent tool, or a Codex primary's native subagent) writes
directly into the primary's checkout. A post-run check can neither undo transient changes nor
separate the child's edits from the primary's. So:

- Every write route has `dispatch: bridge`, including Claude-to-Claude and Codex-to-Codex.
- The plan's dispatch table changes accordingly: Codex to Codex writes go through the bridge.
- Native children stay read-only.

## Effective write authority

- The writable set is the assignment's `owned_paths` intersected with its `:write` filesystem
  permissions. Both are already subsets of the parent's, and the intersection is matched path
  component by component.
- The child's tools come from its granted capabilities. An assignment whose grants do not fit the
  fixed write profile is refused instead of widened.
- The same effective set drives the runtime profile and the post-run ownership check.

## The contained workspace

- **One source commit.** The bridge resolves a single commit and exports every path from it. It
  records the commit, plus a manifest of each exported path with its blob and mode. Executable
  modes are preserved.
- **Read scope.** The workspace holds the committed bytes of granted read and write paths only.
  Paths are literal, Claude context files are excluded, and grants are matched component by
  component, as in G2.
- **Baseline outside the child's reach.** The baseline manifest and the patch are kept in a control
  directory the child cannot read or write. The workspace has no `.git` of its own. The bridge
  never runs project Git hooks, configuration, filters or attributes.
- **Protected names.** The child may never create or change these, at any depth or letter case,
  whatever its grants:
  - `.git`, `.codex/`, `.agents/`, `.claude/`;
  - `AGENTS.md`, `CLAUDE.md`, `CLAUDE.local.md`;
  - `.mcp.json`;
  - every other Claude context name G2 already excludes.
- **Snapshot before inspection.** After the child exits or is stopped, the bridge kills its
  process group, then clones the workspace into the control directory: an APFS clone, each entry
  copied with no-follow semantics. Everything that follows (the inventory, the ownership check,
  the patch and the envelope) uses only the snapshot. A descendant that survived and keeps
  writing to the old workspace cannot change what is checked or integrated.
- **Surviving descendants.** A survivor stays inside the same Seatbelt profile, so it remains
  confined.
  - The bridge sends a second kill to any process that still has a working directory or open
    file under the workspace, found with a bounded `lsof` check. It records whether any survived.
  - The startup sweep repeats this for every leftover workspace before removing it.
  - Lifetime is best effort. Integrity comes from the snapshot.
- **Ownership check.** The bridge compares a bounded, no-follow inventory of every entry in the
  workspace, ignoring nothing, against the baseline manifest. The run fails, and nothing is
  offered, on any of these:
  - a change outside the effective write set;
  - a protected name;
  - a symlink, a hard link (link count above one), a device, a FIFO or a socket;
  - a mode change other than the executable bit on an owned file;
  - more than the caps: 1 MiB per file, 200 changed files, 4 MiB of patch.
- **The patch.** On success the bridge produces one binary-safe patch from the inventory. It is
  not produced from Git. The patch, its digest, the source commit and the manifest digest are
  recorded with the run.
- **Cleanup.** The workspace and the control directory are removed on every terminal state.
  A startup sweep removes leftovers from crashed runs. It runs only under the ledger lock, and
  only for runs the ledger marks as terminal or fenced. A nonterminal run is fenced once its host
  run handle expires, which uses the existing handle lifetime, and only then becomes eligible.

## Claude write profile

G2's locked profile is extended as follows:

- tools `Read,Glob,Grep,Edit,Write`, narrowed by the grants;
- one `Edit(<path>)` and one `Write(<path>)` allow rule per effective write path, with `/**` added
  for directories, and no other allow rules;
- the init check additionally requires exactly the granted tools.

The spikes showed these rules anchor to the working directory and resolve symlinks. Five escape
attempts were all denied. Claude write children have no shell in G3, because verification runs
the project's checks in the integration worktree. A sandboxed shell is a later, optional slice.

## Codex write profile

Codex's own sandbox cannot run inside a restrictive outer profile, and without its shell Codex
cannot read files. So a Codex write child runs:

- `codex exec -s danger-full-access --ephemeral`, with S4b's feature lockdown and event allowlist
  (the allowlist adds `file_change`);
- **Pre-dispatch denial.** For the reviewed client range:
  - the S4b feature disables remove the account connectors, web search, image generation and
    goals from the tool list, so they cannot be called;
  - `spawn_agent` and its siblings fail before any sub-agent run, because `--ephemeral` keeps no
    rollout (live spike on 0.164.0);
  - the event allowlist is detection only.
  - A live check of both behaviours is part of every bump of the reviewed client range. If a
    client in range ever lets a spawn start, Codex bridge routes are refused, read-only and write
    alike, until the denial is restored;
- inside `sandbox-exec -f <generated profile>`. The profile is an **allowlist**:
  - **reads:** the workspace, system paths, and the client's runtime and per-run state;
  - **writes:** the effective write set, the per-run state and a private temporary directory;
  - nothing else, including other workspaces and repositories outside the home directory.
- **Per-run client state.** `CODEX_HOME` points to a private per-run directory, so the child cannot
  change shared configuration, sessions or orchestration state.

**Credentials and network (gates S2).**

- *Transport versus tool network.* The child contract requires network off, but Codex needs
  network for the model API, and Seatbelt cannot tell the two apart. S2 starts with a spike of
  Codex's own `network_proxy` feature and of a local egress proxy. The gate is separation, not
  destination filtering: the client's admitted model transport works while the shell has no
  network at all. Allowing the shell to reach the API hosts is not enough, because it could make
  unadmitted model requests with the readable credentials. S2 does not ship until that
  separation is demonstrated.
- *Credential handling.* Two questions for the owner:
  - Does a per-run `CODEX_HOME` with a copy of the login survive the client's token refresh,
    without invalidating the owner's main login when tokens rotate? This is an account action:
    it needs owner approval and a spike.
  - Does `secret_auth_storage` (keychain) keep the token out of the child's readable tree? Also
    an owner decision, because it changes how the owner's Codex login is stored.
- *Codex primary to bridged write child.* The bridge's `sandbox-exec` cannot start inside a Codex
  primary's own sandbox, so this needs the owner's sandbox decision (an escalation for the bridge
  command). Until then, write children are launched from a Claude primary only.

## Verification

- Every offered patch requires an admitted verifier from the other provider, with a completed
  result and a positive semantic verdict. A missing, same-provider, failed, blocked or negative
  verification blocks integration. Verification is not optional for write routes.
- **Artifact envelope.** The verifier receives a bridge-owned envelope through an explicit input,
  separate from repository path context. The envelope holds:
  - the patch and its digest;
  - the source commit and the manifest;
  - the worker run binding;
  - the resulting contents of each changed file.
- Operational QA becomes provider-aware: it accepts either provider's routed verifier role. Its
  receipts bind the patch digest and the tested tree.

## Integration

Integration is one serialized operation under the ledger lock:

1. **Refuse when the base moved.** The patch is refused if any of these holds:
   - the primary branch is not at the patch's source commit;
   - the primary's index has staged changes;
   - any tracked file differs from the source commit.

   The task is then re-planned with fresh verification; there is no automatic merge in G3.
2. **Apply in a disposable worktree.** The bridge creates a temporary worktree at the source
   commit, applies the patch there, and runs the project's operational checks there.
3. **Commit, then fast-forward.** On success it commits there. Immediately before promotion it
   re-checks the base and the clean primary checkout, then fast-forwards the primary branch.
4. **Leave nothing behind.** On failure or cancellation it deletes the temporary worktree. The
   primary's checkout is never left with provisional changes.
5. **Record.** The run records the commit and the tested-tree digest.

## Routing changes

- Dispatch becomes capability-aware: a write route is always `bridge`.
- The router stops excluding writable bridged Codex routes and Claude agents with write tools.
  Claude agent discovery maps `write-tools` to the contract's `workspace-write`.
- Admission binds to the exact resulting route, as in G2.

## Slices

- **S1: workspace, ownership check and integration, with Claude write children.**
  - Single-commit export with modes and manifest; control directory; protected names.
  - Quiescence check; no-follow inventory; patch and envelope.
  - The Claude write profile and capability-aware routing.
  - Mandatory other-provider verification and provider-aware QA.
  - Integration in a disposable worktree with fast-forward only.
  - Cleanup and the startup sweep.
- **S2: Codex write children.** Gated on the egress spike and the owner's credential decisions.
  - The allowlist profile generator.
  - Per-run `CODEX_HOME`.
  - `codex exec` without its own sandbox, behind S4b's lockdown.
- **S3: security review and exit.**
  - A whole-release review by two vendors. G2 showed this catches cross-slice gaps that slice
    reviews miss.
  - Live tests through a Claude primary, plus a Codex primary if the sandbox decision allows
    it. They cover:
    - writes outside the workspace;
    - aliases for the primary repository;
    - shared client state;
    - narrowing of grants and tools;
    - detached descendants;
    - cancellation;
    - patch substitution;
    - base drift.
  - One coding sub-task per provider, verified by the other provider and integrated.
  - Toolchain v2.

**Estimate.** 4–6 working sessions, up from the plan's 2–3, because of the integration and
verification envelope work. The plan is updated when this design is accepted.

## Owner decisions

1. **Bridge every write child.** Recommended: yes, and the dispatch table changes.
2. **Codex credentials.** Approve a spike of a per-run `CODEX_HOME` with a login copy, or of
   keychain storage. Both touch the owner's login.
3. **Codex sandbox escalation** for the bridge command. This is already pending from G2.
