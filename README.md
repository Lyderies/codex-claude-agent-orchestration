# Planning

This branch holds the plans, design working documents and research for
[codex-claude-agent-orchestration](https://github.com/Lyderies/codex-claude-agent-orchestration).
None of it is shipped. Releases come from `main`; development happens on `version/<semver>` branches.
This branch shares no history with them and is never merged into them.

| Folder | Contents |
|---|---|
| `delivery/` | The delivery plan (`plan.md`) and the goal design documents (`g2-design.md`) |
| `research/` | Research reports and dispatch and containment spikes, copied from `staging` (now retired) |

User and contributor documentation stays on `main`: the README, `docs/` (including the security model
and how it works), `CONTRIBUTING.md`, `SECURITY.md` and the CHANGELOG.

## Check it out beside the code

From a clone of the repository:

```sh
git fetch origin planning
git worktree add ../planning planning
```

Links into the code tree point at `main` on GitHub. When a plan refers to a particular state of the
code, record the commit or version it refers to.

The repository's no-identifiers rule applies here too: no personal names, emails, hostnames, home
paths or machine pins. This branch has no scripts or hooks of its own, so run the scan from the root
of your code checkout before committing here:

```sh
(cd ../planning && python3 "$OLDPWD/scripts/check_identifiers.py")
```
