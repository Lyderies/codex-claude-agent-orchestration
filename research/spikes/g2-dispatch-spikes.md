# G2 dispatch spikes

Three quick tests run on 31 October 2026 (Claude Code 2.1.310, codex-cli 0.164.0, subscription logins
only, no API keys). They decide how the G2 dispatch bridge is built.

## Claude child containment (read-only)

A headless `claude -p` child was asked to read a file inside its working directory and several targets
outside it.

| Profile | Inside read | Outside read, glob, grep | Symlink inside pointing out | `../` path |
|---|---|---|---|---|
| `permissions.allow: ["Read", "Glob", "Grep"]` | Allowed | **Allowed (leak)** | Not tested | Not tested |
| No allow rules, `defaultMode: dontAsk` | Allowed | Denied | Denied | Denied |

The working profile:

- flags: `--tools Read,Glob,Grep --permission-mode dontAsk --setting-sources project --settings <profile>
  --strict-mcp-config --no-session-persistence --output-format stream-json --verbose`, with stdin closed;
- settings: `disableAllHooks`, `availableModels` pinned to the requested model, every built-in plugin
  disabled through `enabledPlugins`, and no permission allow rules;
- never `--bare`, which switches authentication to an API key.

With this profile, the stream reported `apiKeySource: none` (the subscription login) and the user-level
`CLAUDE.md` was not loaded.

## Usage signals

| Provider | Source | What it reports |
|---|---|---|
| Claude | `rate_limit_event` in each child's stream | `unifiedWindows.five_hour` and `seven_day` utilization (fraction), status and reset time |
| Codex | App-server `account/rateLimits/read` | Used percent, window length and reset time per window; Pro-tier plans showed only a weekly window |

The Codex app-server also exposes methods that spend or change account state. The orchestrator must
allow-list read methods only. Claude utilization moved too little to see on tiny runs, so per-task
cost needs aggregation over many runs.

## Codex primary launching a Claude child

| Codex sandbox setting | Result |
|---|---|
| Workspace-write, network off (default) | `claude` fails: "Not logged in" |
| Workspace-write, network on | Child answers correctly |
| Network off plus an execpolicy `allow` rule for `claude -p` | Still sandboxed; fails |
| Escalation request in `codex exec` | Not possible: approval policy is forced to `never` |

A Claude primary can dispatch to Codex children with no change. A Codex primary needs either network in
its sandbox, or an escalation that an exec-policy rule approves for the bridge command only. The second
path needs testing in an interactive Codex session.
