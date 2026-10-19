# The seven specialist agents

One image, seven roles. `referraleasy_agents` is the import package; `<role>/__main__.py`
is each service's entry point.

| directory     | role                | tool surface (§3)                                          |
| ------------- | ------------------- | ---------------------------------------------------------- |
| `coordinator` | `care_coordinator`  | `delegate_task` only                                       |
| `intake`      | `referral_intake`   | `validate_referral`, `resolve_location`, `patient_preferences` |
| `coverage`    | `coverage_network`  | `check_plan`, `check_provider_network`, `get_coverage_rules` |
| `matching`    | `provider_matching` | `find_candidates`, `search_directory` (+ two sub-agents)    |
| `readiness`   | `referral_readiness`| `get_referral_requirements`, `inspect_document`, `validate_referral_package` |
| `scheduling`  | `scheduling`        | `list_slots`, `lookup_request`, `create_booking`, `cancel_booking`, `reschedule_booking` |
| `recovery`    | `recovery_handoff`  | `classify_error`, `create_handoff`, `search_directory`      |

## Running one

```
AGENT_ROLE=scheduling \
A2A_SERVICE_TOKEN=...            \
AGENT_MODEL_ID=anthropic/claude-sonnet-4.5 \
OPENROUTER_API_KEY=...           \
MOCK_API_URL=http://localhost:4010 \
DATABASE_URL=postgresql://...    \
python services/agents/scheduling/__main__.py
```

`A2A_BASE_URL` (default `http://{role}:8080`) is where the coordinator reaches its
peers. `DATABASE_URL` is the policy authority: **without it every tool call is refused**
(`PolicyUnavailable`), which is the runtime's default-deny rule and not a bug.

The offline profile runs a real ADK turn with no socket at all:

```
MODEL_BACKEND=offline
OFFLINE_SCRIPT='[{"tool": "list_slots", "args": {...}}, {"text": "{...}"}]'
```

`AGENT_MODEL_ID` and a non-empty `OPENROUTER_API_KEY` still have to be *present* for
`AgentSettings.from_env` to resolve, because the runtime's model factory implements
one provider (ADR-0002) and `MODEL_BACKEND=offline` is read by this package, not by
it. The offline path never calls the factory, never reads the key and never opens a
socket. Wiring the offline profile into the runtime's factory is Phase 3.5's work,
and is recorded in `docs/adr/ADR-0033-adk-runner-for-specialists.md`.

## A step a human has to take

`services/agents/*` is **not** a member of `[tool.uv.workspace]` in the root
`pyproject.toml` — the glob is commented out, and this workstream may not edit that
file. Until someone adds it (plus a `[tool.uv.sources]` entry), `referraleasy_agents`
is not installed into the shared venv:

* `uv run --no-sync pytest tests/agents` works — `tests/agents/conftest.py` puts
  `services/agents` on `sys.path`, narrowly and idempotently.
* `uv run --no-sync python services/agents/scheduling/__main__.py` does not, because
  `referraleasy_agents` cannot be imported from a process that never loaded that
  conftest.

The fix is two lines in the root `pyproject.toml` and it is not this package's to make.
