"""Validate the schema-v3 semantic authoring sources and print JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    from src.nl2sql.semantic.authoring import compile_authoring_files

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--semantic", type=Path, default=Path("configs/semantic/semantic.md"))
    parser.add_argument("--qa", type=Path, default=Path("configs/semantic/qa.md"))
    parser.add_argument("--views", type=Path, default=Path("configs/semantic/ai_views.yaml"))
    parser.add_argument(
        "--strict-metadata",
        action="store_true",
        help="do not infer owner, sensitivity, or freshness for legacy assets",
    )
    args = parser.parse_args()
    result = compile_authoring_files(
        args.semantic,
        args.qa,
        args.views,
        legacy_defaults=not args.strict_metadata,
    )
    print(json.dumps(result.report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
