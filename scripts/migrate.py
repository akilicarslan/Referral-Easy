#!/usr/bin/env python3
"""CLI entry point for the ReferralEasy migration runner.

This file is deliberately thin: the runner itself lives in `migrations/run.py`,
next to the SQL it applies, so that the two are read and reviewed together. This
wrapper exists only so the command lives where operators expect it.

    uv run python scripts/migrate.py --status
    uv run python scripts/migrate.py --dry-run
    DATABASE_URL=postgresql://... uv run python scripts/migrate.py
    uv run python scripts/migrate.py --validate-only   # no database required
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNNER_PATH = REPO_ROOT / "migrations" / "run.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("referraleasy_migrations", RUNNER_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - packaging error
        raise SystemExit(f"error: cannot load migration runner from {RUNNER_PATH}")
    module = importlib.util.module_from_spec(spec)
    # Register before executing: dataclasses (used by the runner) resolves the
    # defining module through sys.modules while the class body is processed.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    return _load_runner().main(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())
