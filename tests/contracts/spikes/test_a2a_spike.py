"""Spike S-03: A2A Agent Card served, client round trip, and the auth header.

ADR-0003 records two things this file measures rather than asserts:

* the **path** the agent card is served from (``/.well-known/...`` is the convention,
  but it was not verified in the SDK source), and
* the **JSON-RPC method name** in a2a-sdk v1.2.2 (the v0.3 -> v1.0 rewrite renamed
  enums and deleted the wrapper applications; method names were not checked).

It also asserts the v1.0 Agent Card shape and the task lifecycle state names, both of
which *were* taken from the SDK's own v1.0 migration notes.

Requires A2A_BASE_URL and A2A_SERVICE_TOKEN. Run with ``make spike``.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from _spike_env import require_env, require_module

pytestmark = pytest.mark.spike

ADR = "ADR-0003-a2a-exposure-with-a2a-sdk.md"

# v1.0 renamed every TaskState member to proto style; the lowercase v0.3 spellings are gone.
_EXPECTED_TASK_STATES = {
    "TASK_STATE_UNSPECIFIED",
    "TASK_STATE_SUBMITTED",
    "TASK_STATE_WORKING",
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_INPUT_REQUIRED",
    "TASK_STATE_AUTH_REQUIRED",
    "TASK_STATE_REJECTED",
}

_CARD_PATHS = (
    "/.well-known/agent-card.json",
    "/.well-known/agent.json",
    "/a2a/agent-card",
    "/agent-card.json",
)


def _requirements() -> tuple[str, str]:
    base_url = require_env(
        "A2A_BASE_URL",
        purpose="the base URL of a running A2A agent service this spike talks to",
        where="start an agent from services/agents/*; there is no A2A_BASE_URL in "
        ".env.example yet - it is a spike-only variable, so export it for this run",
        adr=ADR,
    ).rstrip("/")
    token = require_env(
        "A2A_SERVICE_TOKEN",
        purpose="the bearer token the A2A route prefix must verify",
        where=".env.example ships CHANGEME_LOCAL_FIXTURE_TOKEN - set a real local value in "
        ".env ([ADR-0001] local-only fixture; the Cloud Run profile replaces it with "
        "audience-checked service identity)",
        adr=ADR,
    )
    return base_url, token


def _httpx() -> Any:
    return require_module("httpx", distribution="httpx", adr=ADR)


def _timeout() -> float:
    return float(os.environ.get("SPIKE_HTTP_TIMEOUT_S", "30"))


def test_agent_card_is_served_in_v1_shape() -> None:
    """Find the card route, then assert the v1.0 shape rather than the v0.3 shape."""
    base_url, _token = _requirements()
    httpx = _httpx()

    attempts: dict[str, int] = {}
    card: dict[str, Any] | None = None
    with httpx.Client(timeout=_timeout()) as client:
        for path in _CARD_PATHS:
            try:
                response = client.get(f"{base_url}{path}")
            except Exception as exc:
                pytest.fail(
                    "A2A SERVICE NOT REACHABLE.\n\n"
                    f"GET {base_url}{path}\n"
                    f"Error: {type(exc).__name__}: {exc}\n\n"
                    "Export A2A_BASE_URL to a running agent service and re-run.",
                    pytrace=False,
                )
            attempts[path] = response.status_code
            if response.status_code == 200:
                card = response.json()
                break

    if card is None:
        pytest.fail(
            f"NO AGENT CARD FOUND at {base_url}.\n\n"
            f"Tried (status codes): {attempts}\n\n"
            "The card route is created by `create_agent_card_routes(agent_card)` from "
            "a2a-sdk v1.2.2. If the service is up but the card is at another path, add that "
            f"path to _CARD_PATHS here and record it in docs/adr/{ADR} - the ADR currently "
            "lists the exact path as unverified.",
            pytrace=False,
        )

    # v1.0 moved `url` off the card root into `supported_interfaces`.
    assert "name" in card, f"agent card has no `name`: {sorted(card)}"
    assert "supported_interfaces" in card, (
        "agent card has no `supported_interfaces` - that means it is being built with the "
        "v0.3 shape (where `url` lived on the card root). ADR-0003 pins a2a-sdk 1.2.2 and "
        f"the v1.0 card shape. Card keys: {sorted(card)}"
    )
    assert "url" not in card, (
        "agent card still carries a top-level `url`; v1.0 removed it in favour of "
        "`supported_interfaces[].url`"
    )
    interfaces = card["supported_interfaces"]
    assert isinstance(interfaces, list) and interfaces, (
        f"supported_interfaces is empty: {interfaces!r}"
    )
    assert any(item.get("url") for item in interfaces), (
        f"no interface declares a URL: {interfaces!r}"
    )

    print(f"\n[A2A CARD] path responded: see {attempts}")
    print(f"[A2A CARD] name={card.get('name')!r} version={card.get('version')!r}")
    print(f"[A2A CARD] interfaces={interfaces}")
    print(f"[A2A CARD] capabilities={card.get('capabilities')!r}")


def test_task_state_enum_matches_the_adr() -> None:
    """The v1.0 enum is proto-style; v0.3's lowercase members must be gone."""
    _requirements()
    a2a_types = require_module("a2a.types", distribution="a2a-sdk==1.2.2", adr=ADR)
    task_state = getattr(a2a_types, "TaskState", None)
    if task_state is None:
        pytest.fail(
            f"`TaskState` is not exported from a2a.types in a2a-sdk==1.2.2. Modules: "
            f"{sorted(n for n in dir(a2a_types) if n[0].isupper())}\n"
            f"Update docs/adr/{ADR}: the enum location changed.",
            pytrace=False,
        )

    members = {name for name in dir(task_state) if name.startswith("TASK_STATE_")}
    missing = _EXPECTED_TASK_STATES - members
    if missing:
        pytest.fail(
            f"TaskState is missing members recorded in docs/adr/{ADR}: {sorted(missing)}\n"
            f"Present: {sorted(members)}\n"
            "The A2A task lifecycle changed again; amend the ADR and the adapters.",
            pytrace=False,
        )

    lowercase = {name for name in dir(task_state) if name.islower() and not name.startswith("_")}
    assert not lowercase, (
        f"v0.3 lowercase members still present: {sorted(lowercase)}. Code written against "
        "the old names would keep working silently, which is exactly what the migration "
        "notes say should not happen."
    )


def test_jsonrpc_round_trip_and_auth_enforcement() -> None:
    """An unauthenticated call must be rejected; an authenticated one must round trip."""
    base_url, token = _requirements()
    httpx = _httpx()

    rpc_url = f"{base_url}/a2a/jsonrpc/"
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": "spike-s-03",
        "method": "message/send",
        "params": {
            "message": {
                "messageId": "spike-s-03-msg",
                "role": "user",
                "parts": [{"kind": "text", "text": "Refer patient P-1001 to cardiology."}],
            }
        },
    }

    with httpx.Client(timeout=_timeout()) as client:
        unauthenticated = client.post(rpc_url, json=payload)
        authenticated = client.post(
            rpc_url, json=payload, headers={"Authorization": f"Bearer {token}"}
        )

    if unauthenticated.status_code not in (401, 403):
        pytest.fail(
            "A2A ROUTES ARE NOT AUTH-GUARDED.\n\n"
            f"POST {rpc_url} without an Authorization header returned "
            f"HTTP {unauthenticated.status_code} (expected 401/403).\n\n"
            f"Body: {unauthenticated.text[:500]}\n\n"
            "ADR-0003 puts bearer verification in a middleware over the A2A route prefix "
            "(a2a-sdk does not enforce anything itself - route factories exist precisely so "
            "the application can). An unauthenticated agent endpoint is a finding, not a "
            "test-configuration problem.",
            pytrace=False,
        )

    if authenticated.status_code >= 400:
        pytest.fail(
            f"AUTHENTICATED A2A CALL FAILED: HTTP {authenticated.status_code}\n\n"
            f"POST {rpc_url}\n"
            f"Body: {authenticated.text[:1000]}\n\n"
            "Check: A2A_SERVICE_TOKEN matches what the service expects, the route prefix is "
            "`/a2a/jsonrpc/`, and the JSON-RPC method name is still `message/send`.",
            pytrace=False,
        )

    body = authenticated.json()
    if "error" in body:
        error = body["error"]
        hint = ""
        if error.get("code") == -32601:
            hint = (
                "\n\nThe server reports METHOD NOT FOUND, which is expected if v1.2.2 renamed "
                "the method from the v0.3 `message/send`. Read the exact name from the "
                f"service's route definitions, update this spike and docs/adr/{ADR} - the ADR "
                "currently lists method names as unverified on purpose."
            )
        pytest.fail(
            f"JSON-RPC RETURNED AN ERROR: {error}{hint}",
            pytrace=False,
        )

    result = body.get("result")
    assert result is not None, f"JSON-RPC response carried no result: {body}"

    status = result.get("status") if isinstance(result, dict) else None
    if isinstance(status, dict) and "state" in status:
        state = str(status["state"]).upper()
        assert state in _EXPECTED_TASK_STATES, (
            f"task state {state!r} is not one of the v1.0 TASK_STATE_* values: "
            f"{sorted(_EXPECTED_TASK_STATES)}"
        )
        print(f"\n[A2A ROUND TRIP] http 200, task id={result.get('id')!r} state={state}")
    else:
        # Streaming and bare-Message responses are legitimate; record what came back.
        print(
            f"\n[A2A ROUND TRIP] http 200, result keys={sorted(result) if isinstance(result, dict) else result}"
        )
