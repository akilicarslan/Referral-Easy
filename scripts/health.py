#!/usr/bin/env python3
"""`make health` — probe every service the running profile declares.

This used to be a Phase 0 stub that probed nothing and exited 3. It is real now, and the reason it
was a stub is worth keeping in mind: a health check that prints "OK" without probing anything is the
most damaging kind of fabricated demo output, because health is exactly the thing a viewer trusts
without checking. So every line below comes from a request that was actually made or a connection
that was actually opened.

What it does *not* do is guess. A service that is not running is reported `DOWN` — that is a true
statement about the stack, not an error in this script. A service in the active profile that has no
probe defined here is reported `NO PROBE` rather than counted as healthy, so the summary can never
claim more coverage than it has.

Exit codes
    0  every service in the profile answered
    1  the environment is not runnable (reported by scripts/validate_env.py)
    2  at least one service did not answer

Usage
    python scripts/health.py                 # probe the `core` profile
    python scripts/health.py --profile showcase
    python scripts/health.py --profile core --profile showcase
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Final

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

EXIT_ENV_INVALID = 1
EXIT_UNHEALTHY = 2

#: Seconds. Short on purpose: a health check that hangs is indistinguishable from a hung service.
TIMEOUT: Final = 5.0

OK = "OK"
DOWN = "DOWN"
NO_PROBE = "NO PROBE"


@dataclass(frozen=True)
class Probe:
    """One service, the profile that starts it, and how to reach it from the host."""

    service: str
    profile: str
    #: Environment variable holding the host-reachable URL, or None when derived/unknown.
    url_env: str | None
    what: str
    #: Env var that overrides this probe's address outright.
    #:
    #: It exists so every probe can be pointed somewhere else, which is what makes the probes
    #: testable: without it, the collector and Data Prepper probes read a fixed port and would
    #: report OK on a machine where the stack is down, so "nothing running" could not be simulated
    #: and the CI check that asserts it would be passing for a reason nobody had verified.
    health_url_env: str | None = None


PROBES: tuple[Probe, ...] = (
    Probe("postgres", "core", "DATABASE_URL", "connect + applied migration count"),
    Probe("opensearch", "core", "OPENSEARCH_URL", "GET /_cluster/health"),
    Probe(
        "otel-collector",
        "core",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "GET :13133/health/status",
        "OTEL_COLLECTOR_HEALTH_URL",
    ),
    Probe("data-prepper", "core", None, "GET :4900/metrics/prometheus", "DATA_PREPPER_HEALTH_URL"),
    Probe("gateway", "core", "GATEWAY_URL", "GET /healthz"),
    Probe("mock-api", "core", "MOCK_API_URL", "GET /health"),
    Probe("web", "showcase", "WEB_URL", "GET /"),
)


def _read_dotenv() -> dict[str, str]:
    """`.env` layered under the process environment, without a dependency.

    Deliberately does not write into `os.environ`: a script that mutates the process environment
    leaks that change into anything it calls afterwards, which is how a spike suite once broke an
    unrelated package's host resolution (see `tests/contracts/spikes/_spike_env.py`).
    """
    values: dict[str, str] = {}
    for path in (REPO_ROOT / ".env", REPO_ROOT / ".env.example"):
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key and key not in values:
                values[key] = value
    return {**values, **{k: v for k, v in os.environ.items() if v}}


def _http(url: str) -> tuple[bool, str]:
    """GET `url`, returning (answered, detail). A non-2xx is answered-but-unhealthy."""
    request = urllib.request.Request(url, headers={"accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read(400).decode("utf-8", "replace").replace("\n", " ")
            return True, f"{response.status} {body[:120]}"
    except urllib.error.HTTPError as exc:
        return True, f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return False, type(exc).__name__


def _probe_postgres(dsn: str) -> tuple[bool, str]:
    try:
        import psycopg
    except ImportError:  # pragma: no cover - the root dev group installs it
        return False, "psycopg not installed"

    try:
        with psycopg.connect(dsn, connect_timeout=int(TIMEOUT)) as connection:
            row = connection.execute("SELECT count(*) FROM schema_migrations").fetchone()
    # Broad on purpose: any failure at all means "not answering", and enumerating driver
    # exceptions here would be a list to keep in sync with psycopg.
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:100]}"

    # The migration count is the part worth reporting: a reachable database with an unapplied schema
    # answers every connection and fails every query, which is the worst of both.
    return True, f"connected, {row[0] if row else 0} migration(s) applied"


def _host_url(configured: str, *, fallback: str | None) -> str:
    """Swap a compose service name for a host-reachable address.

    The shipped `.env` names services the way a container reaches them (`http://opensearch:9200`),
    which does not resolve on the host. This is the same problem `referraleasy_rag.config` solves
    with its host-fallback list, and it is solved here rather than in the committed file so the
    file stays correct for containers.
    """
    if fallback is None:
        return configured
    for container_host in (
        "opensearch",
        "otel-collector",
        "data-prepper",
        "postgres",
        "gateway",
        "mock-api",
        "web",
    ):
        if f"//{container_host}:" in configured:
            return configured.replace(f"//{container_host}:", "//localhost:")
    return configured


#: The path each service's health answer lives at, when the value in `.env` is a base URL.
HEALTH_PATH_BY_SERVICE: Final[dict[str, str]] = {
    "opensearch": "/_cluster/health",
    "gateway": "/healthz",
    "mock-api": "/health",
    "web": "/",
}


def probe_url(probe: Probe, env: dict[str, str]) -> str | None:
    """The exact URL this probe will contact, or None when it cannot be addressed at all.

    Split out of :func:`_probe` so that there is **one** resolution rule rather than two. `make demo`
    prints the demo's entry points, and it is only allowed to print the ones that answered — so it
    has to name the same addresses this module actually contacted. A second, parallel mapping in the
    demo script would be a second answer to "where is the gateway", and the one that drifts is the
    one a viewer reads.

    None means "this probe has no addressable form" — an unset environment variable, or a service
    whose check is a raw TCP connection rather than a URL (`postgres`, which is probed through the
    driver).
    """
    if probe.service == "postgres":
        # Probed by opening a connection with the driver, so there is no URL a viewer could click.
        return None

    if probe.health_url_env and env.get(probe.health_url_env):
        return env[probe.health_url_env]

    if probe.service == "data-prepper":
        return "http://localhost:4900/metrics/prometheus"

    if probe.service == "otel-collector":
        # The health extension listens on 13133 while the OTLP receiver is on 4318; the configured
        # endpoint names the receiver, so only the host is taken from it.
        configured = env.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
        host = configured.split("//", 1)[-1].split(":", 1)[0]
        if host == "otel-collector":
            host = "localhost"
        return f"http://{host}:13133/health/status"

    configured = env.get(probe.url_env or "", "")
    if not configured:
        return None
    url = _host_url(configured, fallback="localhost")
    return url.rstrip("/") + HEALTH_PATH_BY_SERVICE[probe.service]


def _probe(probe: Probe, env: dict[str, str]) -> tuple[str, str]:
    """Return (status, detail) for one service."""
    if probe.service == "postgres":
        dsn = env.get("DATABASE_URL", "")
        if not dsn:
            return NO_PROBE, "DATABASE_URL is not set"
        return _status(*_probe_postgres(dsn))

    url = probe_url(probe, env)
    if url is None:
        return NO_PROBE, f"{probe.url_env} is not set"
    return _status(*_http(url))


def _status(answered: bool, detail: str) -> tuple[str, str]:
    return (OK if answered else DOWN, detail)


def run_env_validation() -> int:
    return subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "validate_env.py")],
        check=False,
        cwd=REPO_ROOT,
    ).returncode


def _compose_running() -> set[str]:
    """Service names compose reports as running, or an empty set if compose cannot be asked."""
    try:
        completed = subprocess.run(
            ["docker", "compose", "ps", "--format", "json", "--all"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=REPO_ROOT,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if completed.returncode != 0:
        return set()
    names: set[str] = set()
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            names.add(json.loads(line).get("Service", ""))
        except json.JSONDecodeError:
            continue
    return names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe the running stack's healthchecks.")
    parser.add_argument(
        "--profile",
        action="append",
        default=None,
        choices=["core", "showcase", "dashboards"],
        help="compose profile to probe; repeatable. Defaults to core.",
    )
    args = parser.parse_args(argv)
    profiles = set(args.profile or ["core"])

    print("referraleasy health")
    print("=" * 72)
    if run_env_validation() != 0:
        print(
            "\nSTOPPED: the environment is not runnable, so a healthcheck would not mean anything.",
            file=sys.stderr,
        )
        return EXIT_ENV_INVALID

    env = _read_dotenv()
    running = _compose_running()

    probeable = tuple(p for p in PROBES if p.profile in profiles)
    print(f"\nProfiles: {', '.join(sorted(profiles))}")
    print(
        f"Compose reports running: {', '.join(sorted(running)) or '(nothing / compose unavailable)'}\n"
    )

    unhealthy: list[str] = []
    unprobed: list[str] = []
    print(f"  {'service':<16} {'status':<10} detail")
    print(f"  {'-' * 16} {'-' * 10} {'-' * 42}")
    for probe in probeable:
        status, detail = _probe(probe, env)
        if status == NO_PROBE:
            unprobed.append(probe.service)
        elif status != OK:
            unhealthy.append(probe.service)
        print(f"  {probe.service:<16} {status:<10} {detail}")

    print()
    if unhealthy:
        print(
            f"{len(unhealthy)} service(s) did not answer: {', '.join(unhealthy)}.\n"
            "A service outside the profile you started will show DOWN - that is a true statement "
            "about the stack, not a failure of this script.",
            file=sys.stderr,
        )
        return EXIT_UNHEALTHY

    if unprobed:
        # Not a failure, but not a clean bill of health either: these were never contacted, so the
        # summary must not imply they were.
        print(f"NOT PROBED (no probe defined, so NOT counted as healthy): {', '.join(unprobed)}")

    print(f"All {len(probeable) - len(unprobed)} probed service(s) answered.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
