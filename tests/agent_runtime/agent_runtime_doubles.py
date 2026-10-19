"""Offline doubles shared by the agent-runtime tests.

They live in a *uniquely named* module, not in ``conftest.py``, and that is a fix
rather than a preference. A bare ``from conftest import X`` does not resolve
against the importing file's own directory: it resolves against
``sys.modules["conftest"]``, which pytest fills with the **first** ``conftest.py``
it imports in the session. This repository has four of them (``tests/agent_runtime``,
``tests/domain``, ``tests/rag``, ``tests/skills``), so the name is shared state
between test directories.

The failure mode is nasty: ``tests/agent_runtime/test_roles_and_tools.py`` passed
when its directory was collected alone and failed in the full-suite run, because
``tests/skills/conftest.py`` had won the name by then — and what broke was an
import *inside a test function*, which is resolved at call time rather than at
module import. A green run has to mean the tests actually ran, so nothing here is
reachable through a name another file could own.

:data:`SPEC_ENV` is the other half of the same idea: every test states its
environment explicitly instead of reading the developer's real ``.env``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

__all__ = ["SPEC_ENV", "FakeClock", "RecordingPolicy", "RecordingSkillSource"]

#: The spec's numbers, as `.env.example` states them. Used both as the fixture default
#: and as the expected value, so a drift in either place fails a test.
SPEC_ENV: dict[str, str] = {
    "APP_ENV": "test",
    "AGENT_ATTEMPT_TIMEOUT_MS": "3000",
    "AGENT_MAX_ATTEMPTS": "3",
    "TURN_DEADLINE_MS": "30000",
    "MAX_DELEGATIONS_PER_TURN": "8",
    "MAX_MODEL_CALLS_PER_TURN": "12",
    "MAX_TOOL_CALLS_PER_TURN": "16",
    "HTTP_TOOL_TIMEOUT_MS": "1500",
    "FALLBACK_TIMEOUT_MS": "3000",
    "RETRY_BACKOFF_BASE_MS": "150",
    "RETRY_BACKOFF_MAX_MS": "500",
    "AGENT_MODEL_PROVIDER": "openrouter",
    "AGENT_MODEL_ID": "anthropic/claude-haiku-5.5",
    "OPENROUTER_API_KEY": "test-key-not-a-real-credential",
    "A2A_SERVICE_TOKEN": "test-service-token",
}


class FakeClock:
    """A clock the test drives. No test in this suite sleeps for real."""

    def __init__(self, start_ms: int = 1_000_000) -> None:
        self._now_ms = start_ms
        self.slept_ms: list[int] = []

    def monotonic_ms(self) -> int:
        return self._now_ms

    async def sleep_ms(self, milliseconds: int) -> None:
        self.slept_ms.append(milliseconds)
        self._now_ms += milliseconds

    def advance(self, milliseconds: int) -> None:
        self._now_ms += milliseconds


class RecordingPolicy:
    """A stand-in for the database's ``policy_assert(role, tool, state)``."""

    def __init__(
        self,
        *,
        allowed: bool = True,
        error: BaseException | None = None,
    ) -> None:
        self.allowed = allowed
        self.error = error
        self.calls: list[tuple[str, str, str]] = []

    def policy_assert(self, role: str, tool: str, workflow_state: str) -> None:
        self.calls.append((role, tool, workflow_state))
        if self.error is not None:
            raise self.error
        if not self.allowed:
            from referraleasy_domain import PolicyDeniedError

            raise PolicyDeniedError(
                f"policy denies role {role} calling tool {tool} in workflow state {workflow_state}"
            )


class RecordingSkillSource:
    """A skill source that counts every manifest and body read."""

    def __init__(self, manifests: Mapping[str, Mapping[str, Any]]) -> None:
        self._manifests = dict(manifests)
        self.manifest_reads: list[str] = []
        self.body_reads: list[str] = []

    def list_skills(self) -> Sequence[str]:
        return tuple(self._manifests)

    def load_manifest(self, name: str) -> Mapping[str, Any]:
        self.manifest_reads.append(name)
        return self._manifests[name]

    def load_body(self, name: str) -> str:
        self.body_reads.append(name)
        return f"# {name}\ninstructions for {name}"
