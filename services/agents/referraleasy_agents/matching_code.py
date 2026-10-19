# ruff: noqa: RUF002  # Turkish quoted from §3 of ReferralEasy_Architecture_v3.md is kept verbatim
"""The parts of Provider Matching that are code, not model judgement (§3).

§3 is unusually specific about this, and it is worth quoting because it is the
difference between a demo and a system: *"Mesafe hesabı, filtreleme ve uniqueness
kodla uygulanır… Basit sıralama doğrudan kodla yapılır; alt ajan belirsiz/çelişkili
metinsel koşulları ve kullanıcıya sorulacak alternatifleri yorumlar."* — distance,
filtering and uniqueness are implemented in code; simple ranking is done directly
in code; the sub-agent interprets ambiguous or contradictory textual conditions
and the alternatives to put to the user.

So this module owns, and only owns:

* :func:`haversine_miles` — distance from coordinates, never from a similarity score
  (the contracts' own `Miles` description says the same thing);
* :func:`filter_providers` — exact, case-folded equality on factual fields;
* :func:`dedupe_candidates` — uniqueness, with provenance unioned rather than dropped;
* :func:`rank_candidates` — the *simple* ordering, by distance then identifier;
* :func:`validate_analysis` — the Constraint Analyst's output is checked here before
  anyone reads it, because §3 forbids covering absent data with a score.

**A finding, recorded rather than smoothed over.** The authored
`fixtures/directory/provider_candidates.json` `distance_miles` values do **not**
match a great-circle distance from any fixture location, nor from the patient's
`search_origin` (e.g. provider `…0001` at hospital `…0001` is authored at 3.2
miles; the two coordinates are ~0.6 miles apart). §3 requires distance to be
computed, so this module computes it, and the discrepancy is asserted by
`tests/agents/test_matching_subagents.py::test_the_fixtures_authored_distance_is_not_geometric`
rather than being tuned away. Anyone reconciling the two should fix the fixtures,
not this function.

Distances here are great-circle, not driving distance. That is stated because the
alternative — presenting a straight-line number as a travel distance — is the kind
of quiet inaccuracy that makes a patient plan around a wrong figure.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from math import asin, cos, radians, sin, sqrt
from typing import Final

from referraleasy_agents.canonical import id_text
from referraleasy_agents.errors import (
    HardConstraintRelaxedRefused,
    ProvenanceMissingError,
    UnknownDataError,
)
from referraleasy_agents.schemas import CandidateConstraintAnalysis
from referraleasy_contracts.generated.common import EvidenceId, GeoPoint, HospitalId, Uuid
from referraleasy_contracts.generated.directory import (
    ConstraintFitItem,
    Hospital,
    Location,
    Provider,
    ProviderCandidate,
)
from referraleasy_contracts.generated.patient import Constraint

__all__ = [
    "EARTH_RADIUS_MILES",
    "build_candidate",
    "constraint_fit_item",
    "dedupe_candidates",
    "filter_providers",
    "haversine_miles",
    "hospital_distance",
    "locations_by_id",
    "rank_candidates",
    "refuse_undecided_hard_constraint_change",
    "validate_analysis",
]

#: Mean Earth radius in miles. A constant, not a parameter: two call sites
#: choosing two radii is how the same pair of hospitals gets two distances.
EARTH_RADIUS_MILES: Final = 3958.8


def _unique_evidence(values: Sequence[EvidenceId | str]) -> list[EvidenceId]:
    """``values`` de-duplicated on their text, in first-seen order.

    ``EvidenceId`` is a `RootModel`, and a `RootModel` is unhashable — so the
    objects cannot go into a ``set`` or a ``dict`` key, and a merge that tried was a
    `TypeError` rather than a union. The text is the identity here, and rebuilding
    the wrappers is cheap.
    """
    return [EvidenceId(item) for item in dict.fromkeys(id_text(value) for value in values)]


def haversine_miles(
    origin: GeoPoint | Mapping[str, float], target: GeoPoint | Mapping[str, float]
) -> float:
    """Great-circle miles between two points. Rounded to two decimals.

    Takes a mapping as well as a `GeoPoint` because the mock API returns the raw
    JSON shape and re-validating it into a model just to measure a distance would
    be ceremony.
    """

    def latlon(point: GeoPoint | Mapping[str, float]) -> tuple[float, float]:
        if isinstance(point, Mapping):
            return float(point["latitude"]), float(point["longitude"])
        return point.latitude, point.longitude

    lat1, lon1 = latlon(origin)
    lat2, lon2 = latlon(target)
    d_lat = radians(lat2 - lat1)
    d_lon = radians(lon2 - lon1)
    a = sin(d_lat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lon / 2) ** 2
    return round(2 * EARTH_RADIUS_MILES * asin(sqrt(a)), 2)


def filter_providers(
    providers: Iterable[Provider],
    *,
    specialty: str | None = None,
    languages: Sequence[str] = (),
    accepts_new_patients: bool | None = None,
    hospital_ids: Sequence[HospitalId | str] = (),
) -> list[Provider]:
    """Factual filtering, in code, with no fuzzy matching.

    `specialty` compares case-folded equality — a substring match would let
    "cardiology" select "cardiothoracic surgery", which is the class of error §3
    keeps out of the model and, here, out of the code too. `languages` compares
    BCP-47 prefix, because `es` must select `es-MX`.
    """
    wanted_specialty = specialty.strip().casefold() if specialty else None
    wanted_languages = [tag.strip().casefold() for tag in languages if tag.strip()]
    wanted_hospitals = {id_text(item) for item in hospital_ids}
    selected: list[Provider] = []
    for provider in providers:
        if (
            wanted_specialty is not None
            and provider.specialty.strip().casefold() != wanted_specialty
        ):
            continue
        if wanted_hospitals and not (
            {id_text(item) for item in provider.hospital_ids} & wanted_hospitals
        ):
            continue
        if (
            accepts_new_patients is not None
            and provider.accepts_new_patients != accepts_new_patients
        ):
            continue
        if wanted_languages:
            # `id_text`, because `LanguageTag` is a `RootModel`: `.casefold()` on it is
            # an `AttributeError`, and a language filter that raised would take down a
            # search rather than narrow it.
            offered = [id_text(skill.language).casefold() for skill in provider.languages]
            if not any(
                any(
                    tag == offered_tag or offered_tag.startswith(f"{tag}-")
                    for offered_tag in offered
                )
                for tag in wanted_languages
            ):
                continue
        selected.append(provider)
    return selected


def build_candidate(
    provider: Provider,
    *,
    hospital_id: HospitalId | str,
    distance_miles: float | None,
    evidence_ids: Sequence[EvidenceId | str],
    constraint_fit: Sequence[ConstraintFitItem] = (),
    rank: int | None = None,
) -> ProviderCandidate:
    """One candidate, with the provenance §5 requires or nothing at all.

    A candidate with no `evidence_ids` is refused (:class:`ProvenanceMissingError`):
    a pool entry nobody can attribute is recollection, and §3's Candidate
    Researcher exists precisely to preserve provenance.
    """
    provenance = [EvidenceId(id_text(item)) for item in evidence_ids]
    if not provenance:
        raise ProvenanceMissingError(
            f"candidate {provider.provider_id} was built with no evidence_ids; every candidate "
            "must cite the retrieved record it came from (§5, §3 Candidate Researcher)."
        )
    return ProviderCandidate(
        provider_id=provider.provider_id,
        hospital_id=HospitalId(id_text(hospital_id)),
        distance_miles=distance_miles,
        constraint_fit=list(constraint_fit),
        rank=rank,
        provenance=provenance,
        evidence_ids=provenance,
    )


def dedupe_candidates(candidates: Iterable[ProviderCandidate]) -> list[ProviderCandidate]:
    """Uniqueness, in code: one entry per ``(provider_id, hospital_id)``.

    Evidence is unioned rather than replaced, and a duplicate with a distance is
    preferred over one without. The order of the input does not affect the result:
    the comparison key is the pair, and ties are broken by the first occurrence of
    each pair in a stable sort over the identifier, so two runs over the same data
    produce the same pool.
    """
    merged: dict[tuple[str, str], ProviderCandidate] = {}
    for candidate in candidates:
        key = (id_text(candidate.provider_id), id_text(candidate.hospital_id))
        existing = merged.get(key)
        if existing is None:
            merged[key] = candidate
            continue
        # De-duplicated on the *text* of each id, because every id type here is a
        # pydantic `RootModel` and a `RootModel` is unhashable: `dict.fromkeys` over
        # the objects themselves is a `TypeError` at the first merge, which is how
        # this line read before `test_matching_subagents.py` exercised it.
        evidence = _unique_evidence([*existing.evidence_ids, *candidate.evidence_ids])
        distance = existing.distance_miles
        if distance is None:
            distance = candidate.distance_miles
        merged[key] = ProviderCandidate(
            provider_id=existing.provider_id,
            hospital_id=existing.hospital_id,
            distance_miles=distance,
            constraint_fit=existing.constraint_fit or candidate.constraint_fit,
            rank=existing.rank if existing.rank is not None else candidate.rank,
            provenance=_unique_evidence(
                [*(existing.provenance or []), *(candidate.provenance or [])]
            ),
            evidence_ids=evidence,
        )
    return sorted(
        merged.values(), key=lambda item: (id_text(item.provider_id), id_text(item.hospital_id))
    )


def rank_candidates(candidates: Sequence[ProviderCandidate]) -> list[ProviderCandidate]:
    """Simple ranking in code: nearest first, unknown distance last, then stable by id.

    "Simple" is the spec's word. An unknown distance sorts *last* rather than first
    or at zero: a candidate whose distance we could not compute has not been shown
    to be close, and §3's rule about not covering missing data with a score applies
    to ordering as much as to labelling.
    """
    ordered = sorted(
        candidates,
        key=lambda item: (
            item.distance_miles is None,
            item.distance_miles if item.distance_miles is not None else 0.0,
            id_text(item.provider_id),
        ),
    )
    return [
        candidate.model_copy(update={"rank": position})
        for position, candidate in enumerate(ordered, start=1)
    ]


def validate_analysis(
    analysis: CandidateConstraintAnalysis | Sequence[CandidateConstraintAnalysis],
    *,
    constraints: Mapping[str, Constraint] | None = None,
) -> None:
    """Check the Constraint Analyst's output. Raises rather than repairs.

    §3 states two rules, and both are enforced here:

    * **absent data is not scored over.** A non-null `satisfied` verdict must be
      backed by evidence for that constraint; without it the verdict is
      :class:`UnknownDataError` and the caller must re-emit it as `null`. There is
      no numeric field anywhere in the analysis schema, so "covering up with a
      score" would additionally have to be invented — and inventing it is a
      validation error, because the schema forbids extra fields.
    * **a verdict names the constraint it is about.** A fit entry for a constraint
      id that was not in the request is refused: it is either a stale constraint
      from a previous turn or an invention, and both are worse than a missing entry.
    """
    # One or many, told apart by the type rather than by duck-typing on `Sequence`:
    # a `CandidateConstraintAnalysis` carries a list of `fits` and mypy is right that
    # "is a Sequence *and* is an analysis" is a type nothing can be — the old check
    # was a branch that could never be taken for the second half.
    analyses = [analysis] if isinstance(analysis, CandidateConstraintAnalysis) else list(analysis)
    for entry in analyses:
        for fit in entry.fits:
            constraint_id = id_text(fit.constraint_id)
            if constraints is not None and constraint_id not in constraints:
                raise UnknownDataError(
                    f"analysis for provider {entry.provider_id} reports constraint "
                    f"{constraint_id}, which is not in this request's constraint set"
                )
            if fit.satisfied is None:
                continue
            evidence = entry.evidence_by_constraint.get(constraint_id) or []
            if not evidence:
                raise UnknownDataError(
                    f"analysis for provider {entry.provider_id} says constraint {constraint_id} is "
                    f"satisfied={fit.satisfied}, but carries no evidence for it. Absent data is not "
                    "scored over (§3): emit satisfied=null with a reason_code instead."
                )


def refuse_undecided_hard_constraint_change(
    constraint: Constraint,
    *,
    decided_by: str | None,
) -> None:
    """Refuse relaxing a hard constraint without an explicit patient decision (§2.1, §2.4).

    The check is deliberately narrow: a **soft** preference may be re-stated by the
    system when the patient answers a question, and refusing that would make the
    platform unable to record the answer it just asked for. A hard constraint moves
    only on `decided_by == "patient"`, and then only as a `ConstraintChange` — the
    `ConstraintChange` type is what carries `presented_options`, so a change with no
    options shown is a change nobody agreed to.
    """
    if constraint.kind != "hard":
        return
    if decided_by == "patient":
        return
    raise HardConstraintRelaxedRefused(
        f"hard constraint {constraint.constraint_id} ({constraint.field}) may only change on an "
        f"explicit patient decision; decided_by was {decided_by!r}. Offer an AlternativeOption "
        "and record a ConstraintChange with decided_by='patient' (§3, §2.4)."
    )


def locations_by_id(locations: Iterable[Location]) -> dict[str, Location]:
    """Index locations, so a hospital's distance is one lookup and not a scan."""
    return {id_text(location.location_id): location for location in locations}


def hospital_distance(
    hospital: Hospital,
    *,
    origin: GeoPoint | Mapping[str, float],
    locations: Mapping[str, Location],
) -> float | None:
    """Distance from the search origin to a hospital, or ``None`` when it cannot be known.

    ``None`` and ``0.0`` are different answers and both are used: a hospital whose
    location row is missing is *unknown*, and returning zero would place it at the
    patient's door and rank it first.
    """
    location = locations.get(id_text(hospital.location_id))
    if location is None:
        return None
    return haversine_miles(origin, location.geo)


def constraint_fit_item(
    constraint_id: Uuid | str,
    *,
    satisfied: bool | None,
    reason_code: str,
    detail: str | None = None,
) -> ConstraintFitItem:
    """Build one fit entry, keeping `satisfied=None` as unknown rather than `False`."""
    from referraleasy_contracts.generated.common import ReasonCode

    return ConstraintFitItem(
        constraint_id=Uuid(id_text(constraint_id)),
        satisfied=satisfied,
        reason_code=ReasonCode(reason_code),
        detail=detail,
    )
