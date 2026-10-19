"""What this package refuses, and why each refusal exists.

Every error here is a *stop*, not a warning. §13's manifest is only worth having if
an incomplete one cannot be produced: a manifest that quietly records `null` for a
field reads exactly like one that recorded a value, and the omission is invisible
until a report is written against it. So the rule this module exists to make
possible is:

    **A missing input raises. It is never filled in with a placeholder, and it is
    never omitted.**

Each class names the offending thing, so a failing CI job says which artifact is
absent rather than "manifest build failed".
"""

from __future__ import annotations

__all__ = [
    "IncompleteManifestError",
    "MissingArtifactError",
    "RegistryConflictError",
    "RegistryError",
    "ReleaseRefusedError",
    "VersionLedgerError",
    "VersionNotAdvancedError",
]


class RegistryError(Exception):
    """Base class for every failure this package raises."""


class MissingArtifactError(RegistryError):
    """An artifact the manifest must hash does not exist, or cannot be read.

    Raised instead of hashing an empty list: "no tools" and "the tool table failed to
    import" produce the same digest over nothing, and only one of them is true.
    """


class VersionLedgerError(RegistryError):
    """The version ledger is absent, malformed, incomplete, or disagrees with an artifact.

    The last case is the interesting one. A ledger that declares skill
    ``booking-confirmation`` at ``1.0.0`` while the shipped manifest says ``1.0.1`` is
    two answers to one question, and §13's whole claim is that there is one.
    """


class IncompleteManifestError(RegistryError):
    """A §13 field the manifest is required to carry has no value.

    Raised by :func:`referraleasy_release_registry.manifest.assert_complete` after the
    manifest is built, which is what makes the check exhaustive rather than a
    property of whichever code path happened to run.
    """


class VersionNotAdvancedError(RegistryError):
    """An artifact changed while its version stayed put.

    §13: **CI fails when an affected agent's version did not increase, and even a PATCH
    counts as a behaviour change.** The Turkish sentences, quoted as written, are in
    ADR-0037.
    """


class RegistryConflictError(RegistryError):
    """A ``(component, version)`` is already registered with different content.

    ``release_registry`` enforces this with ``UNIQUE (component, version)``, and the
    component's own comment gives the reason: *"Re-registering the same version with
    different content would mean the artifact moved, which is exactly the event a
    registry must make impossible."* This error is what the writer raises instead of
    ``ON CONFLICT DO NOTHING``, which would turn that refusal into silence.
    """


class ReleaseRefusedError(RegistryError):
    """A pin, a task or a migration was refused.

    The refusals this carries are the §13/§16 ones: a task whose release does not
    match its conversation's pin (mid-conversation version mixing), a kill switch that
    stops a conversation pinned to an older release, and a migration to a new version
    attempted without an explicit approval and checkpoint.
    """
