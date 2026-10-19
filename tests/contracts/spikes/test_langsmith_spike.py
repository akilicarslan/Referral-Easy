"""Spike S-10: a trace actually reaching LangSmith - proven by readback.

The important detail, recorded in ADR-0010: LangSmith's OTLP endpoint acknowledges
with HTTP 200 and an **empty body**. The exporter therefore cannot distinguish
"accepted" from "ingested", so a green exporter log is not evidence. This spike emits
a span and then **reads the run back** through the LangSmith client.

Requires LANGSMITH_API_KEY and LANGSMITH_PROJECT. Run with ``make spike``.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest
from _spike_env import require_env, require_module

pytestmark = pytest.mark.spike

ADR = "ADR-0010-langsmith-tracing-for-adk.md"

# Verified in ADR-0010 (LangChain's OTEL announcement + the per-framework integration
# guides). A documented constant, not a silent default: override with
# LANGSMITH_OTLP_TRACES_ENDPOINT when pointing at a self-hosted deployment, whose
# endpoint shape differs.
DEFAULT_TRACES_ENDPOINT = "https://api.smith.langchain.com/otel/v1/traces"


def _requirements() -> tuple[str, str, str]:
    api_key = require_env(
        "LANGSMITH_API_KEY",
        purpose="authenticates the OTLP export (header `x-api-key`) and the readback",
        where="LangSmith workspace settings -> API keys",
        adr=ADR,
    )
    project = require_env(
        "LANGSMITH_PROJECT",
        purpose="the project traces are written to (`Langsmith-Project` header)",
        where="required by this spike explicitly: .env.example has no LANGSMITH_PROJECT "
        "today, and adding it is part of resolving this ADR. Defaulting a project name "
        "would scatter traces somewhere unexpected",
        adr=ADR,
    )
    endpoint = os.environ.get("LANGSMITH_OTLP_TRACES_ENDPOINT", DEFAULT_TRACES_ENDPOINT)
    return api_key, project, endpoint


def test_trace_is_emitted_and_readable() -> None:
    api_key, project, endpoint = _requirements()

    require_module(
        "opentelemetry.sdk.trace",
        distribution="opentelemetry-sdk (a dependency of google-adk)",
        adr=ADR,
    )
    require_module(
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
        distribution="opentelemetry-exporter-otlp-proto-http",
        adr=ADR,
    )
    sdk_trace = require_module("opentelemetry.sdk.trace", distribution="opentelemetry-sdk", adr=ADR)
    sdk_resources = require_module(
        "opentelemetry.sdk.resources", distribution="opentelemetry-sdk", adr=ADR
    )
    otlp = require_module(
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
        distribution="opentelemetry-exporter-otlp-proto-http",
        adr=ADR,
    )
    langsmith = require_module("langsmith", distribution="langsmith==0.14.8", adr=ADR)

    run_name = f"referraleasy-spike-s-10-{uuid.uuid4().hex[:8]}"
    exporter = otlp.OTLPSpanExporter(
        endpoint=endpoint,
        headers={"x-api-key": api_key, "Langsmith-Project": project},
    )
    provider = sdk_trace.TracerProvider(
        resource=sdk_resources.Resource.create({"service.name": "referraleasy-spike"})
    )
    provider.add_span_processor(sdk_trace.BatchSpanProcessor(exporter))

    tracer = provider.get_tracer("referraleasy.spike")
    try:
        with tracer.start_as_current_span(run_name) as span:
            # `langsmith.span.kind` is what makes LangSmith render a run by type rather
            # than as an anonymous span (ADR-0010, item 2).
            span.set_attribute("langsmith.span.kind", "chain")
            span.set_attribute("gen_ai.system", "openrouter")
            span.set_attribute("spike.phase", "0")
            span.set_attribute("spike.adr", ADR)
        flushed = provider.force_flush(timeout_millis=30_000)
    except Exception as exc:
        pytest.fail(
            "OTLP EXPORT TO LANGSMITH FAILED.\n\n"
            f"Endpoint: {endpoint}\n"
            f"Error: {type(exc).__name__}: {exc}\n\n"
            "Check that LANGSMITH_API_KEY is valid, that the `x-api-key` header name is "
            "right (LangSmith does NOT use `Authorization: Bearer`), and that outbound "
            f"network access to the endpoint is allowed. See docs/adr/{ADR}.",
            pytrace=False,
        )
    finally:
        provider.shutdown()

    assert flushed is not False, (
        "force_flush reported that spans were still queued after the timeout; the export "
        "did not complete"
    )

    client = langsmith.Client(api_key=api_key)
    deadline = time.monotonic() + float(os.environ.get("SPIKE_LANGSMITH_READBACK_TIMEOUT_S", "60"))
    seen = 0
    while time.monotonic() < deadline:
        runs = list(
            client.list_runs(
                project_name=project,
                filter=f'eq(name, "{run_name}")',
                limit=1,
            )
        )
        seen = len(runs)
        if seen:
            print(f"\n[LANGSMITH] run read back: name={runs[0].name!r} id={runs[0].id}")
            print(f"[LANGSMITH] endpoint={endpoint} project={project!r}")
            return
        time.sleep(3)

    pytest.fail(
        "EXPORT WAS ACCEPTED BUT NO RUN COULD BE READ BACK.\n\n"
        f"Span name waited for : {run_name}\n"
        f"Project              : {project!r}\n"
        f"Endpoint             : {endpoint}\n"
        f"Readback attempts    : 0 runs after the poll window\n\n"
        "This is the exact failure ADR-0010 warns about: the OTLP endpoint answers 200 "
        "with an empty body, so the exporter cannot tell acceptance from ingestion, and a "
        "green export log would have been a false positive.\n\n"
        "Check, in order: the project name (a name mismatch silently writes elsewhere), "
        "ingestion latency (retry with a larger "
        "SPIKE_LANGSMITH_READBACK_TIMEOUT_S), and the `langsmith.span.kind` attribute.",
        pytrace=False,
    )


def test_adk_instrumentor_is_importable() -> None:
    """The OpenInference ADK instrumentor is the source of GenAI attributes (ADR-0010)."""
    _requirements()
    module = require_module(
        "openinference.instrumentation.google_adk",
        distribution="openinference-instrumentation-google-adk==1.0.4",
        adr=ADR,
    )
    assert module is not None
    print(
        "\n[LANGSMITH] openinference-instrumentation-google-adk imported. "
        "ADR-0010 still lists as unverified whether it composes with ADK's own spans "
        "without duplicating them - compare a real ADK run's span tree next."
    )
