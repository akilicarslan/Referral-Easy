"""Run each E2E iteration against its own mock API process and temporary SQLite database."""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def verify_report(path: Path) -> None:
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    if not cases or any(case.find("skipped") is not None for case in cases):
        raise RuntimeError("E2E did not run every collected scenario; empty or skipped runs fail")


def run_iteration(directory: Path, test_path: str = "tests/e2e") -> int:
    node = shutil.which("node")
    tsx = ROOT / "services/mock-api/node_modules/tsx/dist/cli.mjs"
    if node is None or not tsx.exists():
        raise RuntimeError("Install Node and workspace dependencies before running E2E")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "PORT": str(port),
        "HOST": "127.0.0.1",
        "MOCK_API_URL": url,
        "MOCK_API_DB": str(directory / "mock.sqlite"),
        "MOCK_API_SEED": "fixtures",
        "MOCK_API_FAULTS_ENABLED": "true",
    }
    with (directory / "mock.log").open("w+") as log:
        process = subprocess.Popen(
            [node, str(tsx), "src/server.ts"],
            cwd=ROOT / "services/mock-api",
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 30
            with httpx.Client(timeout=1, trust_env=False) as client:
                while True:
                    if process.poll() is not None:
                        raise RuntimeError("isolated mock API exited before becoming ready")
                    try:
                        if client.get(f"{url}/healthz").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if time.monotonic() >= deadline:
                        raise RuntimeError("isolated mock API did not become ready in 30 seconds")
                    time.sleep(0.1)
            report = directory / "results.xml"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    test_path,
                    "-m",
                    "e2e",
                    "-q",
                    f"--junitxml={report}",
                ],
                cwd=ROOT,
                env=env,
                check=False,
            )
            if result.returncode:
                return result.returncode
            verify_report(report)
            return 0
        except Exception:
            log.seek(0)
            print(log.read()[-4000:], file=sys.stderr)
            raise
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--test-path", default="tests/e2e")
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    if not (ROOT / args.test_path).resolve().is_relative_to(ROOT / "tests/e2e"):
        parser.error("--test-path must be inside tests/e2e")
    for number in range(1, args.repeat + 1):
        print(f"E2E iteration {number}/{args.repeat}: fresh isolated mock API", flush=True)
        with tempfile.TemporaryDirectory(prefix="referraleasy-e2e-") as directory:
            code = run_iteration(Path(directory), args.test_path)
        if code:
            return code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
