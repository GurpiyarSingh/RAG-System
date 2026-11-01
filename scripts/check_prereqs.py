"""Check that this machine is ready to run the RAG service locally.

Uses only the Python standard library, so it runs *before* any setup:

    python scripts/check_prereqs.py

Every check prints PASS / WARN / FAIL with a hint on how to fix it. The exit code is 1
if any check FAILs, so it can also be used in scripts.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Host ports the Compose stack publishes (see compose.yaml).
STACK_PORTS = {
    6333: "Qdrant",
    6379: "Redis",
    7233: "Temporal",
    8000: "rag-api",
    8001: "rag-query",
    8233: "Temporal UI",
    8333: "SeaweedFS (S3)",
}
MIN_DOCKER_MEMORY_GB = 6
RECOMMENDED_DOCKER_MEMORY_GB = 8
MIN_FREE_DISK_GB = 20
DEFAULT_PG_BIN = r"D:\C Drive\PostgreSQL\bin"
WSLCONFIG_HINT = r"%UserProfile%\.wslconfig"

failures = 0


def report(status: str, name: str, detail: str = "", hint: str = "") -> None:
    global failures
    if status == "FAIL":
        failures += 1
    line = f"[{status}] {name}"
    if detail:
        line += f": {detail}"
    print(line)
    if hint and status != "PASS":
        print(f"        -> {hint}")


def run(cmd: list[str], timeout: int = 20) -> tuple[int, str]:
    try:
        out = subprocess.run(  # noqa: S603 - fixed argument lists, no shell
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return out.returncode, (out.stdout + out.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def read_env() -> dict[str, str]:
    path = ROOT / ".env"
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    return values


def check_python() -> None:
    v = sys.version_info
    if (v.major, v.minor) == (3, 14):
        report("PASS", "Python", f"{v.major}.{v.minor}.{v.micro}")
    else:
        report(
            "FAIL",
            "Python",
            f"{v.major}.{v.minor} found, 3.14 required",
            "Install Python 3.14 from python.org and run this script with it.",
        )


def check_docker() -> bool:
    if not shutil.which("docker"):
        report("FAIL", "Docker CLI", "not found", "Install Docker Desktop (WSL2 backend).")
        return False
    code, out = run(["docker", "version", "--format", "{{.Server.Version}}"])
    if code != 0:
        report(
            "FAIL",
            "Docker engine",
            "not running",
            "Start Docker Desktop and wait until it says 'Engine running'.",
        )
        return False
    report("PASS", "Docker engine", f"version {out}")

    code, out = run(["docker", "compose", "version", "--short"])
    if code == 0:
        report("PASS", "Docker Compose", f"version {out}")
    else:
        report(
            "FAIL",
            "Docker Compose",
            "plugin missing",
            "Update Docker Desktop (Compose v2 is included).",
        )

    code, out = run(["docker", "info", "--format", "{{json .MemTotal}}"])
    if code == 0 and out.strip().isdigit():
        gb = int(out) / 1024**3
        if gb >= RECOMMENDED_DOCKER_MEMORY_GB - 0.5:
            report("PASS", "Docker memory", f"{gb:.1f} GB")
        elif gb >= MIN_DOCKER_MEMORY_GB - 0.5:
            report(
                "WARN",
                "Docker memory",
                f"{gb:.1f} GB (8 GB recommended)",
                f"Raise 'memory=' under [wsl2] in {WSLCONFIG_HINT}, then run 'wsl --shutdown'.",
            )
        else:
            report(
                "FAIL",
                "Docker memory",
                f"{gb:.1f} GB, at least {MIN_DOCKER_MEMORY_GB} GB needed",
                f"Set 'memory=8GB' under [wsl2] in {WSLCONFIG_HINT}, "
                "then run 'wsl --shutdown' and restart Docker Desktop.",
            )
    return True


def check_postgres(env: dict[str, str]) -> None:
    pg_bin = env.get("RAG_PG_BIN") or os.environ.get("RAG_PG_BIN") or DEFAULT_PG_BIN
    psql = Path(pg_bin) / ("psql.exe" if os.name == "nt" else "psql")
    found = str(psql) if psql.exists() else shutil.which("psql")
    if not found:
        report(
            "FAIL",
            "psql",
            "not found",
            "Install PostgreSQL 18, or set RAG_PG_BIN in .env to its 'bin' folder.",
        )
    else:
        _code, out = run([found, "--version"])
        major = next((int(p.split(".")[0]) for p in out.split() if p[:1].isdigit()), 0)
        if major >= 18:
            report("PASS", "PostgreSQL client", out)
        else:
            report(
                "FAIL",
                "PostgreSQL client",
                out or "unknown version",
                "PostgreSQL 18 or newer is required (uuidv7()).",
            )

    host = env.get("RAG_DB_HOST", "127.0.0.1")
    port = int(env.get("RAG_DB_PORT", "5432"))
    with socket.socket() as sock:
        sock.settimeout(1)
        if sock.connect_ex((host, port)) == 0:
            report("PASS", "PostgreSQL server", f"listening on {host}:{port}")
        else:
            report(
                "FAIL",
                "PostgreSQL server",
                f"nothing listening on {host}:{port}",
                "Start the 'postgresql-x64-18' service (Win+R → services.msc).",
            )


def check_ports() -> None:
    # Ports used by our own running stack are fine; anything else on them is a conflict.
    code, out = run(["docker", "ps", "--format", "{{json .Ports}}"])
    ours = out if code == 0 else ""
    busy = [
        f"{p} ({name})"
        for p, name in STACK_PORTS.items()
        if port_in_use(p) and f":{p}->" not in ours
    ]
    if busy:
        report(
            "FAIL",
            "Free ports",
            "in use: " + ", ".join(busy),
            "Stop the program using that port (netstat -ano | findstr :<port>), "
            "or change it in compose.yaml.",
        )
    else:
        report("PASS", "Free ports", ", ".join(str(p) for p in STACK_PORTS))


def check_disk() -> None:
    free_gb = shutil.disk_usage(ROOT).free / 1024**3
    if free_gb >= MIN_FREE_DISK_GB:
        report("PASS", "Disk space", f"{free_gb:.0f} GB free")
    else:
        report(
            "WARN",
            "Disk space",
            f"{free_gb:.0f} GB free ({MIN_FREE_DISK_GB} GB recommended for images)",
        )


def check_internet() -> None:
    ok = []
    for host in ("pypi.org", "registry-1.docker.io", "api.openai.com"):
        try:
            socket.create_connection((host, 443), timeout=5).close()
            ok.append(host)
        except OSError:
            report(
                "WARN",
                "Internet",
                f"cannot reach {host}",
                "Needed to download packages/images and to call OpenAI.",
            )
    if len(ok) == 3:
        report("PASS", "Internet", "PyPI, Docker Hub and OpenAI reachable")


def check_project_files(env: dict[str, str]) -> None:
    if not env:
        report("WARN", ".env", "not created yet", "Run: python scripts/gen_env.py")
        return
    report("PASS", ".env", "present")
    key = env.get("OPENAI_API_KEY", "")
    if not key or "your-openai-key" in key:
        report(
            "WARN",
            "OpenAI key",
            "placeholder",
            "The stack runs, but questions fail with 503 until you set OPENAI_API_KEY in .env.",
        )
    else:
        report("PASS", "OpenAI key", "set")
    venv_python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if venv_python.exists():
        report("PASS", "Virtual environment", ".venv exists")
    else:
        report("WARN", "Virtual environment", "not created yet", "Run: python -m venv .venv")


def main() -> None:
    print("Checking prerequisites for the RAG service...\n")
    env = read_env()
    check_python()
    if check_docker():
        check_ports()
    check_postgres(env)
    check_disk()
    check_internet()
    check_project_files(env)
    print()
    if failures:
        print(
            f"{failures} check(s) FAILED. Fix them (see the hints above) and run this script again."
        )
        sys.exit(1)
    print("All required checks passed. You're ready to continue with the setup guide.")


if __name__ == "__main__":
    main()
