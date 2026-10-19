"""Connections for the worker processes (§7).

A worker owns its connections. There is no shared pool in this package, and that is a decision rather
than an omission: §7's whole ownership story assumes a worker can *lose* the right to write (its
lease expires, a human takes the workflow, another replica claims it), so the connection a worker
holds must not be the thing that makes it look authoritative. Every write in this package is guarded
by a compare-and-set in SQL, and every one of them can come back empty.

Two consequences the modules above rely on:

* **Transactions are short and the caller's.** The stores take a connection, never a transaction, and
  a claim's row lock must not outlive the statement that made it (see the domain's `workflow.py`).
  :func:`connection` opens a connection, commits on a clean exit, and rolls back on an exception —
  it never spans a network call.
* **The driver is imported lazily.** `psycopg` is imported inside :func:`connect` so that importing
  this package — and therefore the offline unit tests, and `--help` — needs no database driver
  installed. The domain package made the same call for the same reason.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from referraleasy_agent_runtime import ConfigurationError
from referraleasy_domain.workflow import (
    ConnectionLike,
    execute_function,
    fetch_all,
    fetch_one,
    fetch_scalar,
    required_row,
)
from referraleasy_workers.settings import WorkerSettings

__all__ = [
    "ConnectionLike",
    "connect",
    "connection",
    "execute_function",
    "fetch_all",
    "fetch_one",
    "fetch_scalar",
    "required_row",
]


def connect(database_url: str) -> Any:
    """Open one autocommit-off connection.

    Not autocommit on purpose: the guarded writes in this package are compare-and-sets whose *whole
    point* is that a failed one leaves no trace, and a connection that committed every statement
    individually would make a half-applied step possible.
    """
    try:
        import psycopg
    except ModuleNotFoundError as exc:  # pragma: no cover - environment dependent
        raise ConfigurationError(
            "the workers need psycopg (v3) to open a control-state connection; install "
            "`psycopg[binary]` (it is a dependency of this package) or run with a connection "
            "supplied by the caller"
        ) from exc
    return psycopg.connect(database_url)


@contextmanager
def connection(settings: WorkerSettings) -> Iterator[Any]:
    """A connection for one unit of work, committed on success and rolled back otherwise."""
    conn = connect(settings.require_database_url())
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
