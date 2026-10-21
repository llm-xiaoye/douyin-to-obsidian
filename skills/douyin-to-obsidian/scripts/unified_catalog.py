#!/usr/bin/env python3
"""Read-only consistency checks for this package's knowledge index and import log."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import unified_store


def check(vault: str | Path) -> dict:
    root = Path(vault).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Vault 必须是目录")
    return unified_store.check(root)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check",))
    parser.add_argument("--vault", required=True)
    args = parser.parse_args(argv)
    try:
        result = check(args.vault)
    except (OSError, ValueError, UnicodeError) as exc:
        result = {"status": "error", "message": str(exc)}
        print(json.dumps(result, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
