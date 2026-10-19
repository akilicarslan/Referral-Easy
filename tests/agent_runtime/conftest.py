"""Shared fixtures for the agent-runtime tests.

Everything here keeps the suite offline and deterministic: the clock is virtual,
the environment is an explicit dict (never the developer's real ``.env``), and the
database is a recording double. A test that needs a live model says so with
``@pytest.mark.spike`` and is deselected by the default run.

The doubles themselves live in :mod:`agent_runtime_doubles`, beside this file, and
are imported by name from there. They are deliberately *not* defined here: a bare
``from conftest import ...`` resolves against ``sys.modules["conftest"]`` — whichever
``conftest.py`` pytest imported first in the session — so with four of them in this
repository that name is shared state between test directories, and a test can pass
alone and fail in the full run. See that module's docstring.
"""

from __future__ import annotations

import pytest
from agent_runtime_doubles import SPEC_ENV, FakeClock

from referraleasy_agent_runtime.budget import BudgetCaps


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def caps() -> BudgetCaps:
    return BudgetCaps.from_env(SPEC_ENV)


@pytest.fixture
def env() -> dict[str, str]:
    return dict(SPEC_ENV)
