"""LLM tracing to Langfuse: one ADK turn becomes one readable agent observation (ADR-0042).

§15's pipeline (OTel collector → Data Prepper → OpenSearch) answers *how often* and *how slow*.
It does not answer *what did the model see and say*, which is what this module is for. It adds a
second exporter, not a second instrumentation:

* **One provider, two exporters.** Langfuse's span processor is attached to the provider
  ``configure_telemetry`` already built, so a span is created once and each exporter gets its own
  copy. Instrumenting ADK a second time on a separate provider would record every model call twice.
* **The OpenInference ADK instrumentor** turns a turn into ``agent``/``generation``/``tool``
  observations with model, token usage and message content. It also *disables ADK's own tracers*
  (``_disable_existing_tracers``), which is what makes "each model call recorded once" true.
* **Langfuse's default span filter is kept.** It exports Langfuse, ``gen_ai.*`` and
  ``openinference.*`` spans only, so the A2A server spans and HTTP client spans this process also
  produces stay in OpenSearch and out of the Langfuse bill.
* **ADK 2.12 is patched where the instrumentor misses it.** ``openinference-instrumentation-
  google-adk==1.0.4`` suppresses ADK's duplicate spans only on ``start_as_current_span``, and ADK
  2.12 opens ``invocation`` and ``invoke_agent`` with ``start_span``. Unpatched, the second is a
  duplicate agent node holding every generation and the first is filtered out, orphaning the
  agent below it. :func:`_suppress_duplicate_adk_spans` closes that gap;
  ``tests/agents/test_llm_tracing_span_tree.py`` fails if an upgrade reopens it.
* **The workflow path has no model and no instrumentor**, so its governed tool calls are recorded
  as ``tool`` observations by :meth:`LlmTracing.tool` instead.

**Masking is not inherited.** The repo's ``MaskingExporter`` and the collector's ``mask_pii`` only
see the OpenSearch copy; this exporter reads the spans before either. ``_mask_otel_spans`` applies
the same :class:`~referraleasy_telemetry.MaskingPolicy` to the Langfuse copy, so named identifiers
are pseudonymised or redacted and e-mails, phones and tokens are scrubbed from prompt text. Message
content itself is kept — capturing it is the point of this exporter. To drop it, OpenInference's
own switches apply: ``OPENINFERENCE_HIDE_INPUTS`` / ``OPENINFERENCE_HIDE_OUTPUTS``.

**Off unless every condition holds**, and inert rather than failing when one does not:

=====================================  ==========================================================
``LANGFUSE_PUBLIC_KEY`` / ``_SECRET_KEY``  Both set. Missing keys mean "not configured", not an error.
``LANGFUSE_BASE_URL``                  Set. The SDK would otherwise default to the EU region and a
                                       US project's traces would be rejected somewhere else.
``LANGFUSE_TRACING_ENABLED``           Not ``false``. The SDK's own switch, honoured here too.
``MODEL_BACKEND``                      Not ``offline``. The offline profile is zero network calls;
                                       exporting from it would break that guarantee silently.
=====================================  ==========================================================

``LANGFUSE_TRACING_ENVIRONMENT`` names the environment; without it ``APP_ENV`` is used, so a
``demo`` stack's traces do not land in the same filter as a ``local`` laptop's.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from referraleasy_telemetry import DEFAULT_POLICY, MaskingPolicy

if TYPE_CHECKING:
    from langfuse import Langfuse
    from langfuse.types import MaskOtelSpansParams, MaskOtelSpansResult, TraceContext

    from referraleasy_contracts.a2a import AgentTaskRequest
    from referraleasy_telemetry import Telemetry

logger = logging.getLogger(__name__)

ENV_PUBLIC_KEY: Final[str] = "LANGFUSE_PUBLIC_KEY"
ENV_SECRET_KEY: Final[str] = "LANGFUSE_SECRET_KEY"
ENV_BASE_URL: Final[str] = "LANGFUSE_BASE_URL"
ENV_TRACING_ENABLED: Final[str] = "LANGFUSE_TRACING_ENABLED"
ENV_ENVIRONMENT: Final[str] = "LANGFUSE_TRACING_ENVIRONMENT"
ENV_MODEL_BACKEND: Final[str] = "MODEL_BACKEND"

#: Langfuse's environment naming rule: lowercase, digits, ``-``/``_``, no ``langfuse`` prefix,
#: at most 40 characters. A value that breaks it is not sent, rather than sent and rejected.
_ENVIRONMENT_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(?!langfuse)[a-z0-9_-]{1,40}$")

#: Payload keys that are the turn's orchestration metadata, not the role's input. Mirrors
#: ``referraleasy_agents.handler.META_KEYS``: the observation's input is what the role was asked.
_META_KEYS: Final[frozenset[str]] = frozenset({"task_envelope", "command_id", "execution_mode"})

#: Native ADK spans that OpenInference already records as its own ``invocation [...]``,
#: ``agent_run [...]`` and ``call_llm`` spans. Tool, workflow and compaction spans are not listed:
#: the instrumentor routes those to its own tracer.
_DUPLICATE_ADK_SPANS: Final[tuple[str, ...]] = ("invoke_agent ", "generate_content")
_DUPLICATE_ADK_SPAN_NAMES: Final[frozenset[str]] = frozenset({"invocation"})

#: Dropped from the Langfuse copy only. OpenInference copies ADK's ``user_id`` here, and this repo's
#: ADK user is the workflow (``assembly.session_key``), not a person: a Langfuse *user* that is
#: really a workflow would make the Users view a second, wrong Sessions view.
_DROPPED_ATTRIBUTES: Final[frozenset[str]] = frozenset({"user.id"})

#: On a generation whose messages were converted, every other copy of the same request and response:
#: ADK's own ``llm_request``/``llm_response`` and OpenInference's raw ``input.value``/``output.value``.
#: Left in, each generation would ship its prompt three times.
_SUPERSEDED_BY_CHAT: Final[tuple[str, ...]] = (
    "input.value",
    "input.mime_type",
    "gcp.vertex.agent.llm_request",
    "gcp.vertex.agent.llm_response",
)
_SUPERSEDED_OUTPUT: Final[tuple[str, ...]] = ("output.value", "output.mime_type")

_MESSAGE_ATTRIBUTE: Final[re.Pattern[str]] = re.compile(
    r"^llm\.(input|output)_messages\.(\d+)\.message\.(.+)$"
)
_CONTENT_PART: Final[re.Pattern[str]] = re.compile(
    r"^contents\.(\d+)\.message_content\.(text|type)$"
)
_TOOL_CALL: Final[re.Pattern[str]] = re.compile(
    r"^tool_calls\.(\d+)\.tool_call\.(id|function\.name|function\.arguments)$"
)

#: ADK's role names, in the OpenAI vocabulary Langfuse renders as a conversation.
_ROLES: Final[Mapping[str, str]] = {"model": "assistant"}

_active: LlmTracing | None = None


def _disabled_reason(env: Mapping[str, str]) -> str | None:
    """Why tracing stays off, or ``None`` when every condition holds."""
    if (env.get(ENV_MODEL_BACKEND) or "").strip().lower() == "offline":
        return "MODEL_BACKEND=offline makes zero network calls"
    if (env.get(ENV_TRACING_ENABLED) or "").strip().lower() in {"0", "false", "no"}:
        return f"{ENV_TRACING_ENABLED}=false"
    missing = [
        name
        for name in (ENV_PUBLIC_KEY, ENV_SECRET_KEY, ENV_BASE_URL)
        if not (env.get(name) or "").strip()
    ]
    if missing:
        return f"{', '.join(missing)} not set"
    return None


def _environment(env: Mapping[str, str]) -> str | None:
    value = (env.get(ENV_ENVIRONMENT) or env.get("APP_ENV") or "").strip().lower()
    return value if _ENVIRONMENT_PATTERN.match(value) else None


def _messages(attributes: Mapping[str, Any], direction: str) -> list[dict[str, Any]]:
    """OpenInference's flattened ``llm.<direction>_messages.*`` as OpenAI-format messages.

    The shape is the one Langfuse renders as a conversation: ``role`` and ``content``, tool calls in
    the assistant message's ``tool_calls`` with ``arguments`` as a JSON *string*, and each tool result
    as a ``tool`` message carrying the ``tool_call_id`` it answers. A ``reasoning`` text part is kept
    beside the content rather than merged into it, so the model's thinking stays distinguishable.
    """
    grouped: dict[int, dict[str, Any]] = {}
    for key, value in attributes.items():
        match = _MESSAGE_ATTRIBUTE.match(key)
        if match is None or match.group(1) != direction:
            continue
        grouped.setdefault(int(match.group(2)), {})[match.group(3)] = value

    messages = []
    for _, fields in sorted(grouped.items()):
        parts: dict[int, dict[str, Any]] = {}
        calls: dict[int, dict[str, Any]] = {}
        for field, value in fields.items():
            if part := _CONTENT_PART.match(field):
                parts.setdefault(int(part.group(1)), {})[part.group(2)] = value
            elif call := _TOOL_CALL.match(field):
                calls.setdefault(int(call.group(1)), {})[call.group(2)] = value

        role = str(fields.get("role") or "user")
        message: dict[str, Any] = {"role": _ROLES.get(role, role)}
        texts = [
            p.get("text", "") for _, p in sorted(parts.items()) if p.get("type") != "reasoning"
        ]
        thinking = [
            p.get("text", "") for _, p in sorted(parts.items()) if p.get("type") == "reasoning"
        ]
        content = fields.get("content")
        message["content"] = content if isinstance(content, str) else "\n".join(texts)
        if thinking:
            message["reasoning"] = "\n".join(thinking)
        if calls:
            message["tool_calls"] = [
                {
                    "id": call.get("id"),
                    "type": "function",
                    "function": {
                        "name": call.get("function.name"),
                        "arguments": call.get("function.arguments") or "{}",
                    },
                }
                for _, call in sorted(calls.items())
            ]
        for name in ("name", "tool_call_id"):
            if fields.get(name):
                message[name] = fields[name]
        messages.append(message)
    return messages


def _with_chat_view(attributes: Mapping[str, Any]) -> dict[str, Any]:
    """A generation's attributes with its messages as Langfuse's own input and output.

    ``langfuse.observation.input``/``output`` outrank ``input.value``/``output.value`` in Langfuse's
    mapping, and only these two read as a conversation: OpenInference's ``input.value`` is ADK's raw
    ``LlmRequest``, which Langfuse can only show as JSON. The flattened message attributes are
    removed once converted, along with the other copies of the same text.
    """
    inputs = _messages(attributes, "input")
    if not inputs:
        return dict(attributes)
    outputs = _messages(attributes, "output")
    converted = {
        key: value
        for key, value in attributes.items()
        if not key.startswith(("llm.input_messages.", "llm.output_messages."))
        and key not in _SUPERSEDED_BY_CHAT
        and not (outputs and key in _SUPERSEDED_OUTPUT)
    }
    converted["langfuse.observation.input"] = json.dumps(inputs, ensure_ascii=False)
    if outputs:
        converted["langfuse.observation.output"] = json.dumps(
            outputs[0] if len(outputs) == 1 else outputs, ensure_ascii=False
        )
    return converted


def _langfuse_attributes(attributes: Mapping[str, Any], policy: MaskingPolicy) -> dict[str, Any]:
    """What one span's attributes become in Langfuse: masked first, then reshaped.

    Masking runs before the chat conversion so the conversion only ever sees masked text, and the
    messages it builds cannot reintroduce a value the policy removed.
    """
    masked = {
        key: value
        for key, value in policy.mask_mapping(attributes).items()
        if key not in _DROPPED_ATTRIBUTES
    }
    if masked.get("openinference.span.kind") == "LLM":
        return _with_chat_view(masked)
    return masked


def _mask_otel_spans_with(policy: MaskingPolicy) -> Any:
    """A ``mask_otel_spans`` hook that applies :func:`_langfuse_attributes` to each span.

    ``MaskingPolicy.mask_mapping`` may *rename* a key (``patient.identifier`` becomes
    ``patient.identifier_hash``), so the patch deletes what disappeared and sets what changed. An
    exception here makes Langfuse drop the whole batch, which is the right failure: over-masking by
    omission rather than exporting an unmasked batch.
    """
    from langfuse.types import MaskOtelSpansResult, OtelSpanPatch

    def mask_otel_spans(*, params: MaskOtelSpansParams) -> MaskOtelSpansResult | None:
        patches = {}
        for identifier, span in params.spans.items():
            original = dict(span.attributes)
            patched = _langfuse_attributes(original, policy)
            removed = tuple(key for key in original if key not in patched)
            changed = {
                key: value
                for key, value in patched.items()
                if key not in original or original[key] != value
            }
            if removed or changed:
                patches[identifier] = OtelSpanPatch(
                    delete_attributes=removed, set_attributes=changed
                )
        return MaskOtelSpansResult(span_patches=patches) if patches else None

    return mask_otel_spans


def _suppress_duplicate_adk_spans() -> None:
    """Make the instrumentor's ADK tracer proxy suppress ``start_span`` as it does the rest.

    The stand-in is a non-recording span carrying the *parent's* context, which is what
    ``_PassthroughTracer`` achieves for ``start_as_current_span``: whatever ADK opens beneath it is
    parented to the OpenInference span above, and ADK's ``end()`` and attribute writes are no-ops.
    Returning the parent span itself would let ADK end the OpenInference span early.
    """
    from openinference.instrumentation.google_adk import _SelectiveExecuteToolTracer
    from opentelemetry import trace
    from opentelemetry.trace import NonRecordingSpan

    if getattr(_SelectiveExecuteToolTracer, "_referraleasy_start_span_patched", False):
        return

    def start_span(self: Any, name: str, context: Any = None, *args: Any, **kwargs: Any) -> Any:
        if isinstance(name, str) and (
            name in _DUPLICATE_ADK_SPAN_NAMES or name.startswith(_DUPLICATE_ADK_SPANS)
        ):
            return NonRecordingSpan(trace.get_current_span(context).get_span_context())
        return self.__wrapped__.start_span(name, context, *args, **kwargs)

    setattr(_SelectiveExecuteToolTracer, "start_span", start_span)  # noqa: B010
    setattr(_SelectiveExecuteToolTracer, "_referraleasy_start_span_patched", True)  # noqa: B010


@dataclass(frozen=True, slots=True)
class TurnObservation:
    """The handle a turn gets to report what it produced. A no-op when tracing is off."""

    span: Any = None

    def finish(
        self,
        *,
        output: Any,
        outcome: str,
        metadata: Mapping[str, Any] | None = None,
        error: str | None = None,
        level: str = "ERROR",
    ) -> None:
        """Record the result. ``error`` sets the level (``ERROR`` unless given) and the status."""
        if self.span is None:
            return
        self.span.update(
            output=output,
            metadata={"outcome": outcome, **(metadata or {})},
            **({"level": level, "status_message": error} if error else {}),
        )


@dataclass(frozen=True, slots=True)
class LlmTracing:
    """Langfuse for one agent process. ``client`` is ``None`` when tracing is off."""

    client: Langfuse | None = None
    disabled_reason: str | None = None

    @property
    def active(self) -> bool:
        return self.client is not None

    @contextmanager
    def turn(
        self, role: str, request: AgentTaskRequest, payload: Mapping[str, Any]
    ) -> Iterator[TurnObservation]:
        """One agent turn as one ``agent`` observation, with trace attributes propagated.

        * **Name** is the role, verb first and stable (``run-provider-matching``): evaluators and
          dashboards target observations by name, so it carries no per-run value.
        * **Input** is what the role was asked — the payload without the orchestration keys — and
          the output, set by :meth:`TurnObservation.finish`, is the validated decision.
        * **Session** is the workflow: one referral spans several commands with a patient or a
          human answering in between, and each command is its own trace. OpenInference keeps its
          own session context (and would otherwise use ADK's per-command session id), so the same
          id is pushed there too.
        * **Tags** are the role: a coordinator trace that delegated carries every role it reached.
        * **Parent** is the caller's span when the turn arrived over A2A. The span in between is
          this service's HTTP server span, which Langfuse's filter drops as noise; parenting past it
          is what puts a delegated specialist *under* the coordinator's ``delegate_task`` call
          instead of beside it as a second root in the same trace.
        """
        if self.client is None:
            yield TurnObservation()
            return

        from langfuse import propagate_attributes
        from openinference.instrumentation import using_session

        command_id = request.payload.get("command_id")
        caller = _inbound_caller()
        with ExitStack() as stack:
            span = stack.enter_context(
                self.client.start_as_current_observation(
                    trace_context=caller,
                    as_type="agent",
                    name=f"run-{role.replace('_', '-')}",
                    input={key: value for key, value in payload.items() if key not in _META_KEYS},
                    metadata={
                        "task": request.task,
                        "workflow_state": request.workflow_state,
                        "idempotency_key": request.idempotency_key,
                    },
                )
            )
            stack.enter_context(
                propagate_attributes(
                    session_id=request.workflow_id,
                    tags=[role],
                    metadata={
                        "workflow_id": request.workflow_id,
                        **({"command_id": command_id} if isinstance(command_id, str) else {}),
                    },
                )
            )
            stack.enter_context(using_session(request.workflow_id))
            yield TurnObservation(span)

    @contextmanager
    def tool(self, name: str, arguments: Mapping[str, Any]) -> Iterator[TurnObservation]:
        """One governed tool call as a ``tool`` observation, for the path no instrumentor sees.

        Only the workflow path calls this. On the ADK path the instrumentor already records each
        tool as ``execute_tool <name>``, and a second observation here would double it.
        """
        if self.client is None:
            yield TurnObservation()
            return
        with self.client.start_as_current_observation(
            as_type="tool", name=name, input=dict(arguments)
        ) as span:
            yield TurnObservation(span)


def _inbound_caller() -> TraceContext | None:
    """The remote caller's span, when the active span is a server span continuing its trace.

    ``install_server_spans`` opens the server span from the inbound ``traceparent``, so its parent
    is the caller's span context, marked remote. Anything else — no active span, a local parent, a
    root span — means there is no caller to attach to, and the observation keeps its ambient parent.
    """
    from opentelemetry import trace

    parent = getattr(trace.get_current_span(), "parent", None)
    if parent is None or not parent.is_remote or not parent.is_valid:
        return None
    return {"trace_id": f"{parent.trace_id:032x}", "parent_span_id": f"{parent.span_id:016x}"}


def active_llm_tracing() -> LlmTracing:
    """The process's tracing, or a disabled one before (or without) :func:`configure_llm_tracing`.

    For code below the turn that has no handle passed to it. ``langfuse.get_client()`` is not a
    substitute: with keys in the environment it builds an exporting client even when this module
    decided tracing must stay off, for example under ``MODEL_BACKEND=offline``.
    """
    return _active if _active is not None else LlmTracing()


def configure_llm_tracing(
    telemetry: Telemetry,
    *,
    release: str,
    env: Mapping[str, str] | None = None,
    policy: MaskingPolicy = DEFAULT_POLICY,
) -> LlmTracing:
    """Attach Langfuse to ``telemetry``'s provider and instrument ADK, when configured.

    Call once per process, after ``configure_telemetry`` and before the first ``Runner`` runs. The
    instrumentor patches ADK at class level, so a second call would be a second instrumentation.
    """
    resolved = os.environ if env is None else env
    reason = _disabled_reason(resolved)
    if reason is not None or telemetry.tracer_provider is None:
        reason = reason or "no tracer provider"
        logger.info("Langfuse tracing disabled: %s", reason)
        return LlmTracing(disabled_reason=reason)

    from langfuse import Langfuse
    from openinference.instrumentation.google_adk import GoogleADKInstrumentor

    client = Langfuse(
        public_key=resolved[ENV_PUBLIC_KEY].strip(),
        secret_key=resolved[ENV_SECRET_KEY].strip(),
        base_url=resolved[ENV_BASE_URL].strip(),
        tracer_provider=telemetry.tracer_provider,
        environment=_environment(resolved),
        release=release,
        mask_otel_spans=_mask_otel_spans_with(policy),
    )
    GoogleADKInstrumentor().instrument(tracer_provider=telemetry.tracer_provider)
    _suppress_duplicate_adk_spans()
    global _active
    _active = LlmTracing(client=client)
    return _active
