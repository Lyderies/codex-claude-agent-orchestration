from __future__ import annotations

import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
BUNDLE_ROOT = REPOSITORY_ROOT / "native-package"
ORCHESTRATION_ROOT = BUNDLE_ROOT / ".codex" / "orchestration"
FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ORCHESTRATION_ROOT))
