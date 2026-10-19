"""Writing the manifest into ``release_registry``, and reading it back unchanged (§13).

Two halves, and the first is the one that would otherwise go unnoticed.

**The schema contract.** ``migrations/012_release_registry.sql`` already exists and this
package may not edit it, so the writer is written *against* it. That makes every column name a
coupling between two files that no type checker can see: rename ``manifest_hash`` in the
migration and the Python still imports, still type-checks, and fails at runtime in a code path
only a release runs. ``test_the_writer_agrees_with_the_migration`` reads the migration and
asserts every table, column, constraint and view name the writer depends on, which is why that
test names each one individually instead of asserting "the file parses".

**The behaviour.** A fake runner is enough for the rest and is `not` a mock of the database:
it records the parameters of every call so the assertions are about *what the writer sent* —
eight rows, one per component, each carrying the whole manifest and the hash of the whole
manifest, the version of its own component. The two behaviours that matter are idempotency
(CI registers the same release twice and a registry that refused the second time is one people
route around) and conflict (a ``(component, version)`` already present with a different hash,
which is the event §13's gate exists to make impossible, refused rather than overwritten).

No test here touches the running database. Another agent's integration suite is using it, and
an append-only registry is the wrong table to fill with a test's rows.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

import pytest
from release_fixtures import REPO_ROOT, build_test_manifest

from referraleasy_release_registry.errors import (
    MissingArtifactError,
    RegistryConflictError,
    ReleaseRefusedError,
)
from referraleasy_release_registry.manifest import manifest_hash
from referraleasy_release_registry.store import (
    EVENT_TABLE,
    EVENTS,
    REGISTRY_TABLE,
    STATUS_VIEW,
    RegisteredRelease,
    read_manifest,
    record_event,
    register_release,
    registration_rows,
    release_status,
)
from referraleasy_release_registry.versions import load_ledger

pytestmark = pytest.mark.contract

MIGRATION = REPO_ROOT / "migrations" / "012_release_registry.sql"
LEDGER = load_ledger(REPO_ROOT / "registry" / "versions.json")
REGISTERED_BY = "release-agent@example.test"
REGISTERED_AT = datetime(2026, 10, 15, 8, 0, tzinfo=UTC)


class FakeSql:
    """An in-memory ``release_registry`` and ``release_event``, keyed by the statements sent.

    A double rather than a mock: it keeps real rows, so a second ``register_release`` of the
    same release genuinely finds the first one's hash, and ``release_status`` genuinely reduces
    the events it was given. Dispatching on the statement text is what makes it a stand-in for
    *this* schema and no other, which is the point — a fake that accepted anything would let
    the writer and the migration drift apart.
    """

    def __init__(self) -> None:
        self.registry: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.statements: list[str] = []
        self._next_id = 0

    def __call__(self, sql: str, params: Any) -> list[dict[str, Any]]:
        self.statements.append(" ".join(sql.split()))
        if "INSERT INTO release_registry" in sql:
            (
                release_id,
                component,
                version,
                manifest,
                hashed,
                git_sha,
                built_at,
                registered_by,
            ) = params
            self._next_id += 1
            self.registry.append(
                {
                    "id": f"00000000-0000-4000-8000-{self._next_id:012d}",
                    "release_id": release_id,
                    "component": component,
                    "version": version,
                    "manifest": manifest,
                    "manifest_hash": hashed,
                    "git_sha": git_sha,
                    "built_at": built_at,
                    "registered_at": REGISTERED_AT,
                    "registered_by": registered_by,
                }
            )
            return []
        if (
            "SELECT manifest_hash FROM release_registry WHERE component = %s AND version = %s"
            in sql
        ):
            component, version = params
            return [
                {"manifest_hash": row["manifest_hash"]}
                for row in self.registry
                if row["component"] == component and row["version"] == version
            ]
        if "SELECT id FROM release_registry WHERE release_id = %s" in sql:
            (release_id,) = params
            return [{"id": row["id"]} for row in self.registry if row["release_id"] == release_id]
        if "INSERT INTO release_event" in sql:
            release_id, event, environment, actor, note = params
            self.events.append(
                {
                    "release_id": release_id,
                    "event": event,
                    "environment": environment,
                    "actor": actor,
                    "note": note,
                    "occurred_at": REGISTERED_AT,
                }
            )
            return []
        if "FROM release_status" in sql:
            (environment,) = params
            rows = []
            for release in self.registry:
                latest = [
                    event
                    for event in self.events
                    if event["release_id"] == release["id"] and event["environment"] == environment
                ]
                if not latest:
                    continue
                event = latest[-1]
                rows.append(
                    {
                        "release_code": release["release_id"],
                        "component": release["component"],
                        "version": release["version"],
                        "environment": event["environment"],
                        "status": event["event"],
                        "status_actor": event["actor"],
                        "status_at": event["occurred_at"],
                        "manifest_hash": release["manifest_hash"],
                    }
                )
            return rows
        if "SELECT manifest, manifest_hash FROM release_registry WHERE release_id = %s" in sql:
            (release_id,) = params
            return [
                {"manifest": row["manifest"], "manifest_hash": row["manifest_hash"]}
                for row in self.registry
                if row["release_id"] == release_id
            ]
        if "SELECT release_id, manifest FROM release_registry WHERE manifest_hash = %s" in sql:
            (hashed,) = params
            return [
                {"release_id": row["release_id"], "manifest": row["manifest"]}
                for row in sorted(self.registry, key=lambda row: row["component"])
                if row["manifest_hash"] == hashed
            ]
        raise AssertionError(f"the fake runner has no answer for: {self.statements[-1]}")


@pytest.fixture
def sql() -> FakeSql:
    return FakeSql()


@pytest.fixture(scope="module")
def stored_manifest():
    return build_test_manifest()


@pytest.fixture
def the_rows_and_the_sql(stored_manifest, sql: FakeSql):
    """``(manifest, runner)`` with the release already registered, for the two tests that compare
    the rows the writer *would* send against the rows it *did* send."""
    register_release(sql, stored_manifest, LEDGER, registered_by=REGISTERED_BY)
    return stored_manifest, sql


# ── the schema the writer is written against ───────────────────────────────────────


def _migration_sql() -> str:
    text = MIGRATION.read_text(encoding="utf-8")
    assert text.strip(), f"{MIGRATION} is empty; the writer would have nothing to agree with"
    return text


def test_the_writer_agrees_with_the_migration() -> None:
    """Every name the writer sends over the wire, checked against the file that defines them.

    This is the test that fails when the migration and the Python drift — which they can, in
    one direction, without either file changing shape.
    """
    sql = _migration_sql()
    assert REGISTRY_TABLE == "release_registry" and REGISTRY_TABLE in sql
    assert EVENT_TABLE == "release_event" and EVENT_TABLE in sql
    assert STATUS_VIEW == "release_status" and f"VIEW {STATUS_VIEW}" in sql

    for column in (
        "release_id",
        "component",
        "version",
        "manifest",
        "manifest_hash",
        "git_sha",
        "built_at",
        "registered_by",
    ):
        assert re.search(rf"^\s*{column}\s+\w", sql, re.MULTILINE), column
    for column in ("release_id", "event", "environment", "actor", "note", "occurred_at"):
        assert column in sql, column
    for column in ("release_code", "status_actor", "status_at"):
        assert f"AS {column}" in sql, column

    assert "UNIQUE (component, version)" in sql, (
        "the writer's conflict check assumes the database refuses a second row for one "
        "component version; without the constraint the check would be the only defence"
    )
    assert "UNIQUE (release_id)" in sql, (
        "release_id is text and unique, which is why registration_rows appends the component "
        "to the manifest id: eight rows of one release carry the same manifest id"
    )
    assert "CHECK (manifest_hash ~ '^[0-9a-f]{64}$')" in sql
    assert "manifest_hash" in sql and "registered_by text        NOT NULL" in sql


def test_the_event_vocabulary_is_the_constraint_s_own() -> None:
    """Four values, and the same four in both places.

    The Python list exists so a caller gets a readable refusal instead of a constraint
    violation, and this test is what keeps the readable list from becoming a second, wrong
    vocabulary.
    """
    sql = _migration_sql()
    match = re.search(r"CHECK \(event IN \(([^)]*)\)\)", sql)
    assert match is not None
    declared = tuple(part.strip().strip("'") for part in match.group(1).split(","))
    assert declared == EVENTS == ("staged", "activated", "rolled_back", "revoked")


def test_the_migration_s_refusal_of_a_mutation_is_still_there() -> None:
    """§13's registries are append-only, and ``store.py``'s comments say so.

    If the triggers were dropped, this module's claim that a manifest cannot be edited would
    become a claim about Python that no longer holds.
    """
    sql = _migration_sql()
    assert sql.count("refuse_row_mutation()") >= 4
    assert "BEFORE UPDATE OR DELETE" in sql and "BEFORE TRUNCATE" in sql


# ── register ────────────────────────────────────────────────────────────────────────


def test_a_release_becomes_one_row_per_component(sql: FakeSql, stored_manifest) -> None:
    """Eight rows: §3's seven services plus the release itself."""
    ids = register_release(sql, stored_manifest, LEDGER, registered_by=REGISTERED_BY)
    assert len(ids) == 8
    assert len(sql.registry) == 8
    components = sorted(row["component"] for row in sql.registry)
    assert components == sorted(
        [
            "agent-care_coordinator",
            "agent-referral_intake",
            "agent-coverage_network",
            "agent-provider_matching",
            "agent-referral_readiness",
            "agent-scheduling",
            "agent-recovery_handoff",
            "referraleasy",
        ]
    )
    document = stored_manifest.model_dump(mode="json")
    for row in sql.registry:
        assert row["manifest_hash"] == manifest_hash(stored_manifest)
        assert json.loads(row["manifest"]) == document, (
            "every row stores the whole manifest, so any row can reconstruct the release"
        )
        assert row["git_sha"] == stored_manifest.source.git_sha
        assert row["built_at"] == stored_manifest.created_at.root.astimezone(UTC)
        assert row["registered_by"] == REGISTERED_BY
        assert row["release_id"].startswith(f"{stored_manifest.release_manifest_id.root}.")
    versions = {row["component"]: row["version"] for row in sql.registry}
    assert versions["referraleasy"] == LEDGER.release
    assert versions["agent-scheduling"] == LEDGER.agent("scheduling")


def test_release_ids_are_unique_because_the_manifest_id_is_not(
    sql: FakeSql, stored_manifest
) -> None:
    """``UNIQUE (release_id)`` is on the text column, so eight rows need eight ids."""
    register_release(sql, stored_manifest, LEDGER, registered_by=REGISTERED_BY)
    ids = [row["release_id"] for row in sql.registry]
    assert len(set(ids)) == len(ids) == 8
    assert all(
        row["release_id"].split(".")[0] == stored_manifest.release_manifest_id.root
        for row in sql.registry
    )


def test_registering_the_same_release_twice_changes_nothing(sql: FakeSql, stored_manifest) -> None:
    """CI runs the same release twice, and a registry that failed the second time is one that
    people would route around."""
    first = register_release(sql, stored_manifest, LEDGER, registered_by=REGISTERED_BY)
    second = register_release(sql, stored_manifest, LEDGER, registered_by="ci@example.test")
    assert first == second
    assert len(sql.registry) == 8
    assert sql.registry[0]["registered_by"] == REGISTERED_BY, (
        "the second call is a no-op, and it must not rewrite the first registration's actor"
    )
    inserts = [statement for statement in sql.statements if statement.startswith("INSERT")]
    assert len(inserts) == 8, "the second registration issued no INSERT"


def test_a_component_version_that_moved_is_a_conflict_not_an_overwrite(
    sql: FakeSql, stored_manifest
) -> None:
    """The case the whole registry exists for, refused rather than resolved.

    ``provider-matching`` is registered at ``1.0.0``. The same version now hashes differently —
    the shape of every change §13's gate catches — and the writer must not record a second row
    under that pair, because ``UNIQUE (component, version)`` would refuse it and because
    overwriting would delete the evidence the gate depends on.
    """
    register_release(sql, stored_manifest, LEDGER, registered_by=REGISTERED_BY)
    # The tampering is in the *stored* row, not in the build: this is what a re-run of CI would
    # meet after something changed the artifact without raising the version.
    sql.registry[3]["manifest_hash"] = "f" * 64
    with pytest.raises(RegistryConflictError) as refusal:
        register_release(sql, stored_manifest, LEDGER, registered_by=REGISTERED_BY)
    message = str(refusal.value)
    assert "already registered" in message and "Bump the version" in message
    assert "agent-provider_matching" in message
    assert len(sql.registry) == 8, "the refusal left the table untouched"


def test_an_unnamed_registrar_is_refused(sql: FakeSql, stored_manifest) -> None:
    """An append-only table whose rows do not say who wrote them is a table nobody can audit."""
    with pytest.raises(ReleaseRefusedError) as refusal:
        register_release(sql, stored_manifest, LEDGER, registered_by="   ")
    assert "registered_by" in str(refusal.value)
    assert sql.registry == []


def test_registration_rows_are_the_rows_that_go_in(the_rows_and_the_sql) -> None:
    """The pure half is exposed separately, so a caller can inspect a release before writing."""
    stored_manifest, sql = the_rows_and_the_sql
    rows = registration_rows(stored_manifest, LEDGER)
    assert len(rows) == 8
    assert [(row.component, row.version) for row in rows] == [
        (row["component"], row["version"]) for row in sql.registry
    ]
    params = rows[0].insert_params(registered_by=REGISTERED_BY)
    assert len(params) == 8
    assert json.loads(params[3])["release_manifest_id"] == stored_manifest.release_manifest_id.root
    assert params[7] == REGISTERED_BY


# ── events and status ───────────────────────────────────────────────────────────────


def test_status_is_history_not_a_column(sql: FakeSql, stored_manifest) -> None:
    """A release goes staged, then activated, then rolled back — three rows, one current status.

    This is the mechanism that makes ``canary.rollout_targets`` possible without a rollout
    table: the routing decision reads one view, and the view is a reduction of facts.
    """
    release_id = f"{stored_manifest.release_manifest_id.root}.referraleasy"
    register_release(sql, stored_manifest, LEDGER, registered_by=REGISTERED_BY)
    with pytest.raises(ReleaseRefusedError):
        record_event(sql, release_id=release_id, environment="dev", event="live", actor="ci")
    for event in ("staged", "activated"):
        record_event(
            sql,
            release_id=release_id,
            environment="dev",
            event=event,
            actor="release-agent@example.test",
            note="canary",
        )
    assert [row["event"] for row in sql.events] == ["staged", "activated"]

    statuses = release_status(sql, environment="dev")
    current = next(row for row in statuses if row.component == "referraleasy")
    assert isinstance(current, RegisteredRelease)
    assert current.status == "activated"
    assert current.active and not current.rolled_back
    assert current.status_actor == "release-agent@example.test"
    assert current.status_at == REGISTERED_AT
    assert current.version == LEDGER.release
    assert current.manifest_hash == manifest_hash(stored_manifest)

    record_event(
        sql,
        release_id=release_id,
        environment="dev",
        event="rolled_back",
        actor="on-call@example.test",
    )
    after = next(
        row for row in release_status(sql, environment="dev") if row.component == "referraleasy"
    )
    assert after.rolled_back and not after.active, "the latest event wins"
    assert after.status_actor == "on-call@example.test"


def test_a_revocation_reads_as_a_rollback_to_the_router(sql: FakeSql, stored_manifest) -> None:
    """Two events, one routing consequence, and the difference kept for the report."""
    release_id = f"{stored_manifest.release_manifest_id.root}.referraleasy"
    register_release(sql, stored_manifest, LEDGER, registered_by=REGISTERED_BY)
    record_event(
        sql,
        release_id=release_id,
        environment="dev",
        event="revoked",
        actor="security@example.test",
    )
    row = next(r for r in release_status(sql, environment="dev") if r.component == "referraleasy")
    assert row.status == "revoked"
    assert row.rolled_back, "the router treats a revocation as a rollback"
    assert row.status != "rolled_back", "and a report does not"


def test_an_event_for_an_unregistered_release_is_refused(sql: FakeSql) -> None:
    """``release_event.release_id`` is a foreign key; the refusal says so readably."""
    with pytest.raises(ReleaseRefusedError) as refusal:
        record_event(
            sql,
            release_id="rel-20260101-missing.referraleasy",
            environment="dev",
            event="activated",
            actor="ci",
        )
    assert "register the release before" in str(refusal.value)
    with pytest.raises(ReleaseRefusedError):
        record_event(sql, release_id="", environment="", event="activated", actor="ci")


# ── reading it back ─────────────────────────────────────────────────────────────────


def test_a_manifest_reads_back_exactly(sql: FakeSql, stored_manifest) -> None:
    """Verbatim storage, and the hash checked on the way out rather than trusted."""
    release_id = f"{stored_manifest.release_manifest_id.root}.referraleasy"
    register_release(sql, stored_manifest, LEDGER, registered_by=REGISTERED_BY)
    read_back = read_manifest(
        sql, release_id=release_id, expected_hash=manifest_hash(stored_manifest)
    )
    assert read_back == stored_manifest
    assert read_back.model_dump(mode="json") == stored_manifest.model_dump(mode="json")
    assert read_manifest(sql, release_id=release_id) == stored_manifest
    with pytest.raises(MissingArtifactError):
        read_manifest(sql, release_id="rel-20260101-missing.referraleasy")
    with pytest.raises(RegistryConflictError) as refusal:
        read_manifest(sql, release_id=release_id, expected_hash="a" * 64)
    assert "caller expected" in str(refusal.value)


def test_an_edited_manifest_is_caught_on_the_way_out(sql: FakeSql, stored_manifest) -> None:
    """The trigger is supposed to make this impossible; this is the check that says so.

    Something that got past the trigger — a superuser, a restored dump — is caught because the
    hash is recomputed from the bytes rather than compared to itself.
    """
    release_id = f"{stored_manifest.release_manifest_id.root}.referraleasy"
    register_release(sql, stored_manifest, LEDGER, registered_by=REGISTERED_BY)
    stored = next(
        index for index, row in enumerate(sql.registry) if row["component"] == "referraleasy"
    )
    document = json.loads(sql.registry[stored]["manifest"])
    document["rag"]["knowledge_version"] = "tampered"
    sql.registry[stored]["manifest"] = json.dumps(document)
    with pytest.raises(RegistryConflictError) as refusal:
        read_manifest(sql, release_id=release_id)
    assert "modified after insertion" in str(refusal.value)
    tampered = type(stored_manifest).model_validate(document)
    assert manifest_hash(tampered) != sql.registry[stored]["manifest_hash"], (
        "the document and the hash recorded beside it no longer agree, which is what the "
        "recomputation detects"
    )
    sql.registry[stored]["manifest"] = json.dumps(stored_manifest.model_dump(mode="json"))
    assert read_manifest(sql, release_id=release_id) == stored_manifest, (
        "and the refusal was about the bytes, not a cache"
    )


def test_a_manifest_hash_finds_every_component_row(sql: FakeSql, stored_manifest) -> None:
    """``manifests_for`` selects on the hash, not on an id prefix: a prefix match would
    silently include another release whose id shared its first characters."""
    from referraleasy_release_registry.store import manifests_for

    register_release(sql, stored_manifest, LEDGER, registered_by=REGISTERED_BY)
    found = manifests_for(sql, manifest_hash_value=manifest_hash(stored_manifest))
    assert len(found) == 8
    assert set(found) == {row["release_id"] for row in sql.registry}
    assert all(manifest == stored_manifest for manifest in found.values())
    assert manifests_for(sql, manifest_hash_value="0" * 64) == {}
