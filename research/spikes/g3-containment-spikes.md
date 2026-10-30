# G3 containment spikes

- **Date:** 2026-10-31.
- **Clients:** codex-cli 0.164.0 and Claude Code 2.1.310, on macOS.
- **Purpose:** decide how write-capable children are contained.

## Codex: an outer Seatbelt profile

1. **Nested sandboxes fail.** Codex runs its shell commands under its own Seatbelt profile. Inside
   any outer profile that restricts something, Codex's own `sandbox-exec` fails with
   `sandbox_apply: Operation not permitted`, so every shell command fails. Nesting only works when
   the outer profile is `(allow default)` with no restrictions, which contains nothing.
2. **Outer profile with Codex's sandbox off works.** `sandbox-exec -f <profile> codex exec -s
   danger-full-access`, with a profile denying reads and writes outside the child directory:
   - reading inside the child directory worked;
   - reading outside it was denied by the outer profile;
   - writing outside it was denied;
   - writing inside it worked.
3. **Open issue: credentials.** The profile covers the whole process tree, so the child's shell
   can read anything Codex itself must read, including its own login file. Network has to stay
   open for the model API, and Seatbelt cannot filter outbound traffic by hostname. Native Codex
   children can already read that file today, so this is no regression, but G3 should close it.
   Candidates:
   - a local egress proxy that allows only the API hosts;
   - moving credentials out of the readable tree, if the client supports it;
   - accepting it as a documented limit.
4. **Gotcha.** `codex exec` waits for more input when stdin is an open pipe. Always pass
   `< /dev/null`.

## Claude: path-scoped write rules

Settings: `Edit(src/**)` and `Write(src/**)` as the only allow rules, `--permission-mode dontAsk`,
`--setting-sources project`, and the tools limited to `Read,Edit,Write`.

| Attempt | Result |
|---|---|
| Write or edit a file under `src/` | allowed |
| Write under `docs/` (not granted) | denied |
| Write to an absolute path outside the working directory | denied |
| Write to `../escape.txt` | denied |
| Write to a directory named `src` elsewhere on disk | denied |
| Write through a symlink inside `src/` that points outside | denied |

The rules are anchored to the working directory and resolve symlinks. A Claude child with these
rules cannot write outside its owned paths through its file tools. Its shell tool is not covered
here: the planned sandboxed-shell sub-slice handles it, with network off.
