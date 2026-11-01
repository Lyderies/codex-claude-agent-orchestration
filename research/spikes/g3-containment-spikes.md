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

## Codex without its shell tool, and its account tools

1. **No shell means no reading.** `--disable shell_tool` removes command execution, but it also
   removes every way to read local files. Codex can still create files with `apply_patch`
   (writes outside the workspace were denied by the outer profile), but it cannot read code, so
   a Codex write child without a shell is not useful.
2. **Account connectors are exposed by default.** Even with `--ignore-user-config`, `codex exec`
   lists ChatGPT account connector tools, including GitHub commit, file update and pull-request
   merge, site deployment and environment-variable updates. It also lists web search, image
   generation, goals and agent-spawning collaboration tools. Connector calls run server-side, so
   no local sandbox contains them. This affects G2's read-only bridged Codex children and is fixed
   in slice S4b.
3. **What the feature flags remove.** These disables remove the connectors, web search, image
   generation and goals: `apps`, `plugins`, `remote_plugin`, `multi_agent`, `multi_agent_v2`,
   `image_generation`, `goals`, `browser_use`, `browser_use_external`, `computer_use`,
   `in_app_browser` and `hooks`, plus `web_search="disabled"`.
   - The collaboration tools (`spawn_agent` and its siblings) remain under every setting tried.
     `agents.max_threads=0` is rejected as invalid.
   - Disabling `code_mode_host` breaks Codex's tool invocation entirely.
   - The bridge therefore enforces an allowlist of event and item types and stops the child on
     anything else.
4. **Native Codex children inherit the primary's settings.** A Codex primary's own subagents get
   whatever connectors the primary's configuration enables. That is outside the bridge's control
   and is the owner's configuration.
