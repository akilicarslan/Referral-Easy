"""The seven Agent Cards — immutable, published, and cross-checked against reality.

§13 requires an immutable card per agent; ADR-0028 left a loose end and named it:
the `agent_card_skill_id` values in `packages/skills/*/manifest.json` were invented
because no card registry existed, and *"nothing cross-checks them"*. These cards
are that registry's first half. Each card's `skills` list is read **from the
manifests**, keyed by `owner_agent`, so the ids stop being invented: a manifest
that names an owner with no card, or a card that names a skill no manifest
provides, now fails a test instead of being discovered during a demo.

Three things are structural rather than asserted:

* **A card cannot carry a tool outside the allowlist.** `tools` is
  `sorted(allowed_tools(role))` and there is no parameter that could widen it. A
  skill manifest may declare tools; the card does not read that field, so the
  §6/ADR-0028 boundary ("adding a skill grants no tool permission") survives the
  card layer too.
* **`agent_hash` covers every field §13 lists.** The hash is taken over the
  description, the instruction, the tools, the skills and the sub-agents, so a
  change to any of them changes the hash; `test_agent_cards.py` mutates each one
  and asserts the hash moves. It is a SHA-256 of canonical JSON — no field order
  and no whitespace can change it.
* **The cards are built once, at import, and every read is a copy.** A consumer that
  could mutate a card would make "immutable Agent Card" a wish. `AgentCard` is a
  generated contract model that is *not* frozen (a finding, recorded in
  :func:`cards` and `card_for`), so the registry is a `MappingProxyType` and each
  reader is handed a deep copy — the guarantee is the registry's, which is the thing
  every caller shares.

What is *not* here: the release manifest itself (§13's `release_manifest`, with
per-component hashes and a pinned conversation). That is Phase 3.5's
`registry/`, and this module deliberately stops at producing the agent hash and
the card the registry will store.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from types import MappingProxyType
from typing import Any, Final

from referraleasy_agent_runtime import AgentRole, allowed_tools
from referraleasy_agents.prompts import build_instruction
from referraleasy_agents.schemas import (
    SCHEMA_VERSION,
    input_schema_id,
    output_schema_id,
)
from referraleasy_contracts.generated.task import AgentCard, Auth, Capabilities, Skill

__all__ = [
    "CARD_NAME_BY_ROLE",
    "CARD_VERSION",
    "ROLE_BY_CARD_NAME",
    "SUB_AGENT_NAMES",
    "agent_hash",
    "card_for",
    "card_payload",
    "cards",
    "schema_version",
]

#: §3's role names, hyphenated for the A2A card name (which the contract pins to
#: `^[a-z][a-z0-9-]*$`). The mapping is explicit rather than derived, so the
#: service name (`agent-referral_intake`) and the card name (`referral-intake`)
#: can differ without either being guessed.
CARD_NAME_BY_ROLE: Final[Mapping[AgentRole, str]] = MappingProxyType(
    {
        AgentRole.CARE_COORDINATOR: "care-coordinator",
        AgentRole.REFERRAL_INTAKE: "referral-intake",
        AgentRole.COVERAGE_NETWORK: "coverage-network",
        AgentRole.PROVIDER_MATCHING: "provider-matching",
        AgentRole.REFERRAL_READINESS: "referral-readiness",
        AgentRole.SCHEDULING: "scheduling",
        AgentRole.RECOVERY_HANDOFF: "recovery-handoff",
    }
)
ROLE_BY_CARD_NAME: Final[Mapping[str, AgentRole]] = MappingProxyType(
    {name: role for role, name in CARD_NAME_BY_ROLE.items()}
)

#: The card schema version. Bumping it is a release, not an edit (§13).
CARD_VERSION: Final = "2.0.0"

#: The two local ADK sub-agents, both inside Provider Matching (§3).
SUB_AGENT_NAMES: Final[Mapping[AgentRole, tuple[str, ...]]] = MappingProxyType(
    {
        AgentRole.PROVIDER_MATCHING: ("candidate-researcher", "constraint-analyst"),
    }
)

#: Scopes a caller must present. Checked at the transport, never from the payload (§5, §9).
_CARD_SCOPES: Final = ("referraleasy.a2a.invoke",)

_DESCRIPTIONS: Final[Mapping[AgentRole, str]] = MappingProxyType(
    {
        AgentRole.CARE_COORDINATOR: (
            "Routes a synthetic patient referral workflow: decides which specialists to call, "
            "which question to ask, and when to stop. Holds only the delegation tool — it can "
            "never write a booking or change a patient's choice."
        ),
        AgentRole.REFERRAL_INTAKE: (
            "Reads a referral into contract-shaped fields and names what is missing or "
            "contradictory. Cannot diagnose and cannot determine a specialty the referral does "
            "not state."
        ),
        AgentRole.COVERAGE_NETWORK: (
            "Interprets a plan, a provider network and referral rules together, and says which "
            "result is unknown. Cannot guarantee coverage or payment, and cannot invent a rule."
        ),
        AgentRole.PROVIDER_MATCHING: (
            "Builds and explains a provider candidate pool against the patient's constraints, "
            "including evidence-based alternatives. Cannot choose for the patient and cannot "
            "relax a hard constraint."
        ),
        AgentRole.REFERRAL_READINESS: (
            "Compares one hospital's document requirements with what exists and certifies the "
            "package. Cannot assume a document that does not exist and cannot grant approval."
        ),
        AgentRole.SCHEDULING: (
            "Finds fitting slots, writes the booking under a live approval and reconciles an "
            "unknown outcome with the same idempotency key. The only holder of booking write "
            "authority."
        ),
        AgentRole.RECOVERY_HANDOFF: (
            "Classifies a failure and takes the state machine's bounded options: the directory "
            "fallback, a new alternative, or a human handoff. Cannot override a policy block and "
            "cannot call the same failed agent forever."
        ),
    }
)


def _skill_records(role: AgentRole) -> tuple[Skill, ...]:
    """The card's skill entries, read from the manifests that name ``role`` as owner.

    Imported lazily so this module can be imported (and its prompt/hash logic
    tested) without the skill packages present, and read through the package's own
    loader so the manifest stays the single source of truth.
    """
    import referraleasy_skills as skills_package

    records: list[Skill] = []
    for manifest in skills_package.list_manifests():
        if manifest.owner_agent != role.value:
            continue
        rationale = " ".join((manifest.summary or "").split())
        records.append(
            Skill(
                id=manifest.agent_card_skill_id,
                name=manifest.name,
                description=rationale,
                skill_version=manifest.version,
            )
        )
    return tuple(sorted(records, key=lambda skill: skill.id))


def _hash_payload(
    role: AgentRole,
    *,
    description: str,
    instruction: str,
    tools: Sequence[str],
    skills: Sequence[Skill],
    sub_agents: Sequence[str],
) -> dict[str, Any]:
    """Everything §13 says a change to which must change an agent's hash."""
    return {
        "name": CARD_NAME_BY_ROLE[role],
        "role": role.value,
        "version": CARD_VERSION,
        "description": description,
        "instruction": instruction,
        "tools": list(tools),
        "skills": [
            {
                "id": skill.id,
                "name": skill.name,
                "description": skill.description,
                "skill_version": skill.skill_version,
            }
            for skill in skills
        ],
        "sub_agents": list(sub_agents),
        "input_schema_id": input_schema_id(role),
        "output_schema_id": output_schema_id(role),
    }


def agent_hash(
    role: AgentRole,
    *,
    description: str | None = None,
    instruction: str | None = None,
    tools: Iterable[str] | None = None,
    skills: Iterable[Skill] | None = None,
    sub_agents: Iterable[str] | None = None,
) -> str:
    """The 64-hex agent hash, over the fields §13 names.

    Every parameter has a default taken from the card data, which is what lets a
    test change exactly one thing and observe that the hash moved, without
    rebuilding a card from scratch.
    """
    resolved_skills = tuple(skills) if skills is not None else _skill_records(role)
    payload = _hash_payload(
        role,
        description=description if description is not None else _DESCRIPTIONS[role],
        instruction=(
            instruction if instruction is not None else build_instruction(role, skills="")
        ),
        tools=tuple(tools) if tools is not None else tuple(sorted(allowed_tools(role))),
        skills=resolved_skills,
        sub_agents=tuple(sub_agents) if sub_agents is not None else SUB_AGENT_NAMES.get(role, ()),
    )
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _build_card(role: AgentRole) -> AgentCard:
    skills = _skill_records(role)
    sub_agents = SUB_AGENT_NAMES.get(role, ())
    return AgentCard(
        name=CARD_NAME_BY_ROLE[role],
        description=_DESCRIPTIONS[role],
        version=CARD_VERSION,
        agent_hash=agent_hash(role, skills=skills, sub_agents=sub_agents),
        capabilities=Capabilities(
            streaming=True,
            pushNotifications=False,
            stateTransitionHistory=True,
        ),
        skills=list(skills),
        input_schema_id=input_schema_id(role),
        output_schema_id=output_schema_id(role),
        auth=Auth(scheme="bearer", verified_at_transport=True, required_scopes=list(_CARD_SCOPES)),
        tools=sorted(allowed_tools(role)),
        sub_agents=list(sub_agents) or None,
    )


#: Built once. A card that changed between two calls would make the hash worthless.
_CARDS: Final[Mapping[AgentRole, AgentCard]] = MappingProxyType(
    {role: _build_card(role) for role in AgentRole}
)


def cards() -> Mapping[AgentRole, AgentCard]:
    """Every card as a copy, so a caller cannot write into the registry.

    A **finding, and how this module answers it**: `AgentCard` is a generated contract model with
    ``extra="forbid"`` and **no** ``frozen=True``, so the object itself is mutable — the §13 word
    "immutable" is not enforced by the type. `packages/contracts` is outside this package's scope,
    so the guarantee is placed here instead: the registry is a `MappingProxyType` *and* every reader
    gets a deep copy, which makes "a consumer cannot change what the next caller sees" true without
    changing the contract. The cost is one deep copy per read; the cards are read to answer a
    request, not in a loop.
    """
    return MappingProxyType({role: card.model_copy(deep=True) for role, card in _CARDS.items()})


def card_for(role: AgentRole) -> AgentCard:
    """One card, as a copy. Two calls return equal cards that cannot affect each other."""
    return _CARDS[role].model_copy(deep=True)


def card_payload(role: AgentRole) -> dict[str, Any]:
    """The wire form, for the ``/.well-known/agent-card.json`` route."""
    return card_for(role).model_dump(mode="json")


def schema_version() -> str:
    """The schema version the cards publish, exposed so a test needs no string literal."""
    return SCHEMA_VERSION
