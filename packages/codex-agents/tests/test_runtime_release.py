from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import json
import unittest

import tests.support  # noqa: F401
from tests.support import BUNDLE_ROOT, REPOSITORY_ROOT

from codex_agents.learning_store import COLLECTOR_RUNTIME_DIGEST
from codex_agents.runtime_files import (
    PACKAGE_DATA_FILES,
    RUNTIME_FILES,
    runtime_digest,
)

RELEASES = REPOSITORY_ROOT / "runtime-releases.json"


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


class RuntimeReleaseTests(unittest.TestCase):
    """A collector authorizes one build per version, so a runtime change needs a new version."""

    def test_current_runtime_is_recorded_for_the_current_version(self) -> None:
        releases = json.loads(RELEASES.read_text(encoding="utf-8"))
        version = (BUNDLE_ROOT / "VERSION").read_text(encoding="ascii").strip()
        self.assertIn(
            version, releases,
            f"VERSION {version} has no entry in runtime-releases.json",
        )
        self.assertEqual(
            releases[version], COLLECTOR_RUNTIME_DIGEST,
            "the collector runtime changed: bump VERSION and record the new digest",
        )

    def test_the_runtime_file_set_is_exactly_the_package_orchestration_files(self) -> None:
        # The protected copy and the runtime digest use this constant, never a
        # project's manifest, so it must name every file the CLI may load.
        orchestration = BUNDLE_ROOT / ".codex" / "orchestration"
        bundled = {
            path.relative_to(orchestration).as_posix()
            for path in orchestration.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
            and path.name != ".DS_Store"
        }
        self.assertEqual(bundled, {*RUNTIME_FILES, *PACKAGE_DATA_FILES})
        self.assertEqual(list(RUNTIME_FILES), sorted(set(RUNTIME_FILES)))
        self.assertEqual(
            COLLECTOR_RUNTIME_DIGEST,
            runtime_digest({
                relative: (orchestration / relative).read_bytes()
                for relative in RUNTIME_FILES
            }),
        )

    def test_versions_increase_and_digests_are_unique(self) -> None:
        releases = json.loads(RELEASES.read_text(encoding="utf-8"))
        versions = list(releases)
        self.assertEqual(versions, sorted(versions, key=_version_key))
        self.assertEqual(len(set(releases.values())), len(releases))


if __name__ == "__main__":
    unittest.main()
