"""Run the Provider Matching service.

    python services/agents/matching/__main__.py
    # or, once `services/agents` is a workspace member (see README.md):
    AGENT_ROLE=provider_matching uv run --no-sync python -m matching

The role is a *default* for this entry point, not a lock: the environment still
decides, because one image serves all seven roles (see `service.main`).

This is the one service whose agent tree has sub-agents (§3): `assembly.build_agent`
adds the Candidate Researcher and the Constraint Analyst as agent tools, both built
from this turn's own `ToolSet`.
"""

from __future__ import annotations

from referraleasy_agent_runtime import AgentRole
from referraleasy_agents.service import main

#: The role this service is the default deployment of.
ROLE = AgentRole.PROVIDER_MATCHING

if __name__ == "__main__":
    raise SystemExit(main(role=ROLE))
