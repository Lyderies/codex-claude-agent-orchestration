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
