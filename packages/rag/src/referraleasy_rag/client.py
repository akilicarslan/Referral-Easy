"""OpenSearch client construction and reachability probing.

The `.env` in this repository points `OPENSEARCH_URL` at the compose service name
(`http://opensearch:9200`), which resolves inside the compose network and nowhere else. A
process on the host that used it verbatim would report "OpenSearch unavailable" while a healthy
node listened on the published port — and would then take the seed rung for a reason that has
nothing to do with the data.

So the URL is *probed*: candidates are tried in order, the first one that answers is used, and
the outcome is printed. When `OPENSEARCH_URL` is set in the real environment (a container, CI,
a deployment) the candidate list holds exactly that one URL and no fallback happens — an
explicitly configured endpoint is never silently replaced.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import httpx
from opensearchpy import OpenSearch

from referraleasy_rag.config import Settings


@dataclass(frozen=True, slots=True)
class ProbeAttempt:
    """One candidate URL and what happened to it."""

    url: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """The outcome of probing every candidate."""

    url: str | None
    attempts: tuple[ProbeAttempt, ...]
    cluster_name: str = ""
    version: str = ""

    @property
    def reachable(self) -> bool:
        return self.url is not None

    @property
    def used_fallback(self) -> bool:
        return self.reachable and bool(self.attempts) and self.attempts[0].url != self.url

    def lines(self) -> list[str]:
        lines = []
        for attempt in self.attempts:
            mark = "ok" if attempt.ok else "unreachable"
            detail = f" ({attempt.detail})" if attempt.detail else ""
            lines.append(f"  {attempt.url}: {mark}{detail}")
        return lines


def probe_opensearch(
    settings: Settings,
    *,
    timeout_seconds: float = 3.0,
    candidates: Sequence[str] | None = None,
    client: httpx.Client | None = None,
) -> ProbeResult:
    """Return the first reachable OpenSearch URL, with a record of every attempt."""
    urls = tuple(candidates) if candidates is not None else settings.opensearch_url_candidates
    if not urls:
        urls = (settings.opensearch_url,)
    attempts: list[ProbeAttempt] = []
    for url in urls:
        try:
            if client is not None:
                response = client.get(url, timeout=timeout_seconds)
            else:
                response = httpx.get(url, timeout=timeout_seconds)
        except httpx.HTTPError as exc:
            attempts.append(ProbeAttempt(url=url, ok=False, detail=type(exc).__name__))
            continue
        if response.status_code >= 400:
            attempts.append(ProbeAttempt(url=url, ok=False, detail=f"HTTP {response.status_code}"))
            continue
        try:
            body = response.json()
        except ValueError:
            attempts.append(ProbeAttempt(url=url, ok=False, detail="response was not JSON"))
            continue
        cluster = ""
        version = ""
        if isinstance(body, dict):
            cluster = str(body.get("cluster_name", ""))
            version_node = body.get("version")
            if isinstance(version_node, dict):
                version = str(version_node.get("number", ""))
        attempts.append(ProbeAttempt(url=url, ok=True, detail=cluster))
        return ProbeResult(url=url, attempts=tuple(attempts), cluster_name=cluster, version=version)
    return ProbeResult(url=None, attempts=tuple(attempts))


def build_client(settings: Settings, *, url: str | None = None) -> OpenSearch:
    """Build the OpenSearch client for a probed URL.

    The client is constructed with retries off and a bounded timeout: a retrieval request that
    hangs is worse than one that fails fast and takes the seed rung, because the caller has a
    human waiting and a fallback ready.
    """
    return OpenSearch(
        hosts=[url or settings.opensearch_url],
        timeout=settings.request_timeout_seconds,
        max_retries=0,
        retry_on_timeout=False,
    )


__all__ = ["ProbeAttempt", "ProbeResult", "build_client", "probe_opensearch"]
