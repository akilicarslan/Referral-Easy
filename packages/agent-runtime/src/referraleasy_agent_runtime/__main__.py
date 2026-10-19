"""``python -m referraleasy_agent_runtime`` — serve the role in ``AGENT_ROLE``.

The seven services each get a real entry point of their own (they wire a handler);
this one exists so the image can be started, and ``--check`` run, without one.
"""

from __future__ import annotations

import sys

from referraleasy_agent_runtime.app import main

if __name__ == "__main__":
    sys.exit(main())
