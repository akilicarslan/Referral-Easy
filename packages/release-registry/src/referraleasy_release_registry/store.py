"""Reading and writing ``release_registry`` — the §13 manifest, where it already belongs.

**No second schema.** ``release_registry`` exists (``migrations/012_release_registry.sql``),
is append-only by trigger for every role including the owner, and carries exactly the
columns §13 needs: the release code, the component, the version, the manifest verbatim,
its hash, the git sha and the build time. This module writes it. Nothing here creates a
table, and nothing here needs one.

How the existing table expresses a release
------------------------------------------
``UNIQUE (component, version)`` is the table's own statement about what a row *is*: one
component at one version, registered once. So a release is **not** one row. It is one row
per deployed component — the seven ``agent-*`` services and the release as a whole — all
carrying the same manifest, because the manifest is the release's document and every row
of it should be able to reconstruct what was deployed. ``UNIQUE (component, version)``
then enforces, structurally and without any help from this module, the rule §13 states:
a version is registered once, and a change to it is a new version rather than an edit.

The per-row ``release_id`` text is therefore ``<release_manifest_id>.<component>``, since
``UNIQUE (release_id)`` means the column cannot be shared by the eight rows of one
release. The release's identity is still the first half of the string, and a query for
"everything in this release" is a prefix match — or better, a join on ``manifest_hash``,
which is what the status view and :func:`manifests_for` use.

Why a runner rather than a connection
-------------------------------------
Same reason ``referraleasy_agent_runtime`` injects its SQL seam: a store that opens its own
connection cannot be tested without a database, and the parts most worth testing here —
"the same version with different content is refused", "an event names a release that
exists", "the manifest read back hashes to the hash recorded beside it" — are precisely
the parts a test wants to exercise without one. :class:`PostgresRunner` is the production
adapter and is duck-typed against a psycopg connection, so this package takes no
dependency on a driver.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol

from referraleasy_contracts.generated.workflow import ReleaseManifest
from referraleasy_release_registry.errors import (
    MissingArtifactError,
    RegistryConflictError,
    ReleaseRefusedError,
)
from referraleasy_release_registry.manifest import manifest_components, manifest_hash
from referraleasy_release_registry.versions import VersionLedger

__all__ = [
    "EVENTS",
    "EVENT_TABLE",
    "REGISTRY_TABLE",
    "STATUS_VIEW",
    "PostgresRunner",
    "RegisteredRelease",
    "RegistrationRow",
    "SqlRunner",
    "read_manifest",
    "record_event",
    "register_release",
    "registration_rows",
    "release_status",
]

REGISTRY_TABLE: Final = "release_registry"
EVENT_TABLE: Final = "release_event"
STATUS_VIEW: Final = "release_status"

#: The vocabulary ``release_event_event_known`` permits. Repeated here so a caller gets a
#: readable refusal naming the four values instead of a database constraint violation —
#: the CHECK is still the authority, this is the error message.
EVENTS: Final[tuple[str, ...]] = ("staged", "activated", "rolled_back", "revoked")

#: The release-as-a-whole component. §13's release is a unit of deployment, and the row
#: under this component is the one that carries the release's own version.
RELEASE_COMPONENT: Final = "referraleasy"


class SqlRunner(Protocol):
    """``(sql, params) -> rows``. The one seam between this package and a database."""

    def __call__(self, sql: str, params: Sequence[Any]) -> Sequence[Mapping[str, Any]]:
        """Execute ``sql`` with ``params`` and return the rows as mappings."""


class PostgresRunner:
    """The production adapter: a psycopg connection, used without importing psycopg.

    Duck-typed deliberately. ``connection.cursor()`` and ``cursor.fetchall()`` are the
    stable part of DB-API 2.0; taking a driver dependency to say so would be taking a
    dependency to write four lines.
    """

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def __call__(self, sql: str, params: Sequence[Any]) -> Sequence[Mapping[str, Any]]:
        with self._connection.cursor() as cursor:
            cursor.execute(sql, params)
            if cursor.description is None:
                return ()
            columns = [column.name for column in cursor.description]
            return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


# ── writing ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RegistrationRow:
    """One row of ``release_registry``, as it will be inserted."""

    release_id: str
    component: str
    version: str
    manifest: Mapping[str, Any]
    manifest_hash: str
    git_sha: str
    built_at: datetime

    def insert_params(self, *, registered_by: str) -> tuple[Any, ...]:
        return (
            self.release_id,
            self.component,
            self.version,
            json.dumps(self.manifest, sort_keys=True, ensure_ascii=False),
            self.manifest_hash,
            self.git_sha,
            self.built_at,
            registered_by,
        )


def registration_rows(
    manifest: ReleaseManifest, ledger: VersionLedger
) -> tuple[RegistrationRow, ...]:
    """The rows one release becomes, in component order.

    ``registered_by`` is deliberately *not* a parameter: it belongs to the act of
    registering rather than to the release, and a release row that said ``ci`` for
    versions a person registered would be a lie in an append-only table.
    """
    document = manifest.model_dump(mode="json")
    hashed = manifest_hash(manifest)
    built_at = manifest.created_at.root.astimezone(UTC)
    rows = [
        RegistrationRow(
            release_id=f"{manifest.release_manifest_id.root}.{component}",
            component=component,
            version=version,
            manifest=document,
            manifest_hash=hashed,
            git_sha=manifest.source.git_sha,
            built_at=built_at,
        )
        for component, version in manifest_components(manifest, release_version=ledger.release)
    ]
    components = sorted(row.component for row in rows)
    if len(set(components)) != len(components):
        raise MissingArtifactError(
            f"two rows of one release claim the same component: {components}. Component names "
            "must be unique or UNIQUE (component, version) would refuse the release."
        )
    return tuple(rows)


def register_release(
    runner: SqlRunner,
    manifest: ReleaseManifest,
    ledger: VersionLedger,
    *,
    registered_by: str,
) -> tuple[str, ...]:
    """Register every row of ``manifest``, idempotently, refusing a content conflict.

    Re-registering a release that is already in the table is a **no-op**, because CI runs
    the same release twice and a registry that failed the second time would be a registry
    people route around. Re-registering a ``(component, version)`` that is already there
    with a *different* hash is a :class:`RegistryConflictError`, and the second case is
    the one worth having: §13's gate exists so that an artifact cannot change without its
    version rising, and a writer that silently overwrote the old row would delete the
    evidence the gate depends on.

    Returns the row ids, in component order.
    """
    if not registered_by.strip():
        raise ReleaseRefusedError("registered_by is required: the table records who registered")
    release_ids: list[str] = []
    for row in registration_rows(manifest, ledger):
        existing = runner(
            f"SELECT manifest_hash FROM {REGISTRY_TABLE} WHERE component = %s AND version = %s",
            (row.component, row.version),
        )
        if existing:
            recorded = str(existing[0]["manifest_hash"])
            if recorded != row.manifest_hash:
                raise RegistryConflictError(
                    f"{row.component} {row.version} is already registered with manifest_hash "
                    f"{recorded}, but this build hashes to {row.manifest_hash}. The artifact "
                    "moved without its version rising, which is the event the registry exists "
                    "to make impossible (§13). Bump the version."
                )
            release_ids.append(row.release_id)
            continue
        runner(
            f"INSERT INTO {REGISTRY_TABLE} "
            "(release_id, component, version, manifest, manifest_hash, git_sha, built_at, "
            "registered_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            row.insert_params(registered_by=registered_by),
        )
        release_ids.append(row.release_id)
    return tuple(release_ids)


def record_event(
    runner: SqlRunner,
    *,
    release_id: str,
    environment: str,
    event: str,
    actor: str,
    note: str | None = None,
) -> None:
    """Append a deployment event. Status is history in this schema, never a column.

    The event names a *row* (``release_event.release_id`` is the registry's uuid primary
    key, not this module's ``release_id`` text), and a release has one row per component —
    so activating a release is one call per component. That is the schema's shape rather
    than a choice made here, and the status view reduces it back to "what is live" with
    ``DISTINCT ON``, which is why nothing above this function has to think about it.
    """
    if event not in EVENTS:
        raise ReleaseRefusedError(
            f"{event!r} is not a release event; the vocabulary is {', '.join(EVENTS)}"
        )
    if not environment.strip() or not actor.strip():
        raise ReleaseRefusedError("a release event names both an environment and an actor")
    found = runner(f"SELECT id FROM {REGISTRY_TABLE} WHERE release_id = %s", (release_id,))
    if not found:
        raise ReleaseRefusedError(
            f"no row in {REGISTRY_TABLE} has release_id {release_id!r}; register the release "
            "before recording what happened to it"
        )
    runner(
        f"INSERT INTO {EVENT_TABLE} (release_id, event, environment, actor, note) "
        "VALUES (%s, %s, %s, %s, %s)",
        (found[0]["id"], event, environment, actor, note),
    )


# ── reading ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RegisteredRelease:
    """One row of ``release_status``: a component, a version, and what happened to it."""

    release_code: str
    component: str
    version: str
    environment: str
    status: str
    status_actor: str
    status_at: datetime
    manifest_hash: str

    @property
    def rolled_back(self) -> bool:
        """Whether the latest event for this row was a rollback or a revocation.

        Both, and the distinction is kept rather than collapsed: a rollback says "this
        release was wrong", a revocation says "this release must not run". Routing treats
        them the same, and a report should not.
        """
        return self.status in {"rolled_back", "revoked"}

    @property
    def active(self) -> bool:
        return self.status == "activated"


def release_status(runner: SqlRunner, *, environment: str) -> tuple[RegisteredRelease, ...]:
    """Every release row's current status in ``environment``, newest first.

    Read from the view, so this module never computes "the latest event" itself. There is
    one definition of a release's status in this system and it is
    ``DISTINCT ON (release_id, environment) ... ORDER BY occurred_at DESC``.
    """
    rows = runner(
        f"SELECT release_code, component, version, environment, status, status_actor, "
        f"status_at, manifest_hash FROM {STATUS_VIEW} WHERE environment = %s "
        f"ORDER BY status_at DESC",
        (environment,),
    )
    return tuple(
        RegisteredRelease(
            release_code=str(row["release_code"]),
            component=str(row["component"]),
            version=str(row["version"]),
            environment=str(row["environment"]),
            status=str(row["status"]),
            status_actor=str(row["status_actor"]),
            status_at=row["status_at"],
            manifest_hash=str(row["manifest_hash"]),
        )
        for row in rows
    )


def read_manifest(
    runner: SqlRunner, *, release_id: str, expected_hash: str | None = None
) -> ReleaseManifest:
    """Read a manifest back, and check it against the hash stored beside it.

    The check is the reason this function exists rather than a bare ``SELECT``. The
    column's own comment promises the manifest is *"stored verbatim so what ran can be
    reconstructed, not paraphrased"*, and a verbatim document whose hash no longer matches
    its own record has been edited by something that got past the trigger. Recomputing
    here turns "the registry is append-only" from a claim about the schema into a claim
    about the bytes.
    """
    rows = runner(
        f"SELECT manifest, manifest_hash FROM {REGISTRY_TABLE} WHERE release_id = %s",
        (release_id,),
    )
    if not rows:
        raise MissingArtifactError(f"no release row {release_id!r} is registered")
    document = rows[0]["manifest"]
    if isinstance(document, str):  # a driver that returns jsonb as text
        document = json.loads(document)
    manifest = ReleaseManifest.model_validate(document)
    recorded = str(rows[0]["manifest_hash"])
    if expected_hash is not None and recorded != expected_hash:
        raise RegistryConflictError(
            f"{release_id} is recorded under manifest_hash {recorded} but the caller expected "
            f"{expected_hash}"
        )
    recomputed = manifest_hash(manifest)
    if recomputed != recorded:
        raise RegistryConflictError(
            f"the stored manifest for {release_id} hashes to {recomputed}, not to the "
            f"{recorded} recorded beside it. The document has been modified after insertion, "
            "which the table's trigger was supposed to make impossible."
        )
    return manifest


def manifests_for(runner: SqlRunner, *, manifest_hash_value: str) -> Mapping[str, ReleaseManifest]:
    """Every distinct component row of the release with this hash, keyed by ``release_id``.

    Selects on ``manifest_hash`` rather than on a prefix of ``release_id``: the hash is
    the release's identity in this table, and a prefix match would silently include a
    release whose id happened to share the first characters.
    """
    rows = runner(
        f"SELECT release_id, manifest FROM {REGISTRY_TABLE} WHERE manifest_hash = %s "
        "ORDER BY component",
        (manifest_hash_value,),
    )
    manifests: dict[str, ReleaseManifest] = {}
    for row in rows:
        document = row["manifest"]
        if isinstance(document, str):
            document = json.loads(document)
        manifests[str(row["release_id"])] = ReleaseManifest.model_validate(document)
    return manifests


def utc_now() -> datetime:
    """The clock, wrapped so a test can pass its own rather than patching ``datetime``."""
    return datetime.now(UTC)
