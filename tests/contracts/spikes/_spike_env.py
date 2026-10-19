"""Shared requirement checks for the Phase 0 provider spikes.

Policy — read before changing anything in this directory
--------------------------------------------------------
Phase 0 is complete only when every `CHANGEME_*` pin in `.env.example` has been
resolved by a spike that ends in a dated ADR plus a **passing** smoke test.

A spike therefore has exactly two honest outcomes:

* it **runs for real**, or
* it **fails loudly**, naming the environment variable that is missing and what
  would resolve it.

It must never *skip*. A skipped test is reported as neither pass nor fail, so a
green build would hide the fact that Phase 0 never completed — which is the one
outcome this suite exists to prevent. Hence `pytest.fail`, never
`pytest.skip` / `pytest.importorskip`, anywhere in this directory.

Import discipline: third-party SDKs are imported **inside** the test body, after
the requirement check. That way `pytest` can always collect this directory (the
default run deselects spikes with `-m 'not spike'` but still imports the modules)
without any optional dependency installed.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DOTENV_PATH = REPO_ROOT / ".env"
DOTENV_EXAMPLE_PATH = REPO_ROOT / ".env.example"
DOTENV_CANDIDATES: tuple[Path, ...] = (DOTENV_PATH, DOTENV_EXAMPLE_PATH)

PLACEHOLDER_PREFIX = "CHANGEME"

_ADR_DIR = "docs/adr"


def _classify(value: str | None) -> str:
    """One of: 'missing', 'placeholder', 'ok'."""
    if value is None or not value.strip():
        return "missing"
    if value.strip().startswith(PLACEHOLDER_PREFIX):
        return "placeholder"
    return "ok"


def _blocked_message(
    name: str,
    kind: str,
    *,
    purpose: str,
    where: str,
    adr: str,
    extra: str = "",
) -> str:
    value = os.environ.get(name, "")
    headline = (
        f"{name} is not set (or is empty)."
        if kind == "missing"
        else f"{name} is still a placeholder: {name}={value}"
    )
    placeholder_note = (
        """
  A placeholder is NOT real configuration. Phase 0 requires every pin to be
  resolved by a spike that ends in a dated ADR; until then the project's rules
  forbid presenting this value as if it were real.
"""
        if kind == "placeholder"
        else ""
    )
    return f"""
SPIKE BLOCKED — {headline}
{placeholder_note}
  Purpose : {purpose}
  Where   : {where}
  ADR     : {_ADR_DIR}/{adr}
  Env files consulted : {", ".join(ensure_env_loaded()) or "none found"}
                        (real environment variables always win; .env beats .env.example)
{extra}
  This test FAILS rather than skips, on purpose: a skipped spike is invisible in
  CI and would hide the fact that Phase 0 is still open.

  Re-run once the value exists:  make spike
""".strip()


def require_env(
    name: str,
    *,
    purpose: str,
    where: str,
    adr: str,
    extra: str = "",
) -> str:
    """Return `name` from the environment, or fail with an actionable message.

    Also fails when the value is a `CHANGEME` placeholder, because a placeholder
    passed to a provider produces a confusing 401 rather than a statement of the
    real problem.
    """
    # A spike is actually running now, so this is the moment to read `.env` into the process.
    # See `ensure_env_loaded` for why it must not happen when the module is merely imported.
    ensure_env_loaded()
    kind = _classify(os.environ.get(name))
    if kind != "ok":
        pytest.fail(
            _blocked_message(name, kind, purpose=purpose, where=where, adr=adr, extra=extra),
            pytrace=False,
        )
    return os.environ[name].strip()


def require_module(dotted: str, *, distribution: str, adr: str) -> ModuleType:
    """Import an optional SDK, failing loudly (never skipping) when absent."""
    try:
        return importlib.import_module(dotted)
    except ImportError as exc:  # pragma: no cover - depends on the environment
        pytest.fail(
            f"""
SPIKE BLOCKED — the SDK for this spike is not installed: cannot import {dotted!r}
  ({exc})

  Expected package : {distribution}
  ADR              : {_ADR_DIR}/{adr}

  A spike is meant to run *before* the services that will depend on this package
  exist, so it does not require the workspace to be fully scaffolded. Run it
  standalone, with the pinned distribution pulled in for this command only:

      uv run --with {distribution} python -m pytest tests/contracts/spikes -m spike

  Note the `python -m pytest` form: invoking the bare `pytest` entry point does
  not pick up a `--with` overlay, and would report this same missing import.
  (Measured: uv 0.5.20.)

  Or install the whole workspace:

      uv sync --all-packages && pnpm install

  This test FAILS rather than skips: an uninstalled dependency must not be able
  to masquerade as a passing Phase 0.
""".strip(),
            pytrace=False,
        )
        raise  # unreachable; keeps type checkers happy


def isolated_spike_run(body: str) -> str:
    """Format the ready-to-copy command shown when a spike is blocked."""
    return f"\n  Isolated re-run:\n\n{body}\n"


def load_env_files() -> tuple[str, ...]:
    """Load `<repo>/.env` then `<repo>/.env.example` into the process environment.

    The Makefile targets (`make spike`) run plain `uv run pytest`, which reads no
    env file on its own. Precedence is real-environment > `.env` > `.env.example`,
    so a CI or shell export can never be silently overridden by a stale file, and a
    local `.env` is never overridden by the example.

    The `.env.example` fallback exists so a spike run on a fresh clone still
    *attempts* something real (reach `OPENSEARCH_URL`, load a fixture secret) and
    fails with a connection or credential error, rather than claiming the project's
    own documented variables are unset.

    Minimal by design: no dependency, and no interpolation - a value containing `$`
    is left exactly as written.
    """
    sources: list[str] = []
    for path in DOTENV_CANDIDATES:
        if not path.is_file():
            continue
        sources.append(path.name)
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value
    return tuple(sources)


_ENV_SOURCES: tuple[str, ...] | None = None


def ensure_env_loaded() -> tuple[str, ...]:
    """Copy `.env` into `os.environ` once, on first use.

    **This is deliberately not done at import.** It used to be, and the side effect escaped the
    spike suite entirely: pytest imports every module it collects even when the tests are deselected,
    so `pytest` with the default `-m 'not spike and not e2e'` still ran this at *collection* and
    wrote `OPENSEARCH_URL=http://opensearch:9200` into the process environment.

    That is not a harmless extra variable. `referraleasy_rag.config` treats a value present in the
    process environment as an explicit choice and then probes **only** that URL, dropping the
    host-fallback list it would otherwise use. So a compose service name, correct inside the
    network and unresolvable on the host, became the only candidate — and twelve RAG integration
    tests that pass when `tests/rag` is run alone silently skipped in the full run. The failure
    pointed nowhere near its cause: a deselected suite changing global process state.

    Lazy loading keeps the spikes' own behaviour (they still see `.env` when they run) while making
    the module inert to anything that merely imports it.
    """
    global _ENV_SOURCES
    if _ENV_SOURCES is None:
        _ENV_SOURCES = load_env_files()
    return _ENV_SOURCES


def openrouter_model_id(raw: str) -> str:
    """Normalise a configured model id into the string LiteLLM expects.

    `AGENT_MODEL_ID` is stored as a bare model id (`google/<model>`), while the
    LiteLlm path needs the provider prefix (`openrouter/google/<model>`). This is
    an explicit, documented normalisation — not a hidden default.
    """
    value = raw.strip()
    return value if value.startswith("openrouter/") else f"openrouter/{value}"
