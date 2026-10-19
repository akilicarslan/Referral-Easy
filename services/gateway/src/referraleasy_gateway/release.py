"""Which release this gateway dispatches under (§13).

A task's §5 envelope carries ``release_manifest_id``, and §13 makes it load-bearing: a conversation
pinned to a release is one whose evaluation can be compared with another's, and "latest" is never
sufficient provenance for a report. The gateway is the root caller of a task, so it is the process
that names the pin.

**This is the first service that reads the registry.** Until now the manifest was built, tested and
read by `make eval` and the CI gate, and no running service consumed it — README gap #1. The
gateway reads one field of one file: the id of the most recently built release. It does not pin per
conversation yet (that is the rest of §13, and the registry's own `pinning` module), but every task
it dispatches now travels under a release that exists.

Resolution order, and the failure mode:

1. ``RELEASE_MANIFEST_ID`` in the environment — a deployment that pins explicitly.
2. ``registry/releases/latest.json`` — the manifest ``scripts/check_release_versions.py`` builds.
3. Empty — nothing to name. The dispatcher then reports itself unconfigured rather than sending a
   task with an invented id; a missing pin is a deployment fact, not a value to make up.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Final

__all__ = [
    "LATEST_MANIFEST",
    "RELEASE_MANIFEST_ID_ENV",
    "resolve_release_manifest_id",
]

#: The variable a deployment can use to pin explicitly. Same name the registry's CLI documents.
RELEASE_MANIFEST_ID_ENV: Final = "RELEASE_MANIFEST_ID"

#: `services/gateway/src/referraleasy_gateway/release.py` -> repository root in a checkout. In the
#: built image the module lives at `/app/.venv/...`, so this resolves to `/app/.venv` — the
#: Dockerfile copies `registry/` there beside `fixtures/`, for the same reason and with the same
#: caveat as the fixtures copy.
REPO_ROOT: Final = Path(__file__).resolve().parents[4]

#: The manifest `make eval` and the CI version gate read.
LATEST_MANIFEST: Final = REPO_ROOT / "registry" / "releases" / "latest.json"


def resolve_release_manifest_id(
    env: Mapping[str, str] | None = None,
    *,
    latest: Path = LATEST_MANIFEST,
) -> str:
    """The release id this process should dispatch under, or ``""`` when there is none.

    A malformed manifest is treated as no manifest: the caller reports "cannot name a release" and
    the dispatcher stays unconfigured. Raising here would take the gateway down for a file that is
    only needed when a coordinator is configured — and a gateway that cannot serve bookings because
    its release label is unreadable is the wrong failure.
    """
    explicit = (env or {}).get(RELEASE_MANIFEST_ID_ENV, "").strip()
    if explicit:
        return explicit
    try:
        document = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(document, dict):
        return ""
    value = document.get("release_manifest_id")
    return value.strip() if isinstance(value, str) else ""
