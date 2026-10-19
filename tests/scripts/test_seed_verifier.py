"""The checks `make seed` runs against the mock hospital API.

These are the half of `make seed` that answers "is the corpus actually loaded", and the failures they
exist to catch are all *silent*: a service on its built-in dataset, a store that has drifted from the
fixtures, a loader report that no longer describes the files. Each one is tested by constructing the
bad state and asserting the check reports it — a check nobody has seen fail is a guess, and a check
that catches a missing row is worth nothing if it only ever runs against complete data.

No service and no database: the stub in `scripts_fixtures.py` serves the real fixture files verbatim,
and the seed report is built from those same files. What that proves is that the comparison is right.
What it does not prove is that the real service answers as the stub does; the change's own evidence
is the live run, and `services/mock-api/test/fixtures-integration.test.ts` owns the service's
behaviour.
"""

from __future__ import annotations

import json

import pytest
import scripts_fixtures as fx

seed = fx.seed_module()


# ---------------------------------------------------------------------------
# "mock API serves the fixtures"
# ---------------------------------------------------------------------------


def test_serving_the_fixtures_passes() -> None:
    check = seed.check_mock_api_seeded_from_fixtures({"seed": fx.seed_report()}, fx.FIXTURES_DIR)
    assert check.ok, check.detail
    assert "fixtures" in check.detail


def test_serving_the_builtin_dataset_is_reported() -> None:
    """`builtin` is the failure this check exists for.

    The built-in dataset is self-consistent, answers every request, and shares no id with
    `fixtures/scenarios.json`, so the demo would run end to end while every scenario referenced a
    hospital that does not exist. Nothing else in the repository notices that.
    """
    health = {"seed": {**fx.seed_report(), "mode": "builtin"}}
    check = seed.check_mock_api_seeded_from_fixtures(health, fx.FIXTURES_DIR)
    assert not check.ok
    assert "builtin" in check.detail


def test_serving_a_different_fixture_directory_is_reported() -> None:
    """The service can be told to serve someone else's fixtures, which is a different corpus."""
    health = {"seed": {**fx.seed_report(), "source_directory": "/tmp/not-the-fixtures"}}
    check = seed.check_mock_api_seeded_from_fixtures(health, fx.FIXTURES_DIR)
    assert not check.ok
    assert "source_directory" in check.detail


def test_a_health_payload_with_no_seed_block_is_a_failure_not_a_pass() -> None:
    """A service that reports nothing about its dataset has not been verified, so it cannot pass."""
    with pytest.raises(seed.SeedFailure):
        seed.check_mock_api_seeded_from_fixtures({"status": "ok"}, fx.FIXTURES_DIR)


# ---------------------------------------------------------------------------
# "seed report == fixture files"
# ---------------------------------------------------------------------------


def test_the_seed_report_matching_the_files_passes() -> None:
    check = seed.check_seed_report_matches_fixtures({"seed": fx.seed_report()})
    assert check.ok, check.detail


@pytest.mark.parametrize(
    ("field", "wrong"),
    [("hospitals", 6), ("slots", 34), ("evidence", 49)],
)
def test_a_seed_report_that_disagrees_with_a_file_is_reported(field: str, wrong: int) -> None:
    """Each count is compared to the file, so a count nobody checked cannot hide behind the others."""
    health = {"seed": {**fx.seed_report(), field: wrong}}
    check = seed.check_seed_report_matches_fixtures(health)
    assert not check.ok
    assert field in check.detail


def test_the_seed_report_covers_every_fixture_file_the_mock_api_owns() -> None:
    """Anti-vacuity for the check above.

    A mapping that listed one file would pass on any service, so the number of files compared is
    asserted against the stub's independent list of the same service contract.
    """
    assert len(seed.SEED_REPORT_FIXTURES) == len(fx.SEED_REPORT_FIELDS) == 10
    assert dict(seed.SEED_REPORT_FIXTURES) == dict(fx.SEED_REPORT_FIELDS)


# ---------------------------------------------------------------------------
# "store == fixture ids"
# ---------------------------------------------------------------------------


def test_a_store_holding_every_fixture_record_passes() -> None:
    with fx.fake_mock_api() as url:
        check = seed.check_store_matches_fixtures(url)
    assert check.ok, check.detail


def test_a_missing_record_is_reported_and_named() -> None:
    """The case the loader's own report cannot see.

    `seedFromFixtures` returns the counts it *read*, not the counts it wrote, and it writes slots
    with `INSERT OR IGNORE` so that a restart cannot resurrect a slot a live booking holds. A slot
    deleted from the fixtures therefore stays in the store — and a store one record short is a demo
    whose numbers are wrong with nothing detecting it.
    """
    missing = fx.fixture_records("directory/hospitals.json")[0]["hospital_id"]
    with fx.fake_mock_api(fx.StubConfig(drop_ids={missing})) as url:
        check = seed.check_store_matches_fixtures(url)
    assert not check.ok
    assert "hospitals" in check.detail
    assert missing in check.detail


def test_a_record_the_fixtures_do_not_contain_is_reported() -> None:
    """The other direction: a stale row the corpus no longer describes."""
    extra = {"hospital_id": "20000000-0000-4000-8000-00000000dead", "name": "Not In Any Fixture"}
    with fx.fake_mock_api(fx.StubConfig(extra_records={"/hospitals": [extra]})) as url:
        check = seed.check_store_matches_fixtures(url)
    assert not check.ok
    assert extra["hospital_id"] in check.detail


def test_documents_are_counted_through_their_required_filter() -> None:
    """`GET /documents` refuses to answer unfiltered, so the requests come from the fixture itself.

    If the grouping were wrong the route would serve nothing and this check would report ten missing
    ids — which is why it is asserted rather than assumed that the grouped route is exercised.
    """
    documents = fx.fixture_records("documents/documents.json")
    assert {record["patient_id"] for record in documents}, "no patient_id to group by"

    missing = documents[0]["document_id"]
    with fx.fake_mock_api(fx.StubConfig(drop_ids={missing})) as url:
        check = seed.check_store_matches_fixtures(url)
    assert not check.ok, "a dropped document was not noticed, so the filtered route served nothing"
    assert missing in check.detail


def test_an_unreachable_service_is_a_failure_not_a_pass() -> None:
    """No service means the corpus is not loaded, and saying "OK" would be the lie this replaces."""
    with pytest.raises(seed.SeedFailure):
        seed.check_store_matches_fixtures("http://127.0.0.1:1")


# ---------------------------------------------------------------------------
# "mock API corpus coherent"
# ---------------------------------------------------------------------------


def _coherence(**overrides: object):
    health = {
        "fixture_load_errors": [],
        "scenario_manifest": {
            "found": True,
            "complete": True,
            "missing_files": [],
            "unreferenced_files": [],
        },
    }
    health.update(overrides)
    return health


def test_a_clean_corpus_report_passes() -> None:
    check = seed.check_service_coherence(_coherence())
    assert check.ok, check.detail


def test_a_fixture_load_error_is_reported() -> None:
    check = seed.check_service_coherence(_coherence(fixture_load_errors=["fault-scenarios/x.json"]))
    assert not check.ok
    assert "fault-scenarios/x.json" in check.detail


def test_an_incomplete_scenario_manifest_is_reported() -> None:
    check = seed.check_service_coherence(
        _coherence(
            scenario_manifest={
                "found": True,
                "complete": False,
                "missing_files": ["patients/patient_details.json"],
                "unreferenced_files": [],
            }
        )
    )
    assert not check.ok
    assert "patient_details.json" in check.detail


def test_an_unreferenced_fixture_file_is_reported() -> None:
    """A file no scenario names is either dead or a manifest that has not been updated, and both
    are worth knowing about before the demo rather than during it."""
    check = seed.check_service_coherence(
        _coherence(
            scenario_manifest={
                "found": True,
                "complete": True,
                "missing_files": [],
                "unreferenced_files": ["patients/missing_fields.json"],
            }
        )
    )
    assert not check.ok
    assert "missing_fields.json" in check.detail


# ---------------------------------------------------------------------------
# Reading the fixtures
# ---------------------------------------------------------------------------


def test_a_fixture_file_that_is_not_there_is_a_loud_failure() -> None:
    with pytest.raises(seed.SeedFailure, match="missing fixture file"):
        seed.read_fixture_records("patients/not_a_file.json")


def test_a_fixture_record_without_its_id_is_a_loud_failure(tmp_path, monkeypatch) -> None:
    """A record with no id would drop out of the comparison and read as a match.

    That is the quiet drop the fixture contract forbids, so it is made impossible rather than
    tolerated: the check refuses to run instead of comparing a smaller set and passing.
    """
    monkeypatch.setattr(seed, "FIXTURES_DIR", tmp_path)
    (tmp_path / "broken.json").write_text(json.dumps([{"name": "no id here"}]), encoding="utf-8")
    with pytest.raises(seed.SeedFailure, match="document_id"):
        seed.fixture_ids("broken.json", "document_id")


def test_a_duplicated_fixture_id_is_a_loud_failure(tmp_path, monkeypatch) -> None:
    """Two records with one id would compare as one record, hiding the second."""
    monkeypatch.setattr(seed, "FIXTURES_DIR", tmp_path)
    (tmp_path / "dup.json").write_text(
        json.dumps([{"document_id": "a1"}, {"document_id": "a1"}]), encoding="utf-8"
    )
    with pytest.raises(seed.SeedFailure, match="repeats"):
        seed.fixture_ids("dup.json", "document_id")


def test_every_list_probe_names_a_fixture_file_that_exists() -> None:
    """Anti-vacuity: a probe pointing at a file that is not there proves nothing about the store."""
    for probe in seed.LIST_PROBES:
        assert (fx.FIXTURES_DIR / probe.fixture).is_file(), probe.fixture
