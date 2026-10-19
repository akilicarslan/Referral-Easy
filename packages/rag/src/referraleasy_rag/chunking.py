"""Entity-aware chunking of the directory.

The chunking unit is the **entity**, not a fixed window of characters. A sliding window over a
provider record will eventually cut between "Elena Marquez" and "+13125550110", and a chunk that
holds a phone number without the name it belongs to, or a name without any way of reaching it,
cannot ground an answer — it can only produce a confident sentence about the wrong doctor.

So the rules here are:

* Atoms are the smallest unit. An atom is a whole fact (a sentence of prose, a contact block, a
  single field). Atoms are never split.
* The name and the phone number are always in the same atom, therefore always in the same chunk.
* Every other chunk of an entity carries the entity's name in its header, so a retrieved
  fragment is never anonymous.
* Every chunk carries `entity_id`, `source_id`, `knowledge_version` and `updated_at`.

Slots are deliberately **not** chunked. A slot record asserts that a specific appointment is
available; retrieval is not allowed to assert availability, so indexable text is built from
descriptive records only and availability stays with the API that owns it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from referraleasy_rag.contracts import Row
from referraleasy_rag.text import estimate_tokens, normalize_text, split_sentences

#: Bump when the chunker's output changes at all. It is part of the index `run_id`, so a bump
#: retires the previous run's documents on the next index build.
CHUNKER_VERSION = "directory-chunker-1"

#: Target chunk size. Comfortably inside the model's 2048-token maximum sequence length.
MAX_CHUNK_TOKENS = 384

#: The model's hard maximum sequence length (ADR-0006). An atom larger than this cannot be
#: embedded at all, so the chunker refuses to emit it rather than truncating a fact.
HARD_MAX_CHUNK_TOKENS = 2048

ENTITY_TYPES = ("provider", "hospital", "location")

#: Index fields the chunker must never inject through entity metadata.
RESERVED_DOCUMENT_FIELDS = frozenset(
    {
        "chunk_id",
        "entity_id",
        "entity_type",
        "source_id",
        "knowledge_version",
        "updated_at",
        "ordinal",
        "run_id",
        "corpus_version",
        "chunker_version",
        "text",
        "embedding",
    }
)

_LANGUAGE_NAMES = {
    "en": "English",
    "es": "Spanish",
    "tr": "Turkish",
    "pl": "Polish",
    "ar": "Arabic",
    "fr": "French",
    "de": "German",
    "zh": "Chinese",
    "ru": "Russian",
    "ko": "Korean",
    "vi": "Vietnamese",
    "hi": "Hindi",
    "ur": "Urdu",
    "fa": "Persian",
    "pt": "Portuguese",
}

_ACCESSIBILITY_NAMES = {
    "wheelchair_accessible": "wheelchair accessible",
    "accessible_parking": "accessible parking",
    "sign_language_interpreter": "sign language interpreter",
    "hearing_loop": "hearing loop",
    "service_animal_friendly": "service animals welcome",
    "accessible_restroom": "accessible restroom",
}


class ChunkError(RuntimeError):
    """The corpus cannot be chunked without breaking a grounding invariant."""


def _as_text(row: Mapping[str, Any], key: str, default: str = "") -> str:
    value = row.get(key)
    return value if isinstance(value, str) else default


def _as_list(row: Mapping[str, Any], key: str) -> list[str]:
    value = row.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _as_bool(row: Mapping[str, Any], key: str, default: bool = False) -> bool:
    value = row.get(key)
    return value if isinstance(value, bool) else default


def _as_int(row: Mapping[str, Any], key: str) -> int | None:
    value = row.get(key)
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _as_mapping(row: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = row.get(key)
    return value if isinstance(value, dict) else {}


def _coordinate_of(row: Row) -> tuple[float, float] | None:
    geo = _as_mapping(row, "geo")
    latitude, longitude = geo.get("latitude"), geo.get("longitude")
    if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
        return (float(latitude), float(longitude))
    return None


def language_name(code: str) -> str:
    """Human name for an ISO code, falling back to the code itself."""
    return _LANGUAGE_NAMES.get(code, code)


def accessibility_name(code: str) -> str:
    """Readable accessibility feature, falling back to the raw flag."""
    return _ACCESSIBILITY_NAMES.get(code, code.replace("_", " "))


def _join(values: Sequence[str], conjunction: str | None = "and") -> str:
    """Natural join: `a`, `a and b`, `a, b and c`. `conjunction=None` joins with commas only."""
    parts = [value for value in values if value]
    if not parts:
        return ""
    if len(parts) == 1 or conjunction is None:
        return ", ".join(parts)
    return f"{', '.join(parts[:-1])} {conjunction} {parts[-1]}"


@dataclass(frozen=True, slots=True)
class Chunk:
    """One indexable unit of the directory, with the metadata needed to filter and cite it."""

    chunk_id: str
    entity_id: str
    entity_type: str
    source_id: str
    knowledge_version: str
    updated_at: str
    ordinal: int
    text: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def coordinate(self) -> tuple[float, float] | None:
        latitude = self.metadata.get("geo_lat")
        longitude = self.metadata.get("geo_lon")
        if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
            return (float(latitude), float(longitude))
        return None

    @property
    def contact_phone(self) -> str | None:
        phone = self.metadata.get("contact_phone")
        return phone if isinstance(phone, str) and phone else None

    def to_document(
        self,
        *,
        run_id: str,
        corpus_version: str,
        embedding: Sequence[float] | None = None,
    ) -> dict[str, Any]:
        """The exact document shape that is indexed, seeded and read back.

        One shape everywhere is what keeps the seed fallback honest: the JSON rung searches the
        same documents the index holds, so degrading changes where an answer comes from, not
        what it is made of.
        """
        collisions = sorted(set(self.metadata) & RESERVED_DOCUMENT_FIELDS)
        if collisions:
            raise ChunkError(f"metadata may not override document fields: {collisions}")
        document: dict[str, Any] = {
            "chunk_id": self.chunk_id,
            "entity_id": self.entity_id,
            "entity_type": self.entity_type,
            "source_id": self.source_id,
            "knowledge_version": self.knowledge_version,
            "ordinal": self.ordinal,
            "run_id": run_id,
            "corpus_version": corpus_version,
            "chunker_version": CHUNKER_VERSION,
            "text": self.text,
            **self.metadata,
        }
        # A document with no recorded update time omits the field rather than claiming an empty
        # one: the index maps `updated_at` as a date, and "" is not a date.
        if self.updated_at:
            document["updated_at"] = self.updated_at
        if embedding is not None:
            document["embedding"] = [float(value) for value in embedding]
        return document


def _chunk_id(
    entity_type: str, entity_id: str, ordinal: int, knowledge_version: str, text: str
) -> str:
    """Deterministic id: the same corpus produces the same ids, so re-indexing overwrites."""
    digest = hashlib.sha256(
        "\x1f".join(
            [CHUNKER_VERSION, entity_type, entity_id, str(ordinal), knowledge_version, text]
        ).encode("utf-8")
    ).hexdigest()
    return f"{entity_type}:{entity_id}:{ordinal}:{digest[:16]}"


def _pack(
    atoms: Iterable[str],
    *,
    header: str,
    name: str = "",
    max_tokens: int = MAX_CHUNK_TOKENS,
) -> list[str]:
    """Greedily pack whole atoms into chunks, prefixing continuations with the entity header.

    A continuation chunk that does not name its entity is unusable as grounding — "she runs a
    Friday clinic" grounds nothing if the reader cannot tell who "she" is. So a chunk whose atom
    does not already contain the entity's name gets the header prepended instead of being left
    anonymous.
    """
    chunks: list[str] = []
    current = ""
    for atom in atoms:
        text = normalize_text(atom)
        if not text:
            continue
        if estimate_tokens(text) > HARD_MAX_CHUNK_TOKENS:
            raise ChunkError(
                f"atom exceeds the model's maximum sequence length ({HARD_MAX_CHUNK_TOKENS} "
                f"tokens): {text[:80]!r}..."
            )
        labelled = f"{header}: {text}" if _needs_header(text, header, name) else text
        candidate = f"{current} {text}" if current else labelled
        if current and estimate_tokens(candidate) > max_tokens:
            chunks.append(current)
            candidate = labelled
        current = candidate
    if current:
        chunks.append(current)
    return chunks


def _needs_header(text: str, header: str, name: str) -> bool:
    """True when an atom does not already identify the entity it belongs to."""
    if not header:
        return False
    if header.lower() in text.lower():
        return False
    return not (name and name.lower() in text.lower())


def _emit(
    *,
    entity_id: str,
    entity_type: str,
    source_id: str,
    knowledge_version: str,
    updated_at: str,
    texts: Sequence[str],
    metadata: Mapping[str, Any],
) -> list[Chunk]:
    return [
        Chunk(
            chunk_id=_chunk_id(entity_type, entity_id, ordinal, knowledge_version, text),
            entity_id=entity_id,
            entity_type=entity_type,
            source_id=source_id,
            knowledge_version=knowledge_version,
            updated_at=updated_at,
            ordinal=ordinal,
            text=text,
            metadata=dict(metadata),
        )
        for ordinal, text in enumerate(texts)
    ]


def _site_label(hospital: Row, locations: Mapping[str, Row]) -> str:
    name = _as_text(hospital, "name")
    location = locations.get(_as_text(hospital, "location_id"))
    if location is None:
        return name
    city, state = _as_text(location, "city"), _as_text(location, "state")
    place = ", ".join(part for part in (city, state) if part)
    return f"{name} ({place})" if place else name


def chunk_provider(
    provider: Row,
    *,
    hospitals: Mapping[str, Row],
    locations: Mapping[str, Row],
) -> list[Chunk]:
    """Chunk one provider so that name, credentials, specialty and phone stay together."""
    entity_id = _as_text(provider, "provider_id")
    name = _as_text(provider, "display_name")
    if not entity_id or not name:
        raise ChunkError(f"provider record without provider_id/display_name: {provider!r}")
    specialty = _as_text(provider, "specialty")
    credentials = _as_text(provider, "credentials")
    phone = _as_text(provider, "contact_phone")
    source_id = _as_text(_as_mapping(provider, "contact_source"), "source_id")
    knowledge_version = _as_text(provider, "knowledge_version") or "kv-unknown"
    updated_at = _as_text(provider, "updated_at")

    site_rows = [hospitals[hid] for hid in _as_list(provider, "hospital_ids") if hid in hospitals]
    sites = [_site_label(hospital, locations) for hospital in site_rows]
    site_locations = _location_rows(site_rows, locations)
    cities = sorted({_as_text(loc, "city") for loc in site_locations} - {""})
    states = sorted({_as_text(loc, "state") for loc in site_locations} - {""})
    named = f"{name}, {credentials}" if credentials else name

    atoms: list[str] = []
    where = f" at {_join(sites)}" if sites else " listed in the provider directory"
    atoms.append(f"{named} is a {specialty} specialist{where}.")
    if phone:
        atoms.append(
            f"To reach {named}, call {phone}. The number is published in the provider directory "
            f"as {source_id or entity_id}."
        )
    subspecialties = _as_list(provider, "subspecialties")
    if subspecialties:
        atoms.append(f"{name}'s subspecialties: {_join(subspecialties, 'and')}.")
    languages = provider.get("languages")
    if isinstance(languages, list):
        spoken: list[str] = []
        for entry in languages:
            if isinstance(entry, dict):
                code = entry.get("language")
                fluency = entry.get("fluency")
                if isinstance(code, str):
                    label = language_name(code)
                    spoken.append(f"{label} ({fluency})" if isinstance(fluency, str) else label)
        if spoken:
            atoms.append(f"{name} consults in {_join(spoken, 'and')}.")
    atoms.append(
        f"{name} is {'accepting' if _as_bool(provider, 'accepts_new_patients') else 'not accepting'}"
        " new patients."
    )
    features = _as_list(provider, "accessibility_features")
    if features:
        atoms.append(
            f"Accessibility at {name}'s clinic: {_join([accessibility_name(f) for f in features])}."
        )
    first_visit = _as_int(provider, "first_visit_duration_minutes")
    return_visit = _as_int(provider, "return_visit_duration_minutes")
    if first_visit or return_visit:
        atoms.append(
            f"Appointments with {name}: first visit about {first_visit or 'unknown'} minutes, "
            f"follow-up about {return_visit or 'unknown'} minutes."
        )
    plans = _as_list(provider, "accepts_insurance")
    if plans:
        atoms.append(f"{name} accepts these insurance plans: {_join(plans, 'and')}.")
    atoms.extend(split_sentences(_as_text(provider, "profile_text")))

    metadata: dict[str, Any] = {
        "display_name": name,
        "specialty": specialty,
        "specialties": [specialty] if specialty else [],
        "subspecialties": subspecialties,
        "languages": _provider_language_codes(provider),
        "hospital_ids": _as_list(provider, "hospital_ids"),
        "hospital_names": [_as_text(hospital, "name") for hospital in site_rows],
        "cities": cities,
        "states": states,
        "contact_phone": phone,
        "credentials": credentials,
        "accepts_new_patients": _as_bool(provider, "accepts_new_patients"),
        "accessibility_features": features,
        "synthetic": _as_bool(provider, "synthetic", True),
    }
    # A provider has no address of its own in the fixture corpus; its coordinate is the
    # coordinate of its first practice site. Derived, and said so here rather than implied.
    coordinate = None
    if site_rows:
        first_location = locations.get(_as_text(site_rows[0], "location_id"))
        coordinate = _coordinate_of(first_location) if first_location is not None else None
    if coordinate is not None:
        metadata["geo_lat"], metadata["geo_lon"] = coordinate

    texts = _pack(atoms, header=f"{name} ({specialty})" if specialty else name, name=name)
    return _emit(
        entity_id=entity_id,
        entity_type="provider",
        source_id=source_id or f"provider_directory:{entity_id}",
        knowledge_version=knowledge_version,
        updated_at=updated_at,
        texts=texts,
        metadata=metadata,
    )


def _location_rows(rows: Sequence[Row], locations: Mapping[str, Row]) -> list[Row]:
    found: list[Row] = []
    for row in rows:
        location = locations.get(_as_text(row, "location_id"))
        if location is not None:
            found.append(location)
    return found


def _provider_language_codes(provider: Row) -> list[str]:
    languages = provider.get("languages")
    if not isinstance(languages, list):
        return []
    codes: list[str] = []
    for entry in languages:
        if isinstance(entry, dict):
            code = entry.get("language")
            if isinstance(code, str):
                codes.append(code)
    return codes


def chunk_hospital(
    hospital: Row,
    *,
    locations: Mapping[str, Row],
    specialties_by_hospital: Mapping[str, list[str]],
) -> list[Chunk]:
    """Chunk one hospital: identity and address, then the directory blurb, sentence by sentence."""
    entity_id = _as_text(hospital, "hospital_id")
    name = _as_text(hospital, "name")
    if not entity_id or not name:
        raise ChunkError(f"hospital record without hospital_id/name: {hospital!r}")
    location = locations.get(_as_text(hospital, "location_id"))
    knowledge_version = _as_text(hospital, "knowledge_version") or "kv-unknown"
    updated_at = _as_text(hospital, "updated_at")
    specialties = sorted(set(specialties_by_hospital.get(entity_id, [])))
    languages = _as_list(hospital, "languages_supported")
    features = _as_list(hospital, "accessibility_features")

    atoms: list[str] = []
    if location is not None:
        address = _join(
            [_as_text(location, "address_line1"), _as_text(location, "address_line2")],
            conjunction=None,
        )
        locality = _join(
            [
                _as_text(location, "city"),
                " ".join(
                    part
                    for part in (_as_text(location, "state"), _as_text(location, "postal_code"))
                    if part
                ),
            ],
            conjunction=None,
        )
        atoms.append(
            f"{name} is at {address}, {locality} (time zone {_as_text(location, 'timezone')})."
        )
    else:
        atoms.append(f"{name} is listed in the hospital directory.")
    if specialties:
        atoms.append(f"Specialist clinics at {name} cover: {_join(specialties, 'and')}.")
    if languages:
        atoms.append(
            f"{name} supports these languages: {_join([language_name(code) for code in languages])}."
        )
    if features:
        atoms.append(
            f"Accessibility at {name}: {_join([accessibility_name(feature) for feature in features])}."
        )
    atoms.extend(split_sentences(_as_text(hospital, "directory_blurb")))

    metadata: dict[str, Any] = {
        "display_name": name,
        "hospital_ids": [entity_id],
        "hospital_names": [name],
        "specialties": specialties,
        "languages": languages,
        "cities": [_as_text(location, "city")] if location else [],
        "states": [_as_text(location, "state")] if location else [],
        "adapter_type": _as_text(hospital, "adapter_type"),
        "accepts_new_patients": _as_bool(hospital, "accepts_new_patients"),
        "accessibility_features": _as_list(hospital, "accessibility_features"),
        "location_id": _as_text(hospital, "location_id"),
        "synthetic": _as_bool(hospital, "synthetic", True),
    }
    coordinate = _coordinate_of(location) if location is not None else None
    if coordinate is not None:
        metadata["geo_lat"], metadata["geo_lon"] = coordinate

    texts = _pack(atoms, header=name, name=name)
    return _emit(
        entity_id=entity_id,
        entity_type="hospital",
        source_id=f"hospital_directory:{entity_id}",
        knowledge_version=knowledge_version,
        updated_at=updated_at,
        texts=texts,
        metadata=metadata,
    )


def chunk_location(
    location: Row,
    *,
    hospitals: Sequence[Row],
    specialties_by_hospital: Mapping[str, list[str]],
    knowledge_versions: Sequence[str],
) -> list[Chunk]:
    """Chunk one location: the address, and which hospitals and specialties sit at it."""
    entity_id = _as_text(location, "location_id")
    city = _as_text(location, "city")
    state = _as_text(location, "state")
    if not entity_id:
        raise ChunkError(f"location record without location_id: {location!r}")
    label = f"{city}, {state}".strip(", ") or entity_id
    address = _join(
        [_as_text(location, "address_line1"), _as_text(location, "address_line2")], conjunction=None
    )
    postal = _as_text(location, "postal_code")
    name = _as_text(hospitals[0], "name") if hospitals else ""
    header = f"{label} ({name})" if name else label

    atoms: list[str] = [
        f"{address}, {label} {postal}. Time zone {_as_text(location, 'timezone')}.".strip()
    ]
    if hospitals:
        atoms.append(f"Hospitals at {address}: {_join([_as_text(h, 'name') for h in hospitals])}.")
        specialties = sorted(
            {
                specialty
                for hospital in hospitals
                for specialty in specialties_by_hospital.get(_as_text(hospital, "hospital_id"), [])
            }
        )
        if specialties:
            atoms.append(f"Specialties available at this location: {_join(specialties, 'and')}.")

    metadata: dict[str, Any] = {
        "display_name": label,
        "cities": [city] if city else [],
        "states": [state] if state else [],
        "postal_code": postal,
        "hospital_ids": [_as_text(hospital, "hospital_id") for hospital in hospitals],
        "hospital_names": [_as_text(hospital, "name") for hospital in hospitals],
        "specialties": sorted(
            {
                specialty
                for hospital in hospitals
                for specialty in specialties_by_hospital.get(_as_text(hospital, "hospital_id"), [])
            }
        ),
        "synthetic": True,
    }
    coordinate = _coordinate_of(location)
    if coordinate is not None:
        metadata["geo_lat"], metadata["geo_lon"] = coordinate

    texts = _pack(atoms, header=header, name=name)
    return _emit(
        entity_id=entity_id,
        entity_type="location",
        source_id=f"location_directory:{entity_id}",
        knowledge_version="+".join(sorted(set(knowledge_versions))) or "kv-unknown",
        # A location has no freshness of its own; it is as current as the sites standing on it.
        updated_at=max((_as_text(hospital, "updated_at") for hospital in hospitals), default=""),
        texts=texts,
        metadata=metadata,
    )


def chunk_directory(
    providers: Sequence[Row], hospitals: Sequence[Row], locations: Sequence[Row]
) -> list[Chunk]:
    """Chunk the whole directory deterministically (providers, then hospitals, then locations)."""
    hospital_by_id = {_as_text(row, "hospital_id"): row for row in hospitals}
    location_by_id = {_as_text(row, "location_id"): row for row in locations}

    specialties_by_hospital: dict[str, list[str]] = {key: [] for key in hospital_by_id}
    for provider in providers:
        specialty = _as_text(provider, "specialty")
        for hospital_id in _as_list(provider, "hospital_ids"):
            if hospital_id in specialties_by_hospital and specialty:
                specialties_by_hospital[hospital_id].append(specialty)

    hospitals_by_location: dict[str, list[Row]] = {key: [] for key in location_by_id}
    for hospital in hospitals:
        location_id = _as_text(hospital, "location_id")
        if location_id in hospitals_by_location:
            hospitals_by_location[location_id].append(hospital)

    chunks: list[Chunk] = []
    for provider in providers:
        chunks.extend(chunk_provider(provider, hospitals=hospital_by_id, locations=location_by_id))
    for hospital in hospitals:
        chunks.extend(
            chunk_hospital(
                hospital, locations=location_by_id, specialties_by_hospital=specialties_by_hospital
            )
        )
    for location in locations:
        location_id = _as_text(location, "location_id")
        at_location = hospitals_by_location.get(location_id, [])
        chunks.extend(
            chunk_location(
                location,
                hospitals=at_location,
                specialties_by_hospital=specialties_by_hospital,
                knowledge_versions=[
                    _as_text(hospital, "knowledge_version") for hospital in at_location
                ],
            )
        )
    return sorted(chunks, key=lambda chunk: chunk.chunk_id)


def entity_knowledge_versions(chunks: Sequence[Chunk]) -> list[str]:
    """The distinct `knowledge_version` values the corpus is built from, sorted."""
    return sorted({chunk.knowledge_version for chunk in chunks})


def corpus_version(chunks: Sequence[Chunk]) -> str:
    """A deterministic name for "this corpus, as produced by this chunker".

    Derived from the entities' own `knowledge_version` values, never from the clock: re-running
    the indexer over an unchanged corpus must produce the same version, or the idempotency check
    would be comparing two different things. The versions are folded into a short digest — this
    corpus carries 21 of them, and a 400-character run id helps nobody read a log —
    :func:`entity_knowledge_versions` recovers the full list.
    """
    versions = entity_knowledge_versions(chunks)
    if not versions:
        return CHUNKER_VERSION
    digest = hashlib.sha256("+".join(versions).encode("utf-8")).hexdigest()[:12]
    return f"{CHUNKER_VERSION}+{len(versions)}v+{digest}"


def run_id(version: str) -> str:
    """Index run tag. Documents from another run are pruned by the indexer."""
    return version


def validate_name_phone_pairing(chunks: Sequence[Chunk]) -> list[str]:
    """Return every violation of the name <-> phone invariant. Empty means the corpus is sound."""
    problems: list[str] = []
    phones_by_entity: dict[str, str] = {}
    for chunk in chunks:
        phone = chunk.contact_phone
        if phone:
            phones_by_entity[chunk.entity_id] = phone

    for chunk in chunks:
        name = chunk.metadata.get("display_name")
        if not isinstance(name, str) or not name:
            continue
        if chunk.contact_phone and name.lower() not in chunk.text.lower():
            problems.append(
                f"chunk {chunk.chunk_id} carries a phone number without the name it belongs to"
            )

    for entity_id, phone in phones_by_entity.items():
        together = [
            chunk
            for chunk in chunks
            if chunk.entity_id == entity_id
            and phone in chunk.text
            and isinstance(chunk.metadata.get("display_name"), str)
            and str(chunk.metadata["display_name"]).lower() in chunk.text.lower()
        ]
        if not together:
            problems.append(
                f"no chunk holds both the name and the phone number for entity {entity_id}"
            )
    return problems
