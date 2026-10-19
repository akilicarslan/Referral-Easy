#!/usr/bin/env python3
"""ReferralEasy environment guard - the gate in front of every demo run.

What it enforces
----------------
1. **A `live` backend must never run with an unresolved pin.** If any of
   `MODEL_BACKEND` / `EMBEDDING_BACKEND` / `RERANK_BACKEND` is `live` and *any*
   `CHANGEME_*` value is still present, this exits non-zero and names **every**
   offending variable. This is the guard that stops an unpinned model id from
   reaching a demo run.
2. **No silent defaults.** A required variable that is unset is an error, not an
   implicit `offline`. Guessing the user's intent is how a live run silently
   becomes a canned one, and how a canned run gets presented as live.
3. **A live backend must have its credentials.** The key the selected backend
   actually needs must be present and non-empty.

It is deliberately dependency-free (stdlib only) so it can run before anything is
installed, and it never imports a provider SDK.

Usage
-----
    uv run python scripts/validate_env.py [--env-file PATH] [--quiet]

Environment resolution: real environment variables always win; the file supplies
anything they do not set. With no `.env` present the documented offline default
`.env.example` is used, and that substitution is announced loudly on stdout.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"
DEFAULT_ENV_EXAMPLE = REPO_ROOT / ".env.example"

PLACEHOLDER_PREFIX = "CHANGEME"
KNOWN_BACKENDS = frozenset({"offline", "live"})
BACKEND_VARS: tuple[str, ...] = ("MODEL_BACKEND", "EMBEDDING_BACKEND", "RERANK_BACKEND")

# Which credentials each live backend actually needs, and what it needs them for.
# Names come from .env.example; the ADR reference matters because the spike that
# proves the credential works is recorded there.
CREDENTIALS: dict[str, tuple[tuple[str, str], ...]] = {
    "MODEL_BACKEND": (
        ("OPENROUTER_API_KEY", "the ADK -> LiteLlm -> OpenRouter model path (ADR-0002)"),
    ),
    "EMBEDDING_BACKEND": (
        ("VERTEX_PROJECT_ID", "Vertex AI embeddings (ADR-0006, ADR-0009)"),
        ("VERTEX_LOCATION", "the region serving the embedding model (ADR-0009)"),
    ),
    "RERANK_BACKEND": (("OPENROUTER_API_KEY", "the OpenRouter rerank endpoint (ADR-0007)"),),
}

EXIT_OK = 0
EXIT_INVALID = 1


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a minimal `KEY=VALUE` env file.

    No interpolation and no dependency: a value containing `$` is kept verbatim so
    a shell-style expansion can never quietly change a secret.
    """
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def effective_env(file_values: Mapping[str, str]) -> dict[str, str]:
    """File values overlaid by the real environment (the environment wins)."""
    merged = dict(file_values)
    merged.update({key: value for key, value in os.environ.items()})
    return merged


def classify(value: str | None) -> str:
    """Return one of `missing`, `placeholder`, `ok`."""
    if value is None or not value.strip():
        return "missing"
    if value.strip().startswith(PLACEHOLDER_PREFIX):
        return "placeholder"
    return "ok"


def find_placeholders(env: Mapping[str, str], declared: Mapping[str, str]) -> dict[str, str]:
    """Every declared or exported variable still holding a `CHANGEME_*` value."""
    candidates = set(declared) | set(env)
    return {key: env[key] for key in sorted(candidates) if classify(env.get(key)) == "placeholder"}


def validate(env: Mapping[str, str], declared: Mapping[str, str]) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Empty errors means the configuration may run."""
    errors: list[str] = []
    warnings: list[str] = []

    backends: dict[str, str] = {}
    for name in BACKEND_VARS:
        state = classify(env.get(name))
        if state == "missing":
            errors.append(
                f"{name} is not set. Refusing to assume a default: an unset backend is "
                "ambiguous, and silently choosing `offline` is how a canned run gets "
                f"presented as live. Set it explicitly (known values: "
                f"{', '.join(sorted(KNOWN_BACKENDS))})."
            )
        elif env[name].strip() not in KNOWN_BACKENDS:
            errors.append(
                f"{name}={env[name]!r} is not a known backend. Known values: "
                f"{', '.join(sorted(KNOWN_BACKENDS))}. No fallback is applied."
            )
        else:
            backends[name] = env[name].strip()

    live_backends = sorted(name for name, value in backends.items() if value == "live")
    placeholders = find_placeholders(env, declared)

    if live_backends and placeholders:
        rendered = "\n".join(f"        {name}={value}" for name, value in placeholders.items())
        errors.append(
            "PHASE 0 INCOMPLETE - "
            f"{', '.join(f'{name}=live' for name in live_backends)} but "
            f"{len(placeholders)} unresolved pin(s) remain:\n"
            f"{rendered}\n"
            "    A live backend calls a real provider, so these values would be sent "
            "upstream and a demo would present an unverified pin as configuration.\n"
            "    Resolve each pin with a Phase 0 spike (`make spike`), which ends in a "
            "dated ADR plus a passing smoke test, then re-run this check."
        )

    if live_backends and not placeholders:
        for name in live_backends:
            for variable, purpose in CREDENTIALS.get(name, ()):
                state = classify(env.get(variable))
                if state == "ok":
                    continue
                detail = "is empty" if state == "missing" else f"is a placeholder ({env[variable]})"
                errors.append(
                    f"{name}=live but {variable} {detail}. That variable is required for {purpose}."
                )

    if not live_backends and placeholders:
        warnings.append(
            f"offline profile: {len(placeholders)} unresolved pin(s) remain "
            f"({', '.join(placeholders)}). This is expected - the offline profile serves "
            "canned model/embedding/rerank responses and no offline code path reads a pin."
        )

    offline_llm = backends.get("MODEL_BACKEND") == "offline"
    tracing_on = (
        classify(env.get("LANGSMITH_ENABLED")) == "ok"
        and env["LANGSMITH_ENABLED"].strip().lower() == "true"
    )
    if offline_llm and tracing_on:
        warnings.append(
            "LANGSMITH_ENABLED=true while MODEL_BACKEND=offline. The offline profile "
            "is defined as zero network calls, so tracing will not be exported; the "
            "flag is reported here rather than silently honoured or silently ignored "
            "(ADR-0010)."
        )

    return errors, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="validate_env.py",
        description="Fail loudly when a live backend runs with unresolved pins.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help=f"env file to read (default: {DEFAULT_ENV_FILE}, else {DEFAULT_ENV_EXAMPLE})",
    )
    parser.add_argument("--quiet", action="store_true", help="only print problems")
    args = parser.parse_args(argv)

    source: Path | None = args.env_file
    if source is None:
        source = DEFAULT_ENV_FILE if DEFAULT_ENV_FILE.is_file() else DEFAULT_ENV_EXAMPLE
    if not source.is_file():
        print(f"error: env file not found: {source}", file=sys.stderr)
        print(
            "       Create one with `make env` (copies .env.example).",
            file=sys.stderr,
        )
        return EXIT_INVALID

    file_values = parse_env_file(source)
    env = effective_env(file_values)

    if not args.quiet:
        print(f"validate_env: reading {source.relative_to(REPO_ROOT)}")
        if source.name == DEFAULT_ENV_EXAMPLE.name and not DEFAULT_ENV_FILE.is_file():
            print(
                "  NOTE: no .env found - validating the documented offline default from "
                ".env.example. Create .env (`make env`) before running the demo.",
            )

    errors, warnings = validate(env, file_values)

    for warning in warnings:
        print(f"WARN: {warning}")
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)

    if errors:
        print(
            f"\nFAIL: {len(errors)} problem(s) found. Nothing was substituted; the "
            "configuration is not runnable as given.",
            file=sys.stderr,
        )
        return EXIT_INVALID

    if not args.quiet:
        selection = ", ".join(
            f"{name}={env[name].strip()}"
            for name in BACKEND_VARS
            if classify(env.get(name)) == "ok"
        )
        print(f"OK: {selection}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
