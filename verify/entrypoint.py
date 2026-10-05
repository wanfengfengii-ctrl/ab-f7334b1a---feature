"""One-shot verification entrypoint.

Sequence, aborting on first failed stage (the process exit code reports
the overall verdict and the container then exits):

  1. Wait until the API reports healthy over HTTP.
  2. Run the code test suite (``python -m unittest``).
  3. Image build check: compile every shipped source file and validate
     the Dockerfile / compose wiring; if a Docker CLI and socket are
     available, also run ``docker build --check`` on the context.
  4. HTTP smoke test against the live API: the 2016-12-31 leap second
     and an onboard-clock correlation-segment boundary.
"""

from __future__ import annotations

import json
import os
import py_compile
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API_HOST = os.environ.get("API_HOST", "api")
PORT = os.environ.get("PORT", "8080")
HEALTH_URL = f"http://{API_HOST}:{PORT}/healthz"
SRV = Path(os.environ.get("SRV", "/srv"))
BUILD_CONTEXT = Path(os.environ.get("BUILD_CONTEXT", "/build-context"))


def stage(name: str) -> None:
    print(f"\n=== verify: {name} ===", flush=True)


def wait_for_health(timeout: float = 60.0) -> bool:
    stage(f"waiting for API health at {HEALTH_URL}")
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=3) as resp:
                if resp.status == 200:
                    body = json.loads(resp.read())
                    print(f"API healthy: {body}")
                    return True
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            last_err = exc
        time.sleep(1.0)
    print(f"API never became healthy: {last_err}")
    return False


def run_code_tests() -> bool:
    stage("code tests (unittest discover)")
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=SRV,
    )
    return proc.returncode == 0


def image_build_check() -> bool:
    stage("image build check")
    ok = True

    # (a) Every shipped Python file must byte-compile.
    py_files = sorted(
        p for p in SRV.rglob("*.py")
        if "__pycache__" not in p.parts
    )
    print(f"byte-compiling {len(py_files)} files...")
    for path in py_files:
        try:
            py_compile.compile(str(path), doraise=True)
            print(f"  ok  {path.relative_to(SRV)}")
        except py_compile.PyCompileError as exc:
            print(f"  FAIL {path}: {exc}")
            ok = False

    # (b) The application must import and expose the HTTP handler.
    probe = (
        "import app.server, app.timecore;"
        " print('imports ok, leap rows:', len(app.timecore.LEAP_BOUNDS))"
    )
    proc = subprocess.run([sys.executable, "-c", probe], cwd=SRV)
    if proc.returncode != 0:
        ok = False

    # (c) The image must ship the HTTP entrypoint and the runtime probe
    # must succeed from a clean interpreter (proves CMD wiring is viable).
    proc = subprocess.run(
        [sys.executable, "-c",
         "from app.server import Handler, main; print('entrypoint ok')"],
        cwd=SRV,
    )
    if proc.returncode != 0:
        ok = False

    # (d) Validate the Dockerfile content baked into the image at build
    # time (see COPY of /build-context): required instructions present.
    ctx = BUILD_CONTEXT
    dockerfile = ctx / "Dockerfile"
    if dockerfile.exists():
        text = dockerfile.read_text()
        required = {
            "FROM": "base image declaration",
            "HEALTHCHECK": "container healthcheck",
            "CMD": "startup command",
            "EXPOSE": "exposed port",
        }
        for token, why in required.items():
            if any(
                line.strip().startswith(token)
                for line in text.splitlines()
            ):
                print(f"  ok  Dockerfile has {token} ({why})")
            else:
                print(f"  FAIL Dockerfile missing {token} ({why})")
                ok = False
        compose = ctx / "docker-compose.yml"
        if compose.exists():
            ct = compose.read_text()
            for needle in ("healthcheck:", "/healthz", "service_healthy",
                           "verify"):
                if needle in ct:
                    print(f"  ok  compose references {needle!r}")
                else:
                    print(f"  FAIL compose missing {needle!r}")
                    ok = False
    else:
        print("  note: /build-context/Dockerfile not present in image;"
              " skipping static Dockerfile validation")

    # (e) If a Docker CLI + socket happen to be available, do a real
    # BuildKit lint pass on the same context.
    if shutil.which("docker") and os.path.exists("/var/run/docker.sock"):
        print("docker CLI + socket found: running 'docker build --check'")
        proc = subprocess.run(
            ["docker", "build", "--check", "-f",
             str(dockerfile), str(ctx)],
        )
        if proc.returncode != 0:
            ok = False
    else:
        print("  (docker CLI/socket not mounted; static checks suffice)")

    return ok


def run_smoke() -> bool:
    stage("cross-leap-second / cross-correlation-boundary HTTP smoke test")
    proc = subprocess.run(
        [sys.executable, str(SRV / "verify" / "smoke_http.py")],
        cwd=SRV, env={**os.environ, "API_HOST": API_HOST, "PORT": PORT},
    )
    return proc.returncode == 0


def main() -> int:
    steps = [
        ("wait-for-health", wait_for_health),
        ("code-tests", run_code_tests),
        ("image-build-check", image_build_check),
        ("http-smoke", run_smoke),
    ]
    for name, fn in steps:
        if not fn():
            print(f"\nVERIFY FAILED at stage: {name}", flush=True)
            return 1
        print(f"--- {name}: passed", flush=True)
    print("\nALL VERIFY STAGES PASSED", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
