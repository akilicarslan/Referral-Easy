"""Hashing, and the two properties that make a manifest worth having.

A release manifest exists so that "what ran" can be answered later. It stops being
able to answer that the moment the same inputs produce two different digests, because
then every rebuild looks like a release and the version-bump gate becomes noise. So
this module provides exactly two guarantees, and both are proven by
``tests/release/test_hashing_determinism.py`` rather than asserted here:

**1. Hashing is content-only, never metadata.** :func:`file_digest` reads bytes.
Nothing looks at ``mtime``, ``size``, inode, or the order a directory listing came
back in. Re-checking out a commit, running ``touch`` over the tree, or moving the
repository to another path leaves every digest identical.

**2. Hashing is order-free.** :func:`digest` canonicalises with
``json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False)`` before
hashing, so a mapping built in a different order — which Python does not promise, and
which a refactor can change without anyone noticing — produces the same bytes.
:func:`tree_digest` sorts its entries for the same reason.

The canonical form is deliberately **the same rule as
``referraleasy_agents.canonical.canonical_json``** and, through it, of
``services/mock-api/src/domain/ids.ts``: sorted keys, no whitespace, non-ASCII left
as-is. There is one spelling of a payload in this project, and a second one that
disagreed by a single space would make two systems compute two hashes of one thing.

What this module deliberately does *not* do is hash a Python object's ``repr``, its
``id``, or the result of ``hash()``. All three are stable within one process and
meaningless across processes, which is the one property a release hash must have.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

__all__ = [
    "CANONICAL_SEPARATORS",
    "canonical_json",
    "digest",
    "file_digest",
    "is_sha256_hex",
    "sha256_hex",
    "text_digest",
    "tree_digest",
]

#: The one canonical spelling. Named, because "did you pass `separators`?" is the
#: difference between one hash and two.
CANONICAL_SEPARATORS: Final[tuple[str, str]] = (",", ":")

_HEX_LENGTH: Final = 64


def canonical_json(payload: Any) -> str:
    """The one spelling of a payload, as JSON text.

    ``default=str`` is the same escape hatch ``referraleasy_agents.canonical`` uses:
    a ``UUID``, a ``RootModel`` or a ``datetime`` reaching a manifest field is a
    fact about the artifact, not a reason to fail the build. It cannot hide a
    ``None`` — that is handled by refusing incomplete manifests, not by formatting.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=CANONICAL_SEPARATORS,
        ensure_ascii=False,
        default=str,
    )


def sha256_hex(data: bytes) -> str:
    """The lowercase hex SHA-256 of ``data``. Every digest in this package is one of these."""
    return hashlib.sha256(data).hexdigest()


def digest(payload: Any) -> str:
    """The SHA-256 of :func:`canonical_json` of ``payload``."""
    return sha256_hex(canonical_json(payload).encode("utf-8"))


def text_digest(text: str) -> str:
    """The SHA-256 of a string's UTF-8 bytes.

    Separate from :func:`digest` on purpose: an instruction is *text*, and hashing it
    as a JSON string would re-escape every quote and newline in it, so the digest
    would answer "is this the same JSON encoding?" rather than "is this the same
    prompt?".
    """
    return sha256_hex(text.encode("utf-8"))


def file_digest(path: Path) -> str:
    """The SHA-256 of a file's bytes.

    The only thing read is the content — see the module docstring. A file that cannot
    be read raises the operating system's error rather than being treated as empty:
    a missing artifact and an empty artifact must not hash alike.
    """
    return sha256_hex(path.read_bytes())


def tree_digest(entries: Mapping[str, Path]) -> str:
    """The digest of a named set of files, keyed by the name each file is known by.

    Sorting happens through :func:`digest`'s canonical form, so the caller may build
    ``entries`` in any order and get the same answer. The *name* is part of the hash,
    which is what makes a renamed file a content change rather than a silent no-op.
    """
    return digest({name: file_digest(path) for name, path in entries.items()})


def is_sha256_hex(value: str) -> bool:
    """Whether ``value`` is a 64-character lowercase hex digest.

    Used by tests and by the manifest's own completeness check: the database refuses a
    ``manifest_hash`` that is not one (``release_registry_manifest_hash_hex``), so a
    producer that could emit anything else would fail at INSERT time — too late to say
    which field was wrong.
    """
    if len(value) != _HEX_LENGTH:
        return False
    return all(character in "0123456789abcdef" for character in value)
