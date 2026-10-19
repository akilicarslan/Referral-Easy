"""Provider Matching's two tools (§3): `find_candidates`, `search_directory`.

§3 splits this agent's work in a way that is easy to blur: **the pool is built in
code and the constraint question is interpreted by a model**. These tools are the
code half, and they are where the split is enforced:

* distance, filtering, uniqueness and simple ranking come from
  `referraleasy_agents.matching_code`, never from a retrieval score;
* every candidate carries the evidence id of the retrieval that produced it, and a
  candidate that cannot be attributed is refused rather than returned;
* `search_directory` reports a **degradation level** with every answer, because a
  filtered-directory answer and a structured-directory answer are not the same
  claim — §10's ladder is named here rather than left implicit.

**Two route-shape findings, recorded because they shaped this module.**

1. `/directory/search` returns a *partial* `Provider` projection — the rows omit
   `first_visit_duration_minutes`, `return_visit_duration_minutes`,
   `accepts_insurance` and `synthetic`, so they cannot be validated as the frozen
   `Provider` contract and a candidate built from them would be missing fields a
   booking later needs. The structured pool therefore comes from `GET /providers`
   (which returns whole rows), and `/directory/search` is used only by
   :func:`search_directory`, which passes its rows through unvalidated and says so.
2. The frozen `Provider`, `Hospital` and `Location` contracts carry **no
   `evidence_id`**, so provenance cannot come from the entity row. It comes from
   `/evidence`, whose records carry `source_id` (`provider:<uuid>`,
   `hospital:<uuid>`, and one `provider:<uuid>#profile_text`). That index is what
   :func:`_evidence_index` builds in one call instead of one call per provider.

Both are findings about the fixtures and the routes, not about §3; they are stated
here so nobody has to rediscover them from a validation error.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from referraleasy_agents.canonical import id_text, id_texts
from referraleasy_agents.errors import UnknownDataError
from referraleasy_agents.matching_code import (
    build_candidate,
    dedupe_candidates,
    filter_providers,
    hospital_distance,
    locations_by_id,
    rank_candidates,
)
from referraleasy_agents.tools.call import ToolCall
from referraleasy_contracts.generated.common import DegradationLevel
from referraleasy_contracts.generated.directory import Hospital, Location, Provider

__all__ = [
    "find_candidates",
    "search_directory",
]


def _rows(body: Mapping[str, Any], key: str, *, route: str) -> list[dict[str, Any]]:
    rows = body.get(key)
    if not isinstance(rows, list):
        raise UnknownDataError(
            f"GET {route} answered without a `{key}` list; no candidates can be read from it."
        )
    return [row for row in rows if isinstance(row, dict)]


async def _hospitals_and_locations(
    call: ToolCall,
) -> tuple[dict[str, Hospital], dict[str, Location]]:
    """Every hospital and location, indexed. Both are needed to place a candidate."""
    hospitals_body = await call.api.get("/hospitals", budget=call.budget, lease=call.lease)
    locations_body = await call.api.get("/locations", budget=call.budget, lease=call.lease)
    hospitals = [
        Hospital.model_validate(row)
        for row in _rows(hospitals_body, "hospitals", route="/hospitals")
    ]
    locations = [
        Location.model_validate(row)
        for row in _rows(locations_body, "locations", route="/locations")
    ]
    return (
        {id_text(hospital.hospital_id): hospital for hospital in hospitals},
        locations_by_id(locations),
    )


async def _evidence_index(call: ToolCall) -> dict[str, list[str]]:
    """`source_id` -> evidence ids, from one `/evidence` call.

    The evidence fixture spells one record's `source_id` as
    `provider:<uuid>#profile_text`, so the index is keyed on the part before `#`:
    a citation for the provider is a citation for the provider, not for one field
    of its record. Several records may map to the same key, and all of them are
    kept — a candidate citing two retrieved records is more attributable, not less.
    """
    body = await call.api.get("/evidence", budget=call.budget, lease=call.lease)
    index: dict[str, list[str]] = {}
    for row in _rows(body, "records", route="/evidence"):
        source_id = row.get("source_id")
        evidence_id = row.get("evidence_id")
        if not isinstance(source_id, str) or not isinstance(evidence_id, str):
            continue
        ids = index.setdefault(source_id.split("#", 1)[0], [])
        if evidence_id not in ids:
            ids.append(evidence_id)
    if not index:
        raise UnknownDataError(
            "GET /evidence returned no usable records, so no candidate could cite anything. "
            "An unattributed pool is recollection, not retrieval (§3 Candidate Researcher)."
        )
    return index


async def find_candidates(
    call: ToolCall,
    *,
    specialty: str,
    origin: Mapping[str, float],
    constraints: Sequence[Mapping[str, Any]] | None = None,
    hospital_ids: Sequence[str] | None = None,
    max_distance_miles: float | None = None,
) -> dict[str, Any]:
    """Build the candidate pool for ``specialty`` around ``origin``.

    Order of operations, all of it in code:

    1. retrieve the specialty's providers (`/providers`), the hospitals and
       locations needed to place them (`/hospitals`, `/locations`), and the
       provenance index (`/evidence`);
    2. filter — case-folded exact specialty, hospital membership, the patient's
       language tags by BCP-47 prefix, and the hard radius;
    3. measure — great-circle miles from the origin to the provider's hospital;
    4. deduplicate on `(provider_id, hospital_id)`, unioning provenance;
    5. rank — nearest first, unknown distance last.

    A candidate is built only when there is something to cite. The provider's own
    evidence record is preferred; when a provider has none (fixture
    `…000013` is one) the hospital's directory evidence is used instead, and the
    result records which basis each candidate rested on. A candidate with neither
    is **skipped with a reason**, never returned unattributed.

    `max_distance_miles` is applied as a **hard** radius only when the caller says
    so. §3 forbids relaxing a hard constraint, and the mirror image of that rule is
    that this tool must not silently widen a radius either: a caller wanting a wider
    search asks again with a wider number, and the change is recorded as a
    `ConstraintChange` on the patient's decision.
    """
    if not isinstance(origin, Mapping) or "latitude" not in origin or "longitude" not in origin:
        raise UnknownDataError(
            "find_candidates needs an origin with latitude and longitude; without one there is no "
            "distance to compute and no basis for ordering (§3)."
        )
    # `params={}` on purpose: the route's own `specialty` match is exact and
    # case-sensitive (`p.specialty === query.specialty`), while §3 puts filtering in
    # code. Narrowing server-side with a differently-cased specialty would return an
    # empty directory that looks like `no_match`; fetching and filtering here keeps
    # one filter with one meaning.
    providers_body = await call.api.get(
        "/providers", params={}, budget=call.budget, lease=call.lease
    )
    providers = [
        Provider.model_validate(row)
        for row in _rows(providers_body, "providers", route="/providers")
    ]
    hospitals_by_id, locations = await _hospitals_and_locations(call)
    evidence = await _evidence_index(call)

    languages: list[str] = []
    for constraint in constraints or ():
        if (
            constraint.get("field") == "accepts_new_patients"
            and type(constraint.get("value")) is not bool
        ):
            raise UnknownDataError("The accepts_new_patients constraint requires a boolean.")
        if str(constraint.get("field")) == "provider_language" and constraint.get("value"):
            languages.append(str(constraint["value"]))

    selected = filter_providers(
        providers,
        specialty=specialty,
        languages=languages,
        hospital_ids=hospital_ids or (),
        accepts_new_patients=next(
            (
                item["value"]
                for item in constraints or ()
                if item.get("field") == "accepts_new_patients"
            ),
            None,
        ),
    )
    candidates = []
    accessibility = {
        str(c["value"]) for c in constraints or () if c.get("field") == "accessibility"
    }
    skipped: list[dict[str, str]] = []
    basis = {"provider_record": 0, "hospital_directory": 0}
    cited: dict[str, None] = {}
    for provider in selected:
        provider_id = id_text(provider.provider_id)
        for hospital_id in provider.hospital_ids:
            hospital_key = id_text(hospital_id)
            if hospital_ids and hospital_key not in {id_text(item) for item in hospital_ids}:
                continue
            hospital = hospitals_by_id.get(hospital_key)
            if hospital is None:
                skipped.append(
                    {
                        "provider_id": provider_id,
                        "hospital_id": hospital_key,
                        "reason": "hospital_unknown",
                    }
                )
                continue
            if not accessibility.issubset(
                {feature.value for feature in hospital.accessibility_features}
            ):
                skipped.append(
                    {
                        "provider_id": provider_id,
                        "hospital_id": hospital_key,
                        "reason": "accessibility_not_supported",
                    }
                )
                continue
            distance = hospital_distance(hospital, origin=origin, locations=locations)
            if max_distance_miles is not None and (
                distance is None or distance > max_distance_miles
            ):
                skipped.append(
                    {
                        "provider_id": provider_id,
                        "hospital_id": hospital_key,
                        "reason": "outside_hard_radius",
                    }
                )
                continue
            provider_evidence = evidence.get(f"provider:{provider_id}") or []
            hospital_evidence = evidence.get(f"hospital:{hospital_key}") or []
            provenance = provider_evidence or hospital_evidence
            if not provenance:
                skipped.append(
                    {
                        "provider_id": provider_id,
                        "hospital_id": hospital_key,
                        "reason": "no_provenance",
                    }
                )
                continue
            basis["provider_record" if provider_evidence else "hospital_directory"] += 1
            for evidence_id in provenance:
                cited.setdefault(evidence_id, None)
            candidates.append(
                build_candidate(
                    provider,
                    hospital_id=hospital_id,
                    distance_miles=distance,
                    evidence_ids=provenance,
                )
            )

    pool = rank_candidates(dedupe_candidates(candidates))
    return {
        "candidates": [candidate.model_dump(mode="json") for candidate in pool],
        "directory_labels": {
            **{id_text(provider.provider_id): provider.display_name for provider in selected},
            **{key: hospital.name for key, hospital in hospitals_by_id.items()},
        },
        "strategy": "specialty_filter",
        "filters_applied": [
            f"specialty={specialty}",
            *(
                f"hospital_id={item}"
                for item in sorted(id_text(item) for item in (hospital_ids or ()))
            ),
            *(f"provider_language={tag}" for tag in sorted(languages)),
            *(
                [f"max_distance_miles={max_distance_miles}"]
                if max_distance_miles is not None
                else []
            ),
        ],
        "skipped": skipped,
        "provenance_basis": basis,
        "count": len(pool),
        "evidence_ids": list(cited),
    }


async def search_directory(
    call: ToolCall,
    *,
    query: str,
    specialty: str | None = None,
    hospital_id: str | None = None,
    language: str | None = None,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Answer a directory question the structured filters can express.

    Every answer names its degradation level. Right now that level is
    `static_seed`: the query ran against the seeded directory rather than against
    Phase 6's index, so an answer can be *narrower* than a semantic search would be.
    Reporting that is not hedging — a caller that treats a filtered answer as an
    exhaustive one tells a patient "there is nobody" when it means "I did not find
    anyone this way", which is the difference between `no_match` and `unknown`.

    The rows are passed through **unvalidated**, because this route's rows are a
    partial projection (see the module docstring): validating them as `Provider`
    would fail on fields the route does not return, and dropping the rows would
    lose the only semantic-ish search the platform has today. The degradation
    level is what tells a caller how much to trust them.
    """
    params: dict[str, Any] = {
        "specialty": specialty,
        "hospital_id": hospital_id,
        "language": language,
        "plan_id": plan_id,
    }
    body = await call.api.get(
        "/directory/search", params=params, budget=call.budget, lease=call.lease
    )
    rows = _rows(body, "candidates", route="/directory/search")
    result_kind = str(body.get("result_kind") or "")
    if result_kind == "upstream_error":
        # The route's own vocabulary: "we could not ask" is not "we asked and there
        # is nothing". Reported as a degradation rather than an empty answer.
        return {
            "query": query,
            "candidates": [],
            "result_kind": "upstream_error",
            "degraded": DegradationLevel.static_seed.value,
            "count": 0,
            "evidence_ids": [],
        }
    return {
        "query": query,
        "candidates": rows,
        "result_kind": result_kind or ("candidates_found" if rows else "no_matching_provider"),
        # `vector_index` is the level a populated index will report; until then the
        # seeded structured directory is what answered. See the module docstring.
        "degraded": DegradationLevel.static_seed.value,
        "count": len(rows),
        "evidence_ids": id_texts([body["evidence_id"]]) if body.get("evidence_id") else [],
    }
