"""Run the Scheduling service.

    python services/agents/scheduling/__main__.py
    # or, once `services/agents` is a workspace member (see README.md):
    AGENT_ROLE=scheduling uv run --no-sync python -m scheduling

The role is a *default* for this entry point, not a lock: the environment still
decides, because one image serves all seven roles (see `service.main`).

This is the only service that writes: `create_booking`, `cancel_booking` and
`reschedule_booking` are on its allowlist, and every one of them goes through the
write guard — a valid approval and the same idempotency key (§3, §8).
"""

from __future__ import annotations

from referraleasy_agent_runtime import AgentRole
from referraleasy_agents.service import main

#: The role this service is the default deployment of.
ROLE = AgentRole.SCHEDULING

if __name__ == "__main__":
    raise SystemExit(main(role=ROLE))
