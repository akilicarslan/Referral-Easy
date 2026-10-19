"""ReferralEasy's seven A2A specialists, as one import package.

Each role is a service of its own (`services/agents/<role>/`) built from this package:
the same turn implementation, the same tool authority, and a role-specific card,
schemas and instruction. The split is a deployment fact rather than a code fact — one
image, seven roles, and `AgentRole` decides which.

What lives where, since that is the map a reader needs:

===========================  =========================================================
``cards.py``                 the protocol identity: Agent Cards, their hashes, and the
                             skill records they publish (§13)
``schemas.py``               the versioned input/output schemas, `extra="forbid"` on
                             both sides (§5, §6)
``prompts.py``               each role's briefing, including its §3 forbidden list
``forbidden.py``             those forbidden capabilities as callable refusals
``tools/``                   the tool surface: specs, handlers, the gate-bound call
                             path, and the provenance ledger
``assembly.py``              where the ADK agent, its tools and its session come from
``offline_model.py``         the offline model profile: a real ADK turn, no socket
``subagents.py``             Provider Matching's two local ADK sub-agents (§3)
``handler.py``               the runtime's `TaskHandler`: one turn, start to finish
``service.py``               settings to a running ASGI service

Importing this module imports nothing from `google.adk`, no database driver and no
provider SDK: `settings` resolution, the cards and the schemas are all available to a
process that will never run a turn (a manifest check, a registry, a test).
"""

from __future__ import annotations

__all__ = ["__version__"]

#: The package's own version, which is not the Agent Card version: the card version
#: moves when a card's published content moves (`cards.CARD_VERSION`), and this moves
#: when the code does.
__version__ = "0.1.0"
