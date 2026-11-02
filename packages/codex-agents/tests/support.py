from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
BUNDLE_ROOT = REPOSITORY_ROOT / "native-package"
ORCHESTRATION_ROOT = BUNDLE_ROOT / ".codex" / "orchestration"
FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures"
# The system temp directory with symlinks resolved (macOS /tmp is a symlink), because the
# package refuses symlinked paths.
TEMP_ROOT = os.path.realpath(tempfile.gettempdir())
sys.path.insert(0, str(ORCHESTRATION_ROOT))

# Operational QA runs checks inside macOS Seatbelt (sandbox-exec); other platforms fail closed.
MACOS_SANDBOX_ONLY = unittest.skipUnless(
    sys.platform == "darwin", "operational-check sandbox (sandbox-exec) is macOS-only"
)

# Provider admission denies any process with a provider API key, so the tests
# never inherit one from the developer's shell.
for _name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
    os.environ.pop(_name, None)

# The lifecycle detects its host from environment markers. These tests drive it
# as a Codex host unless a test sets the Claude markers itself, so they never
# depend on the shell that runs them.
for _name in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CODEX_SANDBOX"):
    os.environ.pop(_name, None)
os.environ["CODEX_THREAD_ID"] = "tests-thread"
os.environ["CODEX_SESSION_ID"] = "tests-session"

# Claude-host opens deny metered backends and model overrides found in the
# environment, so the tests never inherit those switches either.
for _name in (
    "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "AWS_BEARER_TOKEN_BEDROCK",
    "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_SUBAGENT_MODEL", "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_EFFORT_LEVEL",
):
    os.environ.pop(_name, None)
for _name in [name for name in os.environ if name.startswith("CLAUDE_CODE_USE_")]:
    os.environ.pop(_name, None)


def trusted_hook_interpreter() -> Path | None:
    """A Python the rendered learning hooks may name (hook_config.interpreter_problem).

    CI's toolcache Python sits outside the trusted installation folders, so a system or
    Homebrew one is looked for; None when there is none.
    """

    from codex_agents.hook_config import interpreter_problem

    candidates = [sys.executable, "/opt/homebrew/bin/python3", "/usr/local/bin/python3",
                  "/Library/Frameworks/Python.framework/Versions/Current/bin/python3",
                  "/usr/bin/python3"]
    for candidate in candidates:
        real = Path(os.path.realpath(candidate))
        if real.is_file() and interpreter_problem(real, Path("/nonexistent-project")) is None:
            return real
    return None
