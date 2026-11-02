# Troubleshooting

Your only command is `./setup.sh`, run from the root of this repository. Nearly every fix below is "run
it again". See the [setup wizard](setup-wizard.md) for what each step does.

## "That passphrase did not unlock the key."

`attest.py` asks `Try again or Exit? [T/e]`; press Enter to try again.

## "the owner key must be a file in your own ~/.ssh folder"

Move the key into `~/.ssh` (mode 0600, no symbolic link, the folder writable only by you) and run again.
If it then says the key file has another link elsewhere, make a fresh copy in `~/.ssh` with `cp` (not
`mv`) and remove the old file.

## I lost my owner key passphrase

Signed attestations stay valid (admission uses the latest per account), but you cannot sign new ones: owner
key rotation is not available yet. To stop a provider's children, disable it in `provider-routing.json`.

## Codex reports the collector unreachable or "filesystem operation failed"

The command ran inside Codex's sandbox. Run `./setup.sh`, choose "Update the install and walk through every
step", and allow the rule in step 8 (Commands): the default there is "No, not now", so press Down once to
"Yes, allow these commands" before Enter. Then start a new Codex session in the project's folder.

## The Codex rule is "stale" after a Python or package upgrade

The rule names the exact Python build and runtime copy. Run `./setup.sh`, choose "Update the install and
walk through every step", and choose "Replace it" in step 8. The learning hooks name that Python too: the
update renders them again, so trust them again in Codex's `/hooks` afterwards.

## "This Codex session can write to ..."

The session guard refused the command. Quit that session and start Codex in the project's own folder:
never on a folder that contains a set-up project, the orchestrator clone, your home folder, a folder above
it or `/`. "Run Codex without --ephemeral" means the session left no record to check.

## Codex refuses with `host_client_unknown`

The orchestrator tells Codex and Claude Code apart by the environment markers each client sets. A Codex
session started from inside a Claude Code session (for example from Claude Code's terminal tool) inherits
Claude's markers as well as its own, so the host check refuses. Quit it and start Codex from your own
terminal, in the project's folder.

## The Codex rule is "unsupported"

It works only in your account's real `~/.codex`: set `HOME` to your own home folder, unset `CODEX_HOME`
(or point it at `~/.codex`), replace a symlinked `~/.codex` with a real folder, and run `./setup.sh` again.

## "the learning state is still in its old macOS folder"

Your records are still in `~/Library/Application Support/codex-agents`. Run `./setup.sh`, choose "Update
the install and walk through every step", and approve the move in step 5 (Collector) after checking the
creation date and owner key it shows. Until then every orchestrator command refuses. If it says the state
exists in both folders, keep the one in use, move the other out of both places, and run it again.

## "XDG_STATE_HOME must be inside a hidden folder in your home"

Unset `XDG_STATE_HOME`, or point it at a hidden folder in your home such as `~/.local/state`, and run
again. The default records folder is `~/.local/state/codex-agents`.
