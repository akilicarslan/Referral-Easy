"""Run the Referral Intake service.

    python services/agents/intake/__main__.py
    # or, once `services/agents` is a workspace member (see README.md):
    AGENT_ROLE=referral_intake uv run --no-sync python -m intake

The role is a *default* for this entry point, not a lock: the environment still
decides, because one image serves all seven roles (see `service.main`).

This file is the service's entry point, not a package: it is run by path, and the
ASGI factory in `service.asgi_app` is the equivalent for an ASGI server.
"""

from __future__ import annotations

from referraleasy_agent_runtime import AgentRole
from referraleasy_agents.service import main

#: The role this service is the default deployment of.
ROLE = AgentRole.REFERRAL_INTAKE

if __name__ == "__main__":
    raise SystemExit(main(role=ROLE))
