"""`python -m referraleasy_gateway` — the process entry point.

The only thing here is the bind address: settings come from the environment, so the module that
decides *what* runs is `app.py` and the module that decides *where* it listens is this one.

`proxy_headers=False`: this process is behind the platform's load balancer, and forwarding headers
from a client-controlled connection is how a demo grows a spoofable client address. Nothing in the
gateway reads one. `access_log=False` for the same reason the request-id middleware exists — one
structured line per request from the app, rather than two half-populated ones from the server.
"""

from __future__ import annotations

import atexit
import logging
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version

import uvicorn

from referraleasy_gateway.app import create_app
from referraleasy_gateway.config import GatewaySettings
from referraleasy_telemetry import configure_telemetry

__all__ = ["main", "service_version"]


def service_version() -> str:
    """The gateway's own installed version, for §15's resource attributes.

    Read from the distribution rather than written here, because a literal in this file is a number
    that goes stale silently — and a trace that says "version 0.1.0" while the image is another one
    is worse than a trace that says nothing, since the whole point of the attribute is to tell two
    releases apart. `0.0.0` is the dataclass default and the honest answer for an uninstalled tree
    (`uv run python -m referraleasy_gateway` from a checkout is exactly that case).
    """
    try:
        return _distribution_version("referraleasy-gateway")
    except PackageNotFoundError:  # pragma: no cover - the installed path is the one that runs
        return "0.0.0"


def main() -> None:
    """Build the app from the environment and serve it."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = GatewaySettings.from_env()
    # §15's adoption seam. This is the one process-wide thing the entry point does: the app itself
    # takes the bundle as an argument (see `create_app`), so a test that builds an app installs no
    # provider and exports nothing.
    #
    # Inert when `OTEL_EXPORTER_OTLP_ENDPOINT` is unset, by design: real providers, no exporter, no
    # connection attempts. A developer running `python -m referraleasy_gateway` without the stack
    # gets working spans that go nowhere rather than a service that will not start.
    telemetry = configure_telemetry("gateway", version=service_version())
    # Registered rather than wrapped in a `finally`: `uvicorn.run` installs its own signal handlers
    # and returns on SIGTERM, and `atexit` runs after those handlers have drained. A flush that
    # missed the last batch would lose exactly the spans a shutdown is worth tracing for.
    atexit.register(telemetry.shutdown)
    app = create_app(settings=settings, telemetry=telemetry)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level="info",
        access_log=False,
        proxy_headers=False,
        server_header=False,
        date_header=False,
    )


if __name__ == "__main__":  # pragma: no cover - exercised by running the module, not by a test
    main()
