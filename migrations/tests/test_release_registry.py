"""Release registry immutability.

A registry that can be edited records what somebody most recently wanted to
believe was deployed. "No UPDATE path" is therefore enforced structurally: a
trigger refuses the write for every role, and status changes are new rows in an
append-only event log rather than an UPDATE of a column.
"""

from __future__ import annotations

import pytest
from migration_fixtures import SHA_A, assert_rejects

pytestmark = pytest.mark.integration


def register(
    conn,
    *,
    release_id: str = "rel-1",
    component: str = "gateway",
    version: str = "1.4.0",
    manifest: str = '{"image": "gateway:1.4.0"}',
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO release_registry (release_id, component, version, manifest,"
            " manifest_hash, git_sha, built_at, registered_by)"
            " VALUES (%s, %s, %s, %s, %s, 'abc123', now(), 'ci') RETURNING id",
            (release_id, component, version, manifest, SHA_A),
        )
        return str(cur.fetchone()["id"])


# ---------------------------------------------------------------------------
# No UPDATE path
# ---------------------------------------------------------------------------


def test_the_release_manifest_cannot_be_updated(db):
    """The headline test: the manifest of a registered release is final."""
    register(db)

    assert_rejects(
        db,
        'UPDATE release_registry SET manifest = \'{"image": "gateway:evil"}\''
        " WHERE release_id = 'rel-1'",
        (),
        sqlstate="RE008",
        match="append-only",
    )


def test_the_manifest_hash_cannot_be_updated(db):
    register(db)

    assert_rejects(
        db,
        "UPDATE release_registry SET manifest_hash = %s WHERE release_id = 'rel-1'",
        ("f" * 64,),
        sqlstate="RE008",
    )


def test_a_release_cannot_be_deleted(db):
    register(db)

    assert_rejects(
        db,
        "DELETE FROM release_registry WHERE release_id = 'rel-1'",
        (),
        sqlstate="RE008",
        match="append-only",
    )


def test_the_registry_cannot_be_truncated(db):
    """A row-level trigger never sees TRUNCATE, so this needs its own guard."""
    register(db)

    assert_rejects(db, "TRUNCATE release_registry CASCADE", (), sqlstate="RE008")


def test_a_component_version_is_registered_once(db):
    """Re-registering a version with different content would mean the artifact
    moved, which is exactly what a registry must make impossible."""
    register(db)

    assert_rejects(
        db,
        "INSERT INTO release_registry (release_id, component, version, manifest,"
        " manifest_hash, built_at, registered_by)"
        " VALUES ('rel-2', 'gateway', '1.4.0', '{}', %s, now(), 'ci')",
        ("b" * 64,),
        sqlstate="23505",
        match="release_registry_component_version_key",
    )


def test_a_manifest_cannot_claim_to_predate_its_registration(db):
    assert_rejects(
        db,
        "INSERT INTO release_registry (release_id, component, version, manifest,"
        " manifest_hash, built_at, registered_by)"
        " VALUES ('rel-3', 'gateway', '9.9.9', '{}', %s, now() + interval '1 hour', 'ci')",
        (SHA_A,),
        sqlstate="23514",
        match="release_registry_built_before_registration",
    )


# ---------------------------------------------------------------------------
# Status is derived, not stored
# ---------------------------------------------------------------------------


def test_status_comes_from_the_event_log(db):
    release_pk = register(db)

    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO release_event (release_id, event, environment, actor)"
            " VALUES (%s, 'staged', 'production', 'ci')",
            (release_pk,),
        )
        cur.execute(
            "INSERT INTO release_event (release_id, event, environment, actor)"
            " VALUES (%s, 'activated', 'production', 'release-bot')",
            (release_pk,),
        )
        cur.execute(
            "SELECT status, status_actor FROM release_status WHERE release_pk = %s", (release_pk,)
        )
        row = cur.fetchone()

    assert row["status"] == "activated", "the latest event wins"
    assert row["status_actor"] == "release-bot"


def test_events_are_append_only(db):
    release_pk = register(db)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO release_event (release_id, event, environment, actor)"
            " VALUES (%s, 'staged', 'production', 'ci') RETURNING id",
            (release_pk,),
        )
        event_id = cur.fetchone()["id"]

    assert_rejects(
        db,
        "UPDATE release_event SET event = 'revoked' WHERE id = %s",
        (event_id,),
        sqlstate="RE008",
        match="append-only",
    )


def test_an_unknown_event_type_is_refused(db):
    release_pk = register(db)

    assert_rejects(
        db,
        "INSERT INTO release_event (release_id, event, environment, actor)"
        " VALUES (%s, 'deleted_everything', 'production', 'ci')",
        (release_pk,),
        sqlstate="23514",
        match="release_event_event_known",
    )
