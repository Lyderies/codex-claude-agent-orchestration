# Using it

Your only command is `./setup.sh`. The orchestrator CLI is for the agent: you ask the primary session for
the skill, and it routes the work, opens each run through admission, starts the child, has the result
verified, runs operational QA and reports back, or stops with a `reason_code`.

## From Claude Code

Open the project and ask for the `agent-orchestration` skill, for example: "Use the agent-orchestration
skill to have the routed reviewer check the error handling in `src/`."

It routes the task, opens the run, spawns a Claude child natively or dispatches a Codex child through
the bridge, verifies the result, runs operational QA and reports.

## From Codex

1. From your own terminal, go to the project's own folder. Never start Codex on a folder above the
   project, on this repository or on your home folder ([why](security-model.md#where-to-open-codex)).
2. Start `codex -p orchestrator` if step 7 of the wizard created the profile (needed for Claude
   children), otherwise `codex`.
3. Ask for the `orchestrate-agents` skill.

The skill first runs `host-command` and stops, with the reason, unless the Codex commands rule is
`installed` and the session guard says `ok`.

Do not start Codex from inside a Claude Code session, for example from Claude Code's terminal tool: it
inherits Claude Code's environment markers, and the host check then refuses with `host_client_unknown`.

## When a task is denied

A denial comes back as a `reason_code`, and no child starts. Admission fails closed, for example on an
API key in the environment, an unreviewed client version, a missing attestation, unknown or paid-usage
billing, exhausted headroom or a run cap ([full list](security-model.md#what-v1-enforces)). Common
messages and their fixes are in [troubleshooting](troubleshooting.md); nearly every fix is "run
`./setup.sh` again".

## Keeping it working

- Run `./setup.sh` again to update the install, verify it, re-run the billing attestation or change the
  owner key passphrase.
- Re-attest whenever your billing settings change.
- Close every Codex session in the project before installing or updating.
- After every install or update (a Python upgrade needs one), trust the orchestrator's learning hooks
  again in Codex's `/hooks`; the wizard tells you when.
