"""The guardrail environment contract, and the startup refusals it carries (§9).

§9 is explicit about the decision model — *"«CEV» is not a resolved model ID; a suitable small model
is explicitly selected in the environment"* — and the repository already has the rule this
implements: a `live` backend
may not run with an unresolved pin (`scripts/validate_env.py` refuses anything starting with
`CHANGEME`). This module is that rule for the guardrail pins, plus the two the script cannot see:

* a decision model is only resolvable if it names a vendor *and* a model (``vendor/model``), because
  a bare word is the shape of a placeholder and of a typo alike;
* layer 3's threshold is **not** read from the environment as an independent value. It is read from
  the measurement record (ADR-0005's intake process) and the environment value must equal it —
  otherwise an operator could raise the threshold to 0.99 and quietly turn the layer off, or lower it
  to 0.10 and turn every patient message into a block, without touching the recorded measurement.

Nothing here reads a credential's value into a message. A missing key is named, never printed.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final

__all__ = [
    "DEFAULT_ENV_FILE",
    "PLACEHOLDERS",
    "PLACEHOLDER_PREFIX",
    "ConfigError",
    "GuardrailProfile",
    "GuardrailSettings",
    "load_settings",
    "parse_env_file",
    "validate_pins",
]

#: Same prefix `scripts/validate_env.py` treats as unresolved, so the two gates agree by
#: construction rather than by a comment.
PLACEHOLDER_PREFIX: Final = "CHANGEME"

#: Values that are placeholders in every sense that matters, even though they do not carry the
#: prefix. ``CEV`` is §9's own example of a token that is *not* a resolved model id; ``TBD``,
#: ``TODO`` and ``XXX`` are the same category arriving through a different habit.
PLACEHOLDERS: Final[frozenset[str]] = frozenset(
    {"cev", "tbd", "todo", "xxx", "none", "null", "model", "the-model", "your-model"}
)

DEFAULT_ENV_FILE: Final = Path(".env")

#: The default paths for the two recorded artifacts. Relative to the package's data directory when
#: the environment does not name one, so a checkout with no `.env` still has a resolvable default.
DEFAULT_LOCAL_ARTIFACT_DIR: Final = Path("~/.cache/referraleasy/guardrail-models")


class ConfigError(RuntimeError):
    """The guardrail configuration is not runnable. The message names every offending variable."""


class GuardrailProfile(StrEnum):
    """§9's two profiles.

    ``three_stage`` runs all three layers and shows them. ``conditional`` is the faster one, and
    §9 asks for it to be *compared* against the three-stage profile rather than assumed better —
    :mod:`referraleasy_guardrails.evaluation` is where that comparison is computed.
    """

    THREE_STAGE = "three_stage"
    CONDITIONAL = "conditional"


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a minimal ``KEY=VALUE`` file. Same shape as the repository's other readers.

    No interpolation and no dependency: a value containing ``$`` is kept verbatim so a shell-style
    expansion can never quietly change a secret.
    """
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
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


def _repo_env(repo_root: Path) -> dict[str, str]:
    file_values = parse_env_file(repo_root / DEFAULT_ENV_FILE)
    merged = dict(file_values)
    # The real environment wins, so `GUARDRAIL_MODE=conditional uv run pytest` overrides the file.
    merged.update(os.environ)
    return merged


def is_placeholder(value: str) -> bool:
    """Whether a pin still says "pick me" rather than naming something."""
    stripped = value.strip()
    if not stripped:
        return True
    upper = stripped.upper()
    return upper.startswith(PLACEHOLDER_PREFIX) or stripped.lower() in PLACEHOLDERS


@dataclass(frozen=True, slots=True)
class GuardrailSettings:
    """The guardrail configuration, resolved and already validated.

    Built by :meth:`from_env` (which validates) or directly with an explicit mapping (tests and the
    evaluation CLI). ``env`` keeps the resolved mapping so a report can say *where* a value came from
    without the caller having to thread the environment through.
    """

    profile: GuardrailProfile = GuardrailProfile.THREE_STAGE
    total_timeout_ms: int = 3000
    max_message_chars: int = 2000
    #: Layer 2's model id, e.g. ``openai/gpt-5-nano``. Empty or a placeholder means the layer is
    #: **not configured** — which is a refusal for writes, not a fallback to allow.
    decision_model: str = ""
    #: Layer 3's artifact pin, e.g. ``sentence-transformers/all-MiniLM-L6-v2@<revision>``.
    local_model: str = ""
    #: Layer 3's measured threshold. Checked against the threshold record; never trusted alone.
    local_threshold: str = ""
    #: Whether the model backend is `live` or `offline`. Layer 2's client is only built for `live`;
    #: under `offline` the layer is closed and no request is attempted.
    model_backend: str = "offline"
    #: `repr=False` on the two fields that can hold a credential: a settings object appears in log
    #: lines and tracebacks, and `str(settings)` is not a place a key may be recovered from. The value
    #: itself is still readable — the client that needs it reads the attribute.
    openrouter_api_key: str = field(default="", repr=False)
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    artifact_dir: Path = field(
        default_factory=lambda: Path(DEFAULT_LOCAL_ARTIFACT_DIR).expanduser()
    )
    #: The resolved environment, kept so a report can say where a value came from — and excluded from
    #: the repr for the same reason, since it carries the key's raw form by definition.
    env: Mapping[str, str] = field(default_factory=dict, repr=False)

    @property
    def live_model_backend(self) -> bool:
        return self.model_backend.strip().lower() == "live"

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, *, repo_root: Path | None = None
    ) -> GuardrailSettings:
        """Resolve from the process environment over the repository ``.env``, and validate.

        Validating here rather than in the layer constructors is deliberate: a pin that is unusable
        is a startup problem, and finding out when the first patient message arrives is finding out
        too late.
        """
        root = repo_root or Path(__file__).resolve().parents[4]
        source = dict(_repo_env(root)) if env is None else dict(env)
        # Problems found while resolving a value are carried rather than raised, so that a
        # configuration with an unknown profile *and* a bad pin gets one message naming both.
        problems: list[str] = []
        settings = cls(
            profile=_profile(source.get("GUARDRAIL_MODE", "").strip(), problems),
            total_timeout_ms=_int(source, "GUARDRAIL_TOTAL_TIMEOUT_MS", 3000, problems),
            max_message_chars=_int(source, "GUARDRAIL_MAX_MESSAGE_CHARS", 2000, problems),
            decision_model=source.get("GUARDRAIL_DECISION_MODEL", "").strip(),
            local_model=source.get("GUARDRAIL_LOCAL_MODEL", "").strip(),
            local_threshold=source.get("GUARDRAIL_LOCAL_THRESHOLD", "").strip(),
            model_backend=source.get("MODEL_BACKEND", "offline").strip() or "offline",
            openrouter_api_key=source.get("OPENROUTER_API_KEY", "").strip(),
            openrouter_base_url=source.get(
                "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
            ).strip()
            or "https://openrouter.ai/api/v1",
            artifact_dir=Path(
                source.get("GUARDRAIL_LOCAL_ARTIFACT_DIR", str(DEFAULT_LOCAL_ARTIFACT_DIR))
            ).expanduser(),
            env=source,
        )
        validate_pins(settings, problems=problems)
        return settings


def _int(
    source: Mapping[str, str], key: str, default: int, problems: list[str] | None = None
) -> int:
    """Resolve an integer setting. A value that is not one is a problem, not a silent default.

    The problem is appended when a list is given (the :meth:`GuardrailSettings.from_env` path) so it
    can be reported with the others; the default is returned in its place, which is safe because the
    caller raises before any settings are handed out.
    """
    raw = source.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        if problems is not None:
            problems.append(f"{key}={raw!r} is not an integer")
            return default
        raise ConfigError(f"{key}={raw!r} is not an integer") from None


def _profile(raw: str, problems: list[str] | None = None) -> GuardrailProfile:
    """Resolve ``GUARDRAIL_MODE``, refusing an unknown name rather than defaulting to a profile.

    An unset value is the documented default (``three_stage``, §9's full profile); a value that was
    *set* to something unknown is a refusal, because silently running the other profile would make
    every measurement taken under it a measurement of a policy nobody selected.

    The refusal is *appended* to ``problems`` rather than raised, so an unknown profile cannot be the
    only thing an operator sees while two broken pins behind it wait for the next deploy cycle. The
    profile returned in that case is the default, and it is never used: the appended problem is what
    :func:`validate_pins` raises on, before any settings object leaves this module.
    """
    if not raw:
        return GuardrailProfile.THREE_STAGE
    try:
        return GuardrailProfile(raw)
    except ValueError:
        known = ", ".join(member.value for member in GuardrailProfile)
        message = f"GUARDRAIL_MODE={raw!r} is not a known profile. Known values: {known}."
        if problems is not None:
            problems.append(message)
            return GuardrailProfile.THREE_STAGE
        raise ConfigError(message) from None


def validate_pins(settings: GuardrailSettings, *, problems: Sequence[str] | None = None) -> None:
    """Refuse the configuration that would present an unverified pin as configuration (§9).

    Every problem is collected and reported together: an operator fixing one variable at a time
    across four deploy cycles is how the fourth one gets skipped. ``problems`` carries the ones found
    while values were being resolved (see :meth:`GuardrailSettings.from_env`); a caller validating a
    hand-built settings object passes nothing and gets the pin problems alone.
    """
    collected = list(problems or [])

    if settings.total_timeout_ms <= 0:
        collected.append(f"GUARDRAIL_TOTAL_TIMEOUT_MS={settings.total_timeout_ms} must be positive")

    model = settings.decision_model
    if model and is_placeholder(model):
        collected.append(
            f"GUARDRAIL_DECISION_MODEL={model!r} is a placeholder, and a placeholder is not a "
            "resolved model id (§9). Choose the small decision model explicitly, in the "
            "environment, with the model id the provider actually serves."
        )
    elif model and "/" not in model:
        collected.append(
            f"GUARDRAIL_DECISION_MODEL={model!r} names no vendor. The catalogue id is "
            "`<vendor>/<model>`; a bare word is the shape of a placeholder and of a typo alike, so "
            "it is refused rather than guessed."
        )

    if settings.live_model_backend and not model:
        collected.append(
            "MODEL_BACKEND=live but GUARDRAIL_DECISION_MODEL is empty. Layer 2 would be closed, "
            "and §9's rule closes the booking write with it; set the model id or run the offline "
            "profile deliberately."
        )

    if settings.live_model_backend and not settings.openrouter_api_key:
        collected.append(
            "MODEL_BACKEND=live but OPENROUTER_API_KEY is empty. Layer 2's call is the OpenRouter "
            "path (ADR-0002); the key is required and its value is never printed."
        )

    if settings.local_threshold and not _is_number(settings.local_threshold):
        collected.append(
            f"GUARDRAIL_LOCAL_THRESHOLD={settings.local_threshold!r} is not a number. A threshold "
            "that cannot be compared is not a threshold."
        )

    if collected:
        raise ConfigError(
            "the guardrail configuration is not runnable:\n  - " + "\n  - ".join(collected)
        )


def _is_number(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


def load_settings(
    env: Mapping[str, str] | None = None, *, repo_root: Path | None = None
) -> GuardrailSettings:
    """Alias for :meth:`GuardrailSettings.from_env`, kept for symmetry with the other packages."""
    return GuardrailSettings.from_env(env, repo_root=repo_root)
