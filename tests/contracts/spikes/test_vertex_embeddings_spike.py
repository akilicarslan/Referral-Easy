"""Spike S-06: ``gemini-embedding-001`` at 768 dimensions, with asymmetric task types.

ADR-0006 pins ``EMBEDDING_DIMENSIONS=768`` but records that the vendor docs only ever
demonstrate the 3072 default ("up to 3072"), so **768 is asserted here, not assumed**.
It also records that whether truncated embeddings must be re-normalised before cosine
similarity was not verified for this model - so the norms are printed for the record.

Blocked on the same credentials as ADR-0009 (Vertex). Requires VERTEX_PROJECT_ID,
VERTEX_LOCATION, EMBEDDING_MODEL_ID, EMBEDDING_DIMENSIONS and application-default
credentials.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from _spike_env import require_env, require_module

pytestmark = pytest.mark.spike

ADR = "ADR-0006-embeddings-gemini-embedding-001.md"

_ADC_PATH = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"


def _credentials_available() -> bool:
    import os

    key_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    return (bool(key_file) and Path(key_file).is_file()) or _ADC_PATH.is_file()


def _requirements() -> tuple[str, str, str, int]:
    project = require_env(
        "VERTEX_PROJECT_ID",
        purpose="the GCP project whose Vertex AI endpoint serves the embedding model",
        where="a project with the Vertex AI API enabled; see docs/adr/ADR-0009 for what "
        "resolving this requires",
        adr=ADR,
    )
    location = require_env(
        "VERTEX_LOCATION",
        purpose="the region serving the embedding model",
        where="must be a region that serves BOTH the embedding model and the agent model "
        "(ADR-0009) - a region serving only one fails at run time, in the other path",
        adr=ADR,
    )
    model = require_env(
        "EMBEDDING_MODEL_ID",
        purpose="the embedding model under test",
        where=".env.example pins gemini-embedding-001",
        adr=ADR,
    )
    dimensions = int(
        require_env(
            "EMBEDDING_DIMENSIONS",
            purpose="the output dimensionality requested via output_dimensionality",
            where=".env.example pins 768; it must equal the OpenSearch knn_vector "
            "dimension (ADR-0008) or indexing fails",
            adr=ADR,
        )
    )

    if not _credentials_available():
        pytest.fail(
            "NO VERTEX CREDENTIALS.\n\n"
            "Neither GOOGLE_APPLICATION_CREDENTIALS points at an existing file, nor is "
            f"there application-default credentials at {_ADC_PATH}.\n\n"
            "Provide one of:\n"
            "  gcloud auth application-default login\n"
            "  export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json\n\n"
            "The identity needs roles/aiplatform.user on the project.",
            pytrace=False,
        )
    return project, location, model, dimensions


def _embed(
    model: str, location: str, project: str, task_type: str, texts: list[str], dimensions: int
) -> list[list[float]]:
    genai = require_module("google.genai", distribution="google-genai==2.30.1", adr=ADR)
    genai_types = require_module("google.genai.types", distribution="google-genai==2.30.1", adr=ADR)

    client = genai.Client(vertexai=True, project=project, location=location)
    try:
        response = client.models.embed_content(
            model=model,
            contents=texts,
            config=genai_types.EmbedContentConfig(
                task_type=task_type,
                output_dimensionality=dimensions,
            ),
        )
    except Exception as exc:
        pytest.fail(
            f"EMBEDDING CALL FAILED ({model} @ {location}, task_type={task_type}).\n\n"
            f"Error: {type(exc).__name__}: {exc}\n\n"
            "If the error names the model, EMBEDDING_MODEL_ID is not served in this region "
            "(ADR-0009). If it names output_dimensionality, 768 is not an accepted value "
            f"for this model - which is exactly what docs/adr/{ADR} flags as unverified.",
            pytrace=False,
        )
    return [list(embedding.values or []) for embedding in response.embeddings]


def test_output_dimensionality_is_honoured() -> None:
    project, location, model, dimensions = _requirements()
    vectors = _embed(
        model, location, project, "RETRIEVAL_DOCUMENT", ["Patient referral document."], dimensions
    )

    assert vectors, "the embedding response carried no vectors"
    length = len(vectors[0])
    if length != dimensions:
        pytest.fail(
            f"OUTPUT DIMENSIONALITY NOT HONOURED: requested {dimensions}, got {length}.\n\n"
            f"Model {model} @ {location}.\n\n"
            "The OpenSearch index is mapped at knn_vector dimension "
            f"{dimensions} (ADR-0008), so a different width is an indexing failure waiting "
            f"to happen. Amend docs/adr/{ADR} with the measured width.",
            pytrace=False,
        )
    assert all(len(vector) == dimensions for vector in vectors), "vectors have inconsistent widths"


def test_document_and_query_task_types_differ() -> None:
    """Asymmetric embedding is a documented requirement, so assert the vectors differ."""
    project, location, model, dimensions = _requirements()
    text = "Refer patient P-1001 to cardiology within two weeks."

    as_document = _embed(model, location, project, "RETRIEVAL_DOCUMENT", [text], dimensions)[0]
    as_query = _embed(model, location, project, "RETRIEVAL_QUERY", [text], dimensions)[0]

    if as_document == as_query:
        pytest.fail(
            "RETRIEVAL_DOCUMENT and RETRIEVAL_QUERY produced identical vectors.\n\n"
            "Either the task_type is being ignored on this route, or it no longer changes "
            "the output. ADR-0006 builds the embedding port around the two being distinct "
            "(embed_documents vs embed_queries); if they are the same, that split is "
            f"decoration and docs/adr/{ADR} must say so.",
            pytrace=False,
        )

    norm_document = math.sqrt(sum(value * value for value in as_document))
    norm_query = math.sqrt(sum(value * value for value in as_query))
    print(f"\n[EMBEDDINGS] model={model} location={location} dimensions={dimensions}")
    print(f"[EMBEDDINGS] ||RETRIEVAL_DOCUMENT||={norm_document:.6f}")
    print(f"[EMBEDDINGS] ||RETRIEVAL_QUERY||={norm_query:.6f}")
    print(
        "[EMBEDDINGS] ADR-0006 leaves open whether truncated output must be "
        "re-normalised before cosine similarity. Record these norms in the ADR "
        "amendment: a norm materially away from 1.0 means normalisation is required."
    )


def test_similarity_ordering_is_sane() -> None:
    """A retrieval sanity check: the topical document must beat an unrelated one."""
    project, location, model, dimensions = _requirements()
    query = "urgent cardiology referral for chest pain"
    documents = [
        "Urgent cardiology referral for a 61 year old with exertional chest pain.",
        "Routine dermatology referral for a persistent facial rash.",
    ]

    query_vector = _embed(model, location, project, "RETRIEVAL_QUERY", [query], dimensions)[0]
    document_vectors = _embed(model, location, project, "RETRIEVAL_DOCUMENT", documents, dimensions)

    def cosine(left: list[float], right: list[float]) -> float:
        dot = sum(a * b for a, b in zip(left, right, strict=True))
        left_norm = math.sqrt(sum(a * a for a in left))
        right_norm = math.sqrt(sum(b * b for b in right))
        return dot / (left_norm * right_norm)

    scores = [cosine(query_vector, vector) for vector in document_vectors]
    print(f"\n[EMBEDDINGS] cosine scores={[round(s, 6) for s in scores]}")
    assert scores[0] > scores[1], (
        f"retrieval ordering is inverted (cardiology={scores[0]:.4f}, "
        f"dermatology={scores[1]:.4f}). Either the task types are mismatched across the "
        f"query/document boundary or the model is not behaving as documented in "
        f"docs/adr/{ADR}."
    )
