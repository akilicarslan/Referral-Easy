"""``python -m referraleasy_eval`` — §14's evaluation CLI.

The exit code is the interface: `make eval` and CI read it rather than the prose, and 1 means a
release blocker was found. See :mod:`referraleasy_eval.cli` for the ladder.
"""

from __future__ import annotations

import sys

from referraleasy_eval.cli import main

if __name__ == "__main__":
    sys.exit(main())
