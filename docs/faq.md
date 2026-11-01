# FAQ

## Can this charge me beyond my subscriptions?

It is built not to. API keys, usage credits and paid fallback are never eligible, admission fails closed
on an API key in the environment or unknown billing, and no child runs until you have signed a billing
attestation for that account. The attestation is your own statement that extra usage and Codex credit
purchases are off, so re-attest whenever that changes. See the [security model](security-model.md).

## Can a child edit my files?

Not in v1: every child runs read-only, in a private export of the committed files it was granted.
Children that edit files in their own worktrees are planned for G3.

## Does a model decide where work goes?

No. A route comes from the installed policy, the task and your project's committed configuration, in the
[routing table](routing-table.md)'s fixed order. Model output never grants tools, paths or spend, and the
orchestrator never changes its own routing without you: a row changes only when recorded evidence clears
the bar, and the policy and its test change with it.

## Does it work on Windows or Linux?

macOS comes first. Linux, including WSL, installs but is partly untested, and operational QA needs macOS:
it fails closed elsewhere, so verify reports Linux as not ready. Native Windows is not supported.

## Where are my records?

In `~/.local/state/codex-agents` on your machine, shared by every set-up project on it through one local
collector, and never committed. See [configuration](configuration.md#machine-files).

## Why must I open Codex on the project itself?

Codex runs a nested trusted project's configuration and hooks outside its sandbox, so a session opened on
a folder above the project could plant them. See
[where to open Codex](security-model.md#where-to-open-codex).

## Why does a Codex session need the commands rule?

Codex's sandbox blocks the local learning collector, so the orchestrator's everyday commands cannot run
inside it. The rule lets only those 23 commands run outside the sandbox, through a protected copy of the
orchestrator. See [the protected copy](security-model.md#the-protected-copy-and-the-codex-rule).

## Why are Claude children sometimes native and Codex children never?

A Claude primary can spawn Claude children with its own Agent tool. A native Codex child would share the
Codex primary's session and its command rule, so every Codex child goes through the bridge instead. See
[dispatch paths](how-it-works.md#dispatch-paths).

## I lost my owner key passphrase. What now?

Signed attestations stay valid, but you cannot sign new ones yet. See
[troubleshooting](troubleshooting.md#i-lost-my-owner-key-passphrase).

## How do I remove it?

The wizard's last screen shows the remove command for the project. It removes the Codex commands rule
and its protected copy, the permission entries the installer added, then both packages. See
[manual setup](manual-setup.md#remove-everything).
