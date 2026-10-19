#!/usr/bin/env python3
"""`make demo` — the one-command demo lifecycle (§18).

    validate env → compose up → migrate → seed → index → health → print the URLs that answered

This used to be a Phase 0 stub that ran step one and exited 3. It is real now, and the stub's
framing is worth keeping in view because it is the reason this file is shaped the way it is: a
`demo` target that printed a URL it had not started would be a lie told by the tooling itself. So
every line printed below comes from something that actually ran, and the URL list is derived from
the health probe rather than from a table of addresses this file hopes are right.

Three rules the steps are held to:

* **A failed step stops the run.** No step reports success on the way to a later failure, and the
  exit code says which kind of failure it was.
* **A skipped step says so.** `--skip-index` prints "skipped", not "done" — the difference between
  a run that built an index and one that did not is the difference between a working RAG demo and
  one that answers every query with zero hits and no error.
* **Nothing is torn down.** This script starts and converges the stack; it never runs
  `docker compose down`. A demo target that removed volumes would destroy the state the next step
  of someone else's work depends on.

Exit codes
    0  every step completed and every service in the chosen profiles answered
    1  the environment is not runnable (scripts/validate_env.py said so)
    2  a step failed
    3  reserved: not implemented. Kept in the ladder because callers written against the stub
       handled it, and a code that silently changed meaning would break them.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Final

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

EXIT_OK = 0
EXIT_ENV_INVALID = 1
EXIT_STEP_FAILED = 2
EXIT_NOT_IMPLEMENTED = 3

#: How long `docker compose up --wait` may take. Generous, because it is bounded by the slowest
#: healthcheck's `start_period` plus its retries, and a timeout here would report a stack that is
#: still converging as a broken one.
COMPOSE_TIMEOUT_SECONDS: Final = 600


def _load(name: str) -> ModuleType:
    """Import a sibling script by path.

    By path rather than by name: these files are not a package, and `import health` would resolve
    against whatever else on `sys.path` happens to be called that.

    **Registered in `sys.modules` before execution**, which is not optional here: `health.py` defines
    frozen dataclasses, and `dataclasses` resolves a class's defining module through `sys.modules`
    while the class body is being processed. Without the registration this raises
    `AttributeError: 'NoneType' object has no attribute '__dict__'` from inside `dataclasses`, with
    nothing in the traceback pointing at the missing registration. The same three lines are in
    `tests/scripts/scripts_fixtures.py`, which found it first and says why.
    """
    module_name = f"_referraleasy_{name}"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_DIR / f"{name}.py")
    if spec is None or spec.loader is None:  # pragma: no cover - a packaging error, not a state
        raise RuntimeError(f"cannot load scripts/{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _step(index: int, total: int, title: str) -> None:
    print(f"\n─── step {index}/{total}: {title} " + "─" * max(0, 40 - len(title)))


def _run_step(script: str, *args: str) -> int:
    """Run a sibling script in its own interpreter and return its exit code."""
    completed = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / script), *args],
        check=False,
        cwd=REPO_ROOT,
    )
    return completed.returncode


def _compose_up(profiles: list[str]) -> int:
    """Bring the stack up and wait for its healthchecks, or return a failure code.

    `--wait` is what makes the health step meaningful: without it this returns as soon as the
    containers are created, and the probes that follow would report a stack that is still starting.
    """
    command = ["docker", "compose"]
    for profile in profiles:
        command += ["--profile", profile]
    command += ["up", "-d", "--wait"]
    print(f"$ {' '.join(command)}")
    try:
        completed = subprocess.run(
            command, check=False, cwd=REPO_ROOT, timeout=COMPOSE_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired:
        print(
            f"compose did not converge within {COMPOSE_TIMEOUT_SECONDS}s. `docker compose ps` and "
            "`docker compose logs <service>` say which container is not becoming healthy.",
            file=sys.stderr,
        )
        return 1
    except FileNotFoundError:
        print("`docker` is not on PATH, so the stack cannot be started.", file=sys.stderr)
        return 1
    return completed.returncode


#: The templates script lives under `infra/`, not `scripts/`, and takes its address from
#: `OPENSEARCH_URL` rather than a flag. Declared here so both facts are visible in one place.
INDEX_TEMPLATES_SCRIPT: Final = REPO_ROOT / "infra" / "opensearch" / "apply-index-templates.sh"


def _apply_index_templates(health: ModuleType, env: dict[str, str]) -> int:
    """Apply the ISM policies and index templates, or return a failure code.

    The address is `.env`'s `OPENSEARCH_URL` passed through the health probe's own host resolution,
    because that value names the compose service (`http://opensearch:9200`) and does not resolve
    from the host. Reusing `_host_url` rather than writing a second substitution is the same rule as
    everywhere else here: one answer to "where is OpenSearch".
    """
    configured = env.get("OPENSEARCH_URL", "http://localhost:9200")
    url = health._host_url(configured, fallback="localhost")
    print(f"$ OPENSEARCH_URL={url} {INDEX_TEMPLATES_SCRIPT.relative_to(REPO_ROOT)}")
    try:
        completed = subprocess.run(
            ["bash", str(INDEX_TEMPLATES_SCRIPT)],
            check=False,
            cwd=REPO_ROOT,
            env={**os.environ, "OPENSEARCH_URL": url},
        )
    except FileNotFoundError:  # pragma: no cover - bash is present everywhere this runs
        print("`bash` is not on PATH, so the templates cannot be applied.", file=sys.stderr)
        return 1
    return completed.returncode


def _health(health: ModuleType, profiles: list[str]) -> tuple[bool, list[tuple[str, str]]]:
    """Probe every service in the chosen profiles, in-process.

    In-process rather than by running `health.py` as a subprocess, for one reason: the URL list has
    to be the probe's own answer. This returns `(all_answered, [(service, url)])` for the services
    that are in the profile *and* answered, so `print URLs` cannot name an address nothing checked.
    """
    env = health._read_dotenv()
    probeable = [probe for probe in health.PROBES if probe.profile in profiles]
    healthy: list[tuple[str, str]] = []
    failed: list[str] = []
    for probe in probeable:
        status, detail = health._probe(probe, env)
        marker = "OK  " if status == health.OK else status
        print(f"  {probe.service:<16} {marker:<10} {detail[:90]}")
        if status != health.OK:
            failed.append(probe.service)
            continue
        url = health.probe_url(probe, env)
        if url is not None:
            healthy.append((probe.service, url))
    return not failed, healthy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="make demo", description="Bring the demo up and report what answered."
    )
    parser.add_argument(
        "--profile",
        action="append",
        choices=["core", "showcase", "dashboards"],
        default=None,
        help="compose profile to start and probe; repeatable. Defaults to core.",
    )
    parser.add_argument(
        "--no-up",
        action="store_true",
        help="assume the stack is already running and skip `compose up` (the stack is still probed)",
    )
    parser.add_argument(
        "--skip-index",
        action="store_true",
        help="skip the directory indexing step. Reported as skipped, never as done.",
    )
    args = parser.parse_args(argv)
    profiles = list(dict.fromkeys(args.profile or ["core"]))

    print("referraleasy demo")
    print("=" * 72)

    # Loaded once, at the top, because three steps need it: the index templates and `migrate` for
    # their addresses, and the health step for the probes. Importing it is side-effect-free —
    # everything in it is behind `__main__`.
    health = _load("health")
    env = health._read_dotenv()

    # The script's total is fixed so the step counter cannot drift from the number of things that
    # can happen, including the optional one.
    total = 8

    _step(1, total, "validate env")
    if _run_step("validate_env.py") != 0:
        print(
            "\nSTOPPED: the environment is not runnable. Fix the errors above, then re-run "
            "`make demo`.",
            file=sys.stderr,
        )
        return EXIT_ENV_INVALID

    _step(2, total, f"compose up (profiles: {', '.join(profiles)})")
    if args.no_up:
        print("skipped (--no-up): assuming the stack is already running, and probing it below")
    elif _compose_up(profiles) != 0:
        print("\nSTOPPED: the stack did not come up.", file=sys.stderr)
        return EXIT_STEP_FAILED

    _step(3, total, "apply OpenSearch index templates and ISM policies")
    # Not decoration, and the compose file says so where it declines to do this itself: "Index
    # templates + ISM policies are NOT applied at boot (OpenSearch has no init hooks)." An index
    # created before its template carries the default one replica — on a single node that is an
    # unassigned shard forever, and the 7-day telemetry retention the templates encode simply never
    # applies. So it runs here, after the cluster is up and *before* anything creates an index.
    # The script is idempotent and exits non-zero on failure, which is what makes it a step.
    if _apply_index_templates(health, env) != 0:
        print(
            "\nSTOPPED: the index templates were not applied. Continuing would create indices "
            "without their retention policy — 7 days for telemetry — and on a single node, without "
            "a replica count of 0, an unassigned shard that never resolves.",
            file=sys.stderr,
        )
        return EXIT_STEP_FAILED

    _step(4, total, "migrate")
    # `migrate.py` takes the DSN from `$DATABASE_URL` or `--database-url`, and this repository
    # deliberately does not export `.env` into the process environment (see `health._read_dotenv`).
    # So the orchestrator resolves it once, above, and hands it over — rather than every step growing
    # its own reader, which is how `_read_dotenv` ended up existing twice already.
    database_url = env.get("DATABASE_URL", "")
    migrate_args = ("--database-url", database_url) if database_url else ()
    if _run_step("migrate.py", *migrate_args) != 0:
        print(
            "\nSTOPPED: the database schema is not applied. `make demo` will not continue, because "
            "every step after this one reads through it.",
            file=sys.stderr,
        )
        return EXIT_STEP_FAILED

    _step(5, total, "seed (verify the datasets that are already loaded)")
    if _run_step("seed.py") != 0:
        print("\nSTOPPED: the loaded dataset does not match the fixtures on disk.", file=sys.stderr)
        return EXIT_STEP_FAILED

    _step(6, total, "index the directory")
    if args.skip_index:
        # Not "done": an index that was never built answers every query with zero hits and no error,
        # which reads as "no matching directory entries". The report has to keep those apart.
        print(
            "skipped (--skip-index): retrieval will answer with no evidence until `make index` runs"
        )
    elif _run_step("index_directory.py") != 0:
        print("\nSTOPPED: the directory index was not built.", file=sys.stderr)
        return EXIT_STEP_FAILED

    _step(7, total, "health")
    all_well, healthy_urls = _health(health, profiles)
    if not all_well:
        print(
            "\nSTOPPED: at least one service in the chosen profiles did not answer. A service "
            "outside the profile shows DOWN, which is true rather than wrong — check the profile "
            f"you asked for ({', '.join(profiles)}).",
            file=sys.stderr,
        )
        return EXIT_STEP_FAILED

    _step(8, total, "entry points")
    if healthy_urls:
        print("  These are the addresses that answered the probe above, and nothing else:\n")
        for service, url in healthy_urls:
            print(f"    {service:<16} {url}")
    else:
        # True for a profile whose services are all probeable without a URL — `core` with only
        # `postgres` in it, for instance. Saying so beats printing an empty section.
        print("  No service in this profile exposes an addressable URL.")

    print(
        "\nDone. The stack is up and every service in the chosen profile answered.\n"
        "`make demo` starts things and never tears them down: stop the stack yourself with "
        "`make down` when you are finished with it."
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
