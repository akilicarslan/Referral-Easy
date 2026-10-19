"""Run the Recovery & Handoff service.

    python services/agents/recovery/__main__.py
    # or, once `services/agents` is a workspace member (see README.md):
    AGENT_ROLE=recovery_handoff uv run --no-sync python -m recovery

The role is a *default* for this entry point, not a lock: the environment still
decides, because one image serves all seven roles (see `service.main`).

This is the role that owns §8's one-way door: after the third transient failure the
same agent is not called again in that workflow, and the refusal is code
(`recovery.FailureLedger.refuse_if_closed`), not a prompt.
"""

from __future__ import annotations

from referraleasy_agent_runtime import AgentRole
from referraleasy_agents.service import main

#: The role this service is the default deployment of.
ROLE = AgentRole.RECOVERY_HANDOFF

if __name__ == "__main__":
    raise SystemExit(main(role=ROLE))
