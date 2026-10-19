"""Configuration for the directory RAG package.

Everything is read from the process environment first and from the repository `.env` second.
The process wins, so `OPENSEARCH_URL=http://localhost:9200 uv run pytest` overrides whatever
the file says without editing it.

No credential is ever logged, printed or defaulted to a literal. If a required one is absent,
the code that needs it raises rather than pretending to work.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

#: Fallback OpenRouter catalogue id for the embedding model. OpenRouter names models
#: `vendor/model`; the `.env` pin (`EMBEDDING_MODEL_ID`) is the Vertex catalogue id, so the
#: two are not interchangeable strings. See ADR-0029.
DEFAULT_OPENROUTER_EMBEDDING_MODEL = "google/gemini-embedding-001"

#: OpenRouter's API root. The embeddings and rerank endpoints hang off it.
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

#: Locally reachable OpenSearch addresses, tried only when `OPENSEARCH_URL` came from `.env`.
#: The shipped `.env` points at the compose service name (`http://opensearch:9200`), which does
#: not resolve on the host; the published port does. Real-environment runs override the variable
#: and get exactly the URL they asked for, with no fallback.
HOST_FALLBACK_OPENSEARCH_URLS: tuple[str, ...] = (
    "http://localhost:9200",
    "http://127.0.0.1:9200",
)

DEFAULT_INDEX_NAME = "referraleasy-directory-chunks"


class ConfigError(RuntimeError):
    """The environment is not usable. Raised with a message that names the variable."""


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a dotenv file into a mapping.

    Deliberately small: `KEY=VALUE` per line, `#` comments, optional surrounding quotes.
    It is not a general dotenv implementation and does not need to be — the file is the one
    committed with this repository plus its `.env.example` twin.
    """
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def repo_root() -> Path:
    """The repository root, derived from this file's location (never from the cwd)."""
    return Path(__file__).resolve().parents[4]


@dataclass(frozen=True, slots=True)
class Settings:
    """Resolved settings. One object, passed explicitly, never read from globals."""

    repo_root: Path
    fixtures_dir: Path
    env_file: Path
    env: Mapping[str, str] = field(default_factory=dict)

    opensearch_url: str = "http://localhost:9200"
    opensearch_url_candidates: tuple[str, ...] = ()
    opensearch_url_is_explicit: bool = False
    index_name: str = DEFAULT_INDEX_NAME

    embedding_provider: str = "vertex"
    embedding_backend: str = "live"
    embedding_model_id: str = "gemini-embedding-001"
    embedding_dimensions: int = 768
    openrouter_embedding_model: str = DEFAULT_OPENROUTER_EMBEDDING_MODEL
    openrouter_base_url: str = DEFAULT_OPENROUTER_BASE_URL
    openrouter_api_key: str | None = None

    rerank_provider: str = "openrouter"
    rerank_backend: str = "live"
    rerank_model_id: str = "cohere/rerank-v3.5"

    #: Retrieval shape. Both arms take the same width by construction.
    arm_top_k: int = 20
    rerank_candidates: int = 10
    result_top_k: int = 5
    min_result_top_k: int = 3
    rrf_k: int = 60

    request_timeout_seconds: float = 60.0
    embedding_batch_size: int = 32

    @property
    def embeddings_are_offline(self) -> bool:
        """True when the environment asks for deterministic local vectors instead of a model."""
        return self.embedding_backend.strip().lower() == "offline"

    @property
    def seed_path(self) -> Path:
        """The committed seed corpus that backs the last rung of the ladder."""
        return seed_path()

    def with_opensearch_url(self, url: str) -> Settings:
        """Copy of these settings pinned to one already-probed URL."""
        return replace(self, opensearch_url=url, opensearch_url_candidates=(url,))


def _env_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - defensive, mirrors a config typo
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc


def load_settings(
    *,
    repo_root_override: Path | None = None,
    env_file: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Build :class:`Settings` from the process environment layered over `.env`."""
    root = repo_root_override or repo_root()
    process_env: Mapping[str, str] = os.environ if environ is None else environ
    dotenv_path = env_file or (root / ".env")
    file_env = parse_env_file(dotenv_path)
    merged: dict[str, str] = {**file_env, **{k: v for k, v in process_env.items() if v != ""}}

    explicit_url = process_env.get("OPENSEARCH_URL", "").strip()
    file_url = file_env.get("OPENSEARCH_URL", "").strip()
    base_url = explicit_url or file_url or "http://localhost:9200"
    if explicit_url:
        candidates: tuple[str, ...] = (explicit_url,)
    else:
        seen: list[str] = []
        for candidate in (base_url, *HOST_FALLBACK_OPENSEARCH_URLS):
            if candidate not in seen:
                seen.append(candidate)
        candidates = tuple(seen)

    model_id = merged.get("EMBEDDING_MODEL_ID", "gemini-embedding-001").strip()
    # OpenRouter ids are namespaced (`vendor/model`). If the pin already looks like one, honour
    # it; otherwise fall back to the OpenRouter catalogue id for the same model family.
    openrouter_model = merged.get("OPENROUTER_EMBEDDING_MODEL_ID", "").strip() or (
        model_id if "/" in model_id else DEFAULT_OPENROUTER_EMBEDDING_MODEL
    )

    return Settings(
        repo_root=root,
        fixtures_dir=Path(merged.get("DIRECTORY_FIXTURES_DIR", str(root / "fixtures"))),
        env_file=dotenv_path,
        env=merged,
        opensearch_url=base_url,
        opensearch_url_candidates=candidates,
        opensearch_url_is_explicit=bool(explicit_url),
        index_name=merged.get("DIRECTORY_INDEX_NAME", DEFAULT_INDEX_NAME),
        embedding_provider=merged.get("EMBEDDING_PROVIDER", "vertex"),
        embedding_backend=merged.get("EMBEDDING_BACKEND", "live"),
        embedding_model_id=model_id,
        embedding_dimensions=_env_int(merged, "EMBEDDING_DIMENSIONS", 768),
        openrouter_embedding_model=openrouter_model,
        openrouter_base_url=merged.get("OPENROUTER_BASE_URL", DEFAULT_OPENROUTER_BASE_URL).rstrip(
            "/"
        ),
        openrouter_api_key=(merged.get("OPENROUTER_API_KEY") or None),
        rerank_provider=merged.get("RERANK_PROVIDER", "openrouter"),
        rerank_backend=merged.get("RERANK_BACKEND", "live"),
        rerank_model_id=merged.get("RERANK_MODEL_ID", "cohere/rerank-v3.5"),
        arm_top_k=_env_int(merged, "RAG_ARM_TOP_K", 20),
        rerank_candidates=_env_int(merged, "RAG_RERANK_CANDIDATES", 10),
        result_top_k=_env_int(merged, "RAG_RESULT_TOP_K", 5),
        rrf_k=_env_int(merged, "RAG_RRF_K", 60),
        request_timeout_seconds=float(_env_int(merged, "RAG_REQUEST_TIMEOUT_SECONDS", 60)),
        embedding_batch_size=_env_int(merged, "RAG_EMBEDDING_BATCH_SIZE", 32),
    )


def seed_path() -> Path:
    """Path of the committed, versioned seed corpus (package data, so it ships with the wheel)."""
    return Path(__file__).resolve().parent / "data" / "seed_directory.v1.json"


def eval_dataset_path() -> Path:
    """Path of the hand-written retrieval evaluation dataset (package data)."""
    return Path(__file__).resolve().parent / "data" / "retrieval_eval.json"


def require_api_key(settings: Settings, *, purpose: str) -> str:
    """Return the OpenRouter key or raise with an actionable message. Never logs the value."""
    key = settings.openrouter_api_key
    if not key:
        raise ConfigError(
            f"{purpose} needs OPENROUTER_API_KEY, which is not set in the environment or "
            f"{settings.env_file}. Set it there; it is never read from a literal in this codebase."
        )
    return key
