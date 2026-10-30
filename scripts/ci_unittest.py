#!/usr/bin/env python3
"""Run a unittest suite and report each failure as a GitHub Actions error annotation.

Annotations are readable without access to the full job log, which keeps CI failures
diagnosable from anywhere. Usage: ``ci_unittest.py <start-directory>``.
"""

from __future__ import annotations

import os
import sys
import unittest

MAX_ANNOTATIONS = 40


def _escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(argv: list[str]) -> int:
    start = argv[1] if len(argv) > 1 else "tests"
    sys.path.insert(0, os.getcwd())  # match `python -m unittest`, which imports from the cwd
    suite = unittest.defaultTestLoader.discover(start)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    problems = [("ERROR", test, trace) for test, trace in result.errors]
    problems += [("FAIL", test, trace) for test, trace in result.failures]
    for kind, test, trace in problems[:MAX_ANNOTATIONS]:
        lines = [line for line in trace.strip().splitlines() if line.strip()]
        message = f"{kind}: {test.id()}\n" + "\n".join(lines[-4:])
        print(f"::error title={kind} {test.id()}::{_escape(message)}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
