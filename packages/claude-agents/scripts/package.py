"""Owner-invoked native package preview/install/update/verify/remove; no providers."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "src"))

def main() -> int:
    from claude_agents.packaging import PackageError, package_operation

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("install", "update", "verify", "remove"))
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--previous-source", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = package_operation(
            SOURCE_ROOT, args.target.absolute(), args.operation, apply=args.apply,
            previous_source_root=(args.previous_source.absolute()
                                  if args.previous_source else None),
        )
    except PackageError as exc:
        print(json.dumps({"error": str(exc), "provider_execution_enabled": False}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
