# ADR-0003 — A2A exposure: `a2a-sdk` version, Agent Card, transport mounting and auth

- **Status:** Accepted (SDK version pinned, shapes recorded). Transport decision made;
  **ADK↔a2a-sdk API compatibility is UNVERIFIED**.
- **Date:** 2026-10-14
- **Phase:** 0 (spike S-03)
- **Decides:** the A2A SDK pin, the Agent Card shape, how A2A routes are exposed, and where
  the service token is verified.

## Question

Which `a2a-sdk` version does ReferralEasy use, what is the Agent Card shape in that
version, should A2A be mounted into an existing ASGI app or served on its own port, what
are the task lifecycle state names, and who verifies the auth header?

## What was checked

| # | Command / URL | Result |
|---|---|---|
| 1 | `curl -s https://pypi.org/pypi/a2a-sdk/json` | latest `1.2.2`, `requires_python >=3.10` |
| 2 | `gh api repos/a2aproject/a2a-python/releases/latest` | `v1.2.2`, published `2026-10-05T09:50:04Z` |
| 3 | `gh api repos/google/adk-python/contents/src/google/adk/models?ref=v2.12.0` (via PyPI `requires_dist`) | `google-adk[a2a]` requires `a2a-sdk[http-server]<2,>=0.3.4` — a range spanning the v0.3 → v1.0 **breaking** change |
| 4 | `gh api repos/a2aproject/a2a-python/contents/src/a2a?ref=v1.2.2` | modules `auth/`, `client/`, `compat/v0_3/`, `server/`, `types/`, `utils/`, `extensions/` |
| 5 | `docs/migrations/v1_0/README.md` in the same repo @ `v1.2.2` | TaskState renames, AgentCard restructure, wrapper-class removal, route factories |
| 6 | `gh search code "TASK_STATE"` in `a2aproject/a2a-python` | proto-style enum confirmed in `tck/sut_agent.py`, `itk/main.py`, `server/tasks/task_updater.py` |

## Decision

### Pin

- **`a2a-sdk==1.2.2`** — CONFIRMED to exist (PyPI + upstream tag agree), and it satisfies
  ADK's declared `<2,>=0.3.4` range.

### Agent Card (v1.0 shape)

The `url` field has **moved off `AgentCard`** into a list of interfaces. The card as this
project will build it:

```python
AgentCard(
    name=..., description=..., version=...,
    supported_interfaces=[
        AgentInterface(protocol_binding="JSONRPC", url="http://<host>/a2a/jsonrpc/"),
        # AgentInterface(protocol_binding="HTTP+JSON", url=".../a2a/rest/"),
    ],
    default_input_modes=["text/plain", "application/json"],
    default_output_modes=["text/plain", "application/json"],
    capabilities=AgentCapabilities(streaming=True, extended_agent_card=True),
    skills=[AgentSkill(id=..., name=..., description=..., tags=[...],
                       input_modes=[...], output_modes=[...], examples=[...])],
)
```

Renames that matter when reading older examples (they will not work as written):

| v0.3 | v1.x |
|---|---|
| `AgentCard.url` | `AgentCard.supported_interfaces[].url` |
| `AgentCard.supports_authenticated_extended_card` | `AgentCapabilities.extended_agent_card` |
| `AgentCard.examples` | `AgentSkill.examples` |
| `AgentCapabilities.input_modes` / `output_modes` | `AgentCard.default_input_modes` / `default_output_modes` (or per-skill) |

### Task lifecycle state names

v1.0 renamed every enum to proto style. The states are:

`TASK_STATE_UNSPECIFIED` (new), `TASK_STATE_SUBMITTED`, `TASK_STATE_WORKING`,
`TASK_STATE_COMPLETED`, `TASK_STATE_FAILED`, `TASK_STATE_CANCELED`,
`TASK_STATE_INPUT_REQUIRED`, `TASK_STATE_AUTH_REQUIRED`, `TASK_STATE_REJECTED`.

The v0.3 lowercase spellings (`TaskState.working`, …) no longer exist. Any adapter written
against the old names breaks at import/member-access time — which is good, it fails loudly.

### How A2A is exposed — route factories, mounted, not a separate port

v1.0 **removed** the application wrapper classes (`A2AStarletteApplication`,
`A2AFastApiApplication`, `A2ARESTFastApiApplication`). Only route factories remain:

```python
routes  = create_agent_card_routes(agent_card)
routes += create_jsonrpc_routes(request_handler, rpc_url="/a2a/jsonrpc/")
app = Starlette(routes=routes)          # or FastAPI(routes=routes)
```

Decision, with rationale:

- **Each agent registers these routes into its own ASGI app, at a path prefix** — no second
  port per agent, and no separate long-lived server process. One deployable, one port per
  agent service, one lifecycle.
- `uvicorn`/the process manager stays responsible for the port; the routes are path-scoped
  (`/a2a/jsonrpc/`, agent card) so a health endpoint, and later the A2UI surface, can share
  the app without collision.
- **The gateway remains the only public entry point.** A2A routes are reachable
  service-to-service and are not published directly to the demo's browser origin.
- If a future agent needs isolation (separate scaling, different runtime), the same route
  factories are served by a dedicated app — this is a deployment change, not a code change.
  That is precisely why the factories were preferred over a monolith wrapper.

### Auth-header verification

`a2a-sdk` ships `src/a2a/auth/user.py`, `src/a2a/client/auth/` and `client/interceptors.py`.
It does **not** enforce anything itself: the v1.0 migration notes say the route-factory
approach exists to give the application "better control over routing, **middleware,
authentication**, logging". So verification is ours:

- A Starlette middleware wraps the A2A route prefix and requires
  `Authorization: Bearer $A2A_SERVICE_TOKEN`, compared with `secrets.compare_digest`
  (constant time).
- **The public Agent Card route is deliberately exempt** — discovery must work without
  credentials. Any *extended* card (the `extended_agent_card` capability) stays behind the
  bearer check.
- Failure is `401` with a JSON-RPC-shaped error body, never a redirect and never a
  partially-served request.

## What remains unverified

1. **ADK 2.12.0's A2A integration against `a2a-sdk==1.2.2`.** ADK declares
   `a2a-sdk>=0.3.4,<2` — a range that admits both the pre-v1 and post-v1 APIs, which
   changed incompatibly (enums renamed, wrappers deleted, `AgentCard.url` moved). The
   declared range therefore does **not** prove compatibility. This is the single largest
   risk in this ADR and only the spike can settle it.
2. **The fixed path of the agent-card route** served by `create_agent_card_routes` (the
   `/.well-known/...` convention is expected but was not verified in the source).
3. **JSON-RPC method names** in v1.2.2 — not verified. The spike asserts the round trip
   behaviourally rather than assuming method strings.
4. **Whether the SDK enforces `capabilities.extended_agent_card`** or leaves it entirely to
   application middleware.
5. **Push-notification config**: present in the schema, not exercised.
6. **`protocol_version` semantics** — the v1.2.2 task rows carry a `protocol_version`
   column, implying multi-version coexistence; how it is populated is unverified.

## Consequences

- The pin is one package wide; nothing else in the repo imports A2A types directly, so a
  future v2 upgrade is a bounded change.
- Spike `tests/contracts/spikes/test_a2a_spike.py` fetches the served Agent Card and asserts
  the v1.0 shape (`supported_interfaces` present, `url` absent from the card root), then
  runs a client round trip and asserts the terminal state is drawn from the
  `TASK_STATE_*` set above. It fails loudly naming `A2A_SERVICE_TOKEN` when absent.
