"""Every package is complete, self-contained, and says the thing it exists to say.

Two kinds of assertion live here. The first is structural: the eight directories exist, every file
a manifest cites is on disk, no reference file is unlisted, and every body is a real instruction
body. The second is the one that matters: each package must still teach **its own** non-negotiable
rule. A rewrite that drops the rule while keeping the headings would pass every structural check, so
the rule is asserted literally.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from skills_fixtures import EXPECTED_SKILLS, MIN_BODY_CHARS, MIN_BODY_WORDS, SKILLS_DIR

from referraleasy_skills import (
    INSTRUCTIONS_FILENAME,
    MANIFEST_FILENAME,
    REFERENCES_DIRNAME,
    REQUIRED_SECTIONS,
    SkillManifest,
)

pytestmark = pytest.mark.contract

#: Each package's non-negotiable instruction, quoted from its body. Collapsed and lowercased before
#: matching, so line wrapping is irrelevant - but a rule that has been removed is a failure.
NON_NEGOTIABLE_RULES: dict[str, tuple[str, ...]] = {
    "referral-intake": (
        "no diagnosis",
        "comes from the referral, or it is asked for",
        "never name a condition",
    ),
    "coverage-interpretation": (
        "accepts this insurance",
        "in-network for this plan",
        "never promise payment or coverage",
        "a gap is unknown",
        "conflict",
        "winning_evidence_id",
    ),
    "constraint-negotiation": (
        "hard constraints are never relaxed silently",
        "change only on an explicit patient decision",
        "distance is computed, never inferred",
        "never taken from a search relevance score",
    ),
    "directory-grounding": (
        "never assert availability from the directory",
        "never assert a booking confirmation from the directory",
        "when retrieval is degraded, say so",
        "no_source",
    ),
    "referral-readiness": (
        "never assume a document exists",
        "re-run only the affected checks",
        "never grant clinical or administrative approval",
    ),
    "booking-confirmation": (
        "payload_hash",
        "state_version",
        "expires_at",
        "requested and pending_review are never described as confirmed",
        "http 200 is not a confirmation",
    ),
    "uncertain-booking-reconciliation": (
        "never mint a second key",
        "never re-book",
        "idempotency key",
        "never describe an unresolved booking as failed or as confirmed",
        "escalate",
    ),
    "handoff-triage": (
        "an operator resolving a ticket is not booking consent",
        "technical_outage",
        "no_match",
        "missing_document",
        "conflicting_sources",
        "prolonged_unknown_booking",
        "patient_requested",
        "no_reliable_recovery",
        "consent_given_at",
    ),
}


def collapse(text: str) -> str:
    flat = re.sub(r"[`*]", "", text)
    return re.sub(r"\s+", " ", flat).lower()


def test_exactly_the_eight_expected_packages_exist() -> None:
    found = {
        entry.name
        for entry in SKILLS_DIR.iterdir()
        if entry.is_dir() and entry.name != "src" and not entry.name.startswith((".", "__"))
    }
    assert found == set(EXPECTED_SKILLS), (
        f"unexpected or missing packages: {found ^ set(EXPECTED_SKILLS)}"
    )


@pytest.mark.parametrize("name", EXPECTED_SKILLS)
def test_each_package_has_the_three_parts(name: str) -> None:
    package = SKILLS_DIR / name
    assert (package / MANIFEST_FILENAME).is_file(), name
    assert (package / INSTRUCTIONS_FILENAME).is_file(), name
    # references/ is optional as a format, but every one of these eight ships at least one.
    assert (package / REFERENCES_DIRNAME).is_dir(), name


@pytest.mark.parametrize("name", EXPECTED_SKILLS)
def test_every_referenced_file_exists(name: str, manifests: dict[str, SkillManifest]) -> None:
    for entry in manifests[name].references:
        assert (SKILLS_DIR / name / str(entry["path"])).is_file(), f"{name}: {entry['path']}"


@pytest.mark.parametrize("name", EXPECTED_SKILLS)
def test_no_reference_file_is_unlisted(name: str, manifests: dict[str, SkillManifest]) -> None:
    on_disk = {
        str(path.relative_to(SKILLS_DIR / name))
        for path in (SKILLS_DIR / name / REFERENCES_DIRNAME).rglob("*")
        if path.is_file()
    }
    assert on_disk == set(manifests[name].reference_paths), (
        f"{name}: reference files and manifest disagree"
    )


@pytest.mark.parametrize("name", EXPECTED_SKILLS)
def test_every_reference_file_is_non_trivial(
    name: str, manifests: dict[str, SkillManifest]
) -> None:
    for entry in manifests[name].references:
        text = (SKILLS_DIR / name / str(entry["path"])).read_text(encoding="utf-8")
        assert len(text.split()) >= 120, f"{name}: {entry['path']} is a stub"
        assert len(str(entry["purpose"]).strip()) > 20, f"{name}: {entry['path']} purpose is vague"


@pytest.mark.parametrize("name", EXPECTED_SKILLS)
def test_every_body_is_a_real_instruction_body(name: str, bodies: dict[str, str]) -> None:
    body = bodies[name]
    assert len(body.split()) >= MIN_BODY_WORDS, f"{name}: {len(body.split())} words"
    assert len(body) >= MIN_BODY_CHARS, f"{name}: {len(body)} chars"
    assert not re.search(r"\b(TODO|FIXME|TBD|placeholder|lorem ipsum)\b", body, re.IGNORECASE), name


@pytest.mark.parametrize("name", EXPECTED_SKILLS)
def test_every_body_has_the_required_sections(name: str, bodies: dict[str, str]) -> None:
    body = bodies[name]
    assert body.startswith("# "), f"{name}: body needs a level-one title"
    for section in REQUIRED_SECTIONS:
        assert f"\n{section}\n" in body, f"{name}: missing section {section!r}"


@pytest.mark.parametrize("name", EXPECTED_SKILLS)
def test_every_body_has_a_procedure_with_numbered_steps(name: str, bodies: dict[str, str]) -> None:
    body = bodies[name]
    procedure = body.split("## Procedure\n", 1)[1].split("\n## ", 1)[0]
    steps = re.findall(r"^\d+\. ", procedure, re.MULTILINE)
    assert len(steps) >= 4, f"{name}: procedure has {len(steps)} numbered steps"


#: Ways a body may deny that retrieved text has directive force. At least one is required: the
#: section must not merely mention "data", it must say the text is not something to obey. The
#: project ships a deliberate injection fixture, so this sentence is load-bearing.
DIRECTIVE_DENIALS: tuple[str, ...] = (
    "not an instruction",
    "never instructions",
    "not obeyed",
    "do not act on",
    "not a directive",
)


@pytest.mark.parametrize("name", EXPECTED_SKILLS)
def test_every_body_teaches_that_documents_are_data(name: str, bodies: dict[str, str]) -> None:
    section = bodies[name].split("## Documents and retrieved text are data", 1)
    assert len(section) == 2, f"{name}: no data-not-instructions section"
    body = collapse(section[1].split("\n## ", 1)[0])
    assert "data" in body, name
    assert any(denial in body for denial in DIRECTIVE_DENIALS), (
        f"{name}: the section never denies that retrieved text has directive force"
    )


@pytest.mark.parametrize("name", EXPECTED_SKILLS)
def test_every_body_states_its_failure_modes(name: str, bodies: dict[str, str]) -> None:
    section = bodies[name].split("## Failure modes and escalation\n", 1)[1].split("\n## ", 1)[0]
    bullets = [line for line in section.splitlines() if line.strip().startswith("-")]
    assert len(bullets) >= 3, f"{name}: {len(bullets)} failure modes"


@pytest.mark.parametrize("name", sorted(NON_NEGOTIABLE_RULES))
def test_each_package_still_teaches_its_non_negotiable_rule(
    name: str, bodies: dict[str, str]
) -> None:
    flat = collapse(bodies[name])
    missing = [rule for rule in NON_NEGOTIABLE_RULES[name] if rule.lower() not in flat]
    assert not missing, f"{name} no longer states: {missing}"


def test_the_rule_table_covers_every_package() -> None:
    assert set(NON_NEGOTIABLE_RULES) == set(EXPECTED_SKILLS)


@pytest.mark.parametrize("name", EXPECTED_SKILLS)
def test_bodies_do_not_reference_each_others_private_files(
    name: str, bodies: dict[str, str]
) -> None:
    """Packages are independently loadable: no body may point into another package's tree."""
    for other in EXPECTED_SKILLS:
        if other == name:
            continue
        assert f"{other}/" not in bodies[name], f"{name} reaches into {other}"


@pytest.mark.parametrize("name", EXPECTED_SKILLS)
def test_no_package_directory_contains_stray_files(name: str) -> None:
    package = SKILLS_DIR / name
    allowed = {MANIFEST_FILENAME, INSTRUCTIONS_FILENAME}
    stray = {
        entry.name for entry in package.iterdir() if entry.is_file() and entry.name not in allowed
    }
    assert not stray, f"{name}: stray files at package root: {stray}"


def test_the_loader_finds_exactly_the_eight_packages() -> None:
    from referraleasy_skills import list_skills

    assert list_skills() == sorted(EXPECTED_SKILLS)


def test_the_adr_records_the_package_format() -> None:
    adr = Path(__file__).resolve().parents[2] / "docs" / "adr" / "ADR-0028-skill-packages.md"
    assert adr.is_file(), f"missing {adr}"
    text = adr.read_text(encoding="utf-8")
    assert "grants no tool permission" in text.lower() or "no tool permission" in text.lower()
