#!/usr/bin/env python3.11
"""Run the local CI gate for octo-drone-shop with macOS Seatbelt isolation and private receipts."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ".gate/local-ci"
PYTHON = "python3.11"


def _venv_bin_dirs() -> list[str]:
    return [
        str(candidate)
        for candidate in (
            ROOT / ".gate/venv/bin",
            ROOT / ".venv/bin",
            ROOT / ".venv311/bin",
        )
        if candidate.is_dir()
    ]

JOBS: dict[str, list[list[str]]] = {
    "unit-tests": [
        [PYTHON, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        [PYTHON, "-m", "pytest", "-q", "-p", "no:cacheprovider", "services/async-worker/tests"],
    ],
    "python-sast": [
        ["bandit", "-q", "-r", "server", "-x", "server/__pycache__", "-ll", "-iii"],
        ["ruff", "check", "--no-cache", "--select", "S", "server"],
    ],
    "dependency-audit": [
        [
            "pip-audit",
            "-r",
            "requirements.txt",
            "--strict",
            "--progress-spinner",
            "off",
            "--cache-dir",
            f"{OUTPUT}/audit-cache",
        ],
    ],
    "docs-build": [
        [PYTHON, "-m", "mkdocs", "build", "--strict", "-d", f"{OUTPUT}/site"],
    ],
    "secret-scan": [
        ["gitleaks", "detect", "--source", ".", "--redact", "--no-banner"],
    ],
    "go-gateway": [
        ["go", "-C", "services/workflow-gateway", "vet", "./..."],
        ["go", "-C", "services/workflow-gateway", "build", "./..."],
    ],
    "terraform-fmt": [
        ["terraform", "-chdir=deploy/terraform", "fmt", "-check", "-recursive"],
    ],
}

UNAVAILABLE: dict[str, str] = {}
READONLY_NETWORK_JOBS = {"dependency-audit", "docs-build"}


def _audit_cache_warm() -> bool:
    cache_dir = ROOT / OUTPUT / "audit-cache"
    return cache_dir.is_dir() and any(cache_dir.iterdir())


def _job_allows_network(job: str) -> bool:
    if job == "dependency-audit":
        return not _audit_cache_warm()
    return job in READONLY_NETWORK_JOBS


def run_commands(commands: list[list[str]], output: Path | str, *, allow_network: bool = False) -> list[dict[str, object]]:
    """Execute argv commands, retaining each failure; reject missing isolation."""
    if platform.system() != "Darwin" or not shutil.which("sandbox-exec"):
        raise RuntimeError("macOS sandbox-exec required; refusing unisolated execution")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output, 0o700)
    host_home = Path.home()
    gomodcache = os.environ.get("GOMODCACHE", str(host_home / "go/pkg/mod"))
    gocache = os.environ.get("GOCACHE", str(host_home / "Library/Caches/go-build"))
    pip_cache_dir = os.environ.get("PIP_CACHE_DIR", str(host_home / "Library/Caches/pip"))
    policy = (
        "(version 1) (allow default) (deny network-inbound) (deny network-bind)"
        if allow_network
        else "(version 1) (allow default) (deny network*)"
    )
    results: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="local-ci-home-") as home:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith((
                "OCI_",
                "AWS_",
                "AZURE_",
                "GOOGLE_",
                "OTEL_",
                "OCTO_",
                "IDCS_",
                "ORACLE_",
                "DATABASE_",
                "POSTGRES_",
                "REDIS_",
            ))
        }
        path_parts = [part for part in (*_venv_bin_dirs(), *env.get("PATH", "").split(os.pathsep)) if part]
        current_path = os.pathsep.join(dict.fromkeys(path_parts))
        pythonpath_entries = [
            str(ROOT),
            str(ROOT / "services/cache/client"),
            str(ROOT / "services/async-worker/src"),
        ]
        env.update(
            HOME=home,
            PATH=current_path,
            ENV="test",
            OCI_AUTH_MODE="config",
            OCI_CONFIG_FILE="/dev/null",
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONPATH=os.pathsep.join(pythonpath_entries),
            GOMODCACHE=gomodcache,
            GOCACHE=gocache,
            GOTOOLCHAIN="local",
            GOPROXY="off",
            GOSUMDB="off",
            PIP_CACHE_DIR=pip_cache_dir,
        )
        for index, command in enumerate(commands):
            log = output / f"{index:02d}.log"
            with log.open("w") as stream:
                os.chmod(log, 0o600)
                try:
                    result = subprocess.run(
                        ["sandbox-exec", "-p", policy, *command],
                        cwd=ROOT,
                        env=env,
                        stdout=stream,
                        stderr=subprocess.STDOUT,
                        timeout=300,
                        check=False,
                    )
                    code = result.returncode
                except (OSError, subprocess.TimeoutExpired) as error:
                    stream.write(f"Local command unavailable: {type(error).__name__}\n")
                    code = 124 if isinstance(error, subprocess.TimeoutExpired) else 127
            results.append({"command": command, "exit_code": code, "log": log.name})
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--job", choices=JOBS, action="append")
    parser.add_argument("--receipt", type=Path, default=ROOT / OUTPUT / "receipt.json")
    args = parser.parse_args(argv)
    if args.list:
        print(
            json.dumps(
                {
                    key: {
                        "commands": commands,
                        "available": key not in UNAVAILABLE,
                        "reason": UNAVAILABLE.get(key),
                    }
                    for key, commands in JOBS.items()
                },
                indent=2,
            )
        )
        return 0
    output = ROOT / OUTPUT
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output, 0o700)
    records: dict[str, dict[str, object]] = {}
    for job in args.job or JOBS:
        if job in UNAVAILABLE:
            record: dict[str, object] = {
                "status": "unavailable",
                "reason": UNAVAILABLE[job],
                "commands": [],
            }
        else:
            try:
                results = run_commands(
                    JOBS[job],
                    output / job,
                    allow_network=_job_allows_network(job),
                )
                record = {
                    "status": "passed" if all(r["exit_code"] == 0 for r in results) else "failed",
                    "commands": results,
                }
            except RuntimeError as error:
                record = {"status": "unavailable", "reason": str(error), "commands": []}
        records[job] = record
        print(f"{job}: {record['status']}", flush=True)
    success = all(record["status"] == "passed" for record in records.values())
    receipt = {
        "schema": "octo-drone-shop.local-ci/v1",
        "status": "passed" if success else "failed",
        "provider_verified": False,
        "release_accepted": False,
        "jobs": records,
    }
    args.receipt.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
    os.chmod(args.receipt, 0o600)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
