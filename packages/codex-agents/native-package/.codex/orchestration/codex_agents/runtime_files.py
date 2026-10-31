"""The exact files that make up the orchestration runtime, decided by this package.

The owner's Codex command rule runs a copy of these files, and nothing else, from
an owner-only directory, and their digest is the runtime identity the collector
authorizes and `runtime-releases.json` records. The list is a constant of the
code doing the work, never read from a project's install manifest, which a
session with write access to the project could extend. A test keeps it equal to
the package's own files.

Paths are relative to `.codex/orchestration/`. Signed release data
(`config/learning-releases.json`) and the hook script are package files but not
runtime code: a protected copy reads release data from its project.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

ORCHESTRATION_PREFIX = ".codex/orchestration/"
RUNTIME_FILES = (
    "cli.py",
    "codex_agents/__init__.py",
    "codex_agents/bridge.py",
    "codex_agents/catalog.py",
    "codex_agents/claude_host.py",
    "codex_agents/contracts.py",
    "codex_agents/evaluation.py",
    "codex_agents/git_plumbing.py",
    "codex_agents/host_commands.py",
    "codex_agents/host_paths.py",
    "codex_agents/hosts.py",
    "codex_agents/learning_adapters.py",
    "codex_agents/learning_contracts.py",
    "codex_agents/learning_engine.py",
    "codex_agents/learning_store.py",
    "codex_agents/process_tree.py",
    "codex_agents/providers.py",
    "codex_agents/routing.py",
    "codex_agents/runtime_files.py",
    "codex_agents/supervision.py",
    "codex_agents/validation.py",
    "config/discovery-policy.json",
    "config/evaluation-summary.json",
    "config/learning-clients.json",
    "config/learning-overhead.json",
    "config/learning-producers.json",
    "config/routing-policy.json",
    "schemas/agent-state.schema.json",
    "schemas/assignment.schema.json",
    "schemas/evaluation-record.schema.json",
    "schemas/learning-canary-report.schema.json",
    "schemas/learning-candidate.schema.json",
    "schemas/learning-comparison.schema.json",
    "schemas/learning-evaluation-assignment.schema.json",
    "schemas/learning-evaluation-case.schema.json",
    "schemas/learning-evaluation-trial.schema.json",
    "schemas/learning-event.schema.json",
    "schemas/learning-evidence-review.schema.json",
    "schemas/learning-export.schema.json",
    "schemas/learning-feedback.schema.json",
    "schemas/learning-grader-calibration.schema.json",
    "schemas/learning-host-observation.schema.json",
    "schemas/learning-outcome.schema.json",
    "schemas/learning-owner-grant.schema.json",
    "schemas/learning-promotion.schema.json",
    "schemas/learning-qa-evidence.schema.json",
    "schemas/operational-checks.schema.json",
    "schemas/parent-grant.schema.json",
    "schemas/project-agent-routing.schema.json",
    "schemas/result.schema.json",
    "schemas/task.schema.json",
)
# Package files under `.codex/orchestration/` that are not runtime code.
PACKAGE_DATA_FILES = ("config/learning-releases.json", "learning_hook.py")
# Files the installer or setup writes there for the project itself.
PROJECT_FILES = (
    "install-manifest.json",
    "operational-checks.json",
    "project-agent-routing.json",
    "provider-routing.json",
)
RELEASES_FILE = "config/learning-releases.json"


def runtime_digest(contents: Mapping[str, bytes]) -> str:
    """The digest of exactly the runtime files, each length-prefixed with its path.

    `contents` maps every name in RUNTIME_FILES (and no other) to its bytes.
    """

    if set(contents) != set(RUNTIME_FILES):
        raise ValueError("the runtime file set is incomplete or has extra files")
    hasher = hashlib.sha256()
    for relative in RUNTIME_FILES:
        name = relative.encode("utf-8")
        content = contents[relative]
        hasher.update(len(name).to_bytes(4, "big"))
        hasher.update(name)
        hasher.update(len(content).to_bytes(8, "big"))
        hasher.update(content)
    return hasher.hexdigest()
