#!/usr/bin/env python3
"""Safe handler-command fixture: copies JSON stdin to a local output file.

The command receives remote data through stdin.  It does not evaluate that
data as shell syntax.  This is useful for an end-to-end passive-trigger test.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    value = json.load(__import__("sys").stdin)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
