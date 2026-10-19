"""Where this service opens a PostgreSQL connection, and the only place.

Two modules talk to the database — `control` (the control-state port, §7) and `approval` (the consent
ledger, §9) — and they must not each grow a connection convention. What they need is small and
identical: a connection, closed when the operation is done, and a driver refusal turned into its
typed counterpart.

**One connection per operation, opened and closed around the statements that make it up.** §7 forbids
holding a transaction open across a workflow, and every operation in this service is one statement or
a short series of them that commits together and ends (`control.record_booking` with a transition is
the longest; that module says why it is a series rather than a single statement). There is no pool
because the workspace has no pool dependency, and inventing a half-pool here — a shared connection
behind a lock, or a hand-rolled checkout — would trade a visible cost (a connect per request, on a
local socket or a unix socket) for an invisible one: a stale connection that fails once, under load,
in the one service that has no retry. When a pool is worth having, it is worth `psycopg_pool` and its
own measurement, not a dict of connections in a context manager.

**`autocommit=True`, and callers that need two statements wrap them in an explicit transaction.**
Leaving the default alone would make the commit depend on where a context manager happens to exit,
which is the kind of subtlety that works until it does not.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

__all__ = ["session", "translated_refusal"]


@contextmanager
def translated_refusal() -> Iterator[None]:
    """Turn a database refusal into its typed counterpart; leave everything else alone.

    Follows `referraleasy_domain.workflow._translated` rather than importing it, because that helper
    is private to the package and reachable only through the store methods it wraps. The rule is the
    same one: a driver error carrying a SQLSTATE is the database refusing, and a Python-level bug
    carries none, so a typo can never masquerade as a `RE00x` refusal.
    """
    try:
        yield
    except Exception as exc:
        if any(getattr(exc, attribute, None) for attribute in ("sqlstate", "pgcode")):
            from referraleasy_domain import translate

            raise translate(exc) from exc
        raise


@contextmanager
def session(dsn: str) -> Iterator[Any]:
    """One connection, closed when the operation is done, refused or not.

    The driver is imported here rather than at module scope so that importing this module — which
    `control` and `approval` both do — never requires psycopg to be installed. The memory profile
    must run on a checkout with no database driver in it, and that is a property of the imports, not
    of a promise.
    """
    import psycopg

    connection = psycopg.connect(dsn, autocommit=True)
    try:
        with translated_refusal():
            yield connection
    finally:
        connection.close()
