#!/usr/bin/env python3.11
"""Run the local CI gate for octo-drone-shop with macOS Seatbelt isolation and private receipts.

Default execution is strictly offline under (deny network*). Jobs that require outbound
network access report unavailable and return exit 1 unless --allow-online is passed.
With --allow-online, only online opt-in jobs use outbound-permitted (inbound/bind denied)
Seatbelt rules while offline jobs remain under (deny network*).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import pwd
import shutil
import stat
import subprocess
import tempfile
from typing import TextIO

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ".gate/local-ci"
PYTHON = "python3.11"

ALLOWED_ENV_KEYS = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TMPDIR",
    "USER",
    "LOGNAME",
    "SHELL",
    "GOPATH",
    "GOMODCACHE",
    "GOCACHE",
    "GOROOT",
)


def _venv_bin_dirs() -> list[str]:
    return [
        str(candidate)
        for candidate in (
            ROOT / ".gate/venv/bin",
            ROOT / ".gate/bin",
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
        [
            "semgrep", "scan", "--error", "--quiet", "--metrics=off", "--disable-version-check",
            "--config", ".gate/semgrep-rules/owasp-top-ten.yml",
            "--config", ".gate/semgrep-rules/python.yml",
            "--config", ".gate/semgrep-rules/fastapi.yml",
            ".",
        ],
    ],
    "dependency-audit": [
        [
            "pip-audit",
            "-r",
            "requirements.txt",
            "--strict",
            "--progress-spinner",
            "off",
        ],
        ["npm", "audit", "--omit=dev", "--audit-level=high", "--package-lock-only", "--ignore-scripts"],
    ],
    "docs-build": [
        [PYTHON, "-m", "mkdocs", "build", "--strict", "-d", f"{OUTPUT}/site"],
    ],
    "secret-scan": [
        [PYTHON, "scripts/local_ci_gate.py", "--added-line-scan"],
    ],
    "go-gateway": [
        ["go", "-C", "services/workflow-gateway", "vet", "./..."],
        ["go", "-C", "services/workflow-gateway", "build", "./..."],
    ],
    "terraform-fmt": [
        ["terraform", "-chdir=deploy/terraform", "fmt", "-check", "-recursive"],
        ["tflint", "--init"],
        ["tflint", "--chdir=deploy/terraform", "--recursive"],
    ],
    "container-scan": [
        [
            "trivy", "config", "--skip-check-update", "--skip-version-check", "--severity", "HIGH,CRITICAL",
            "--exit-code", "1", "--skip-dirs", ".gate", "--skip-dirs", ".git",
            "--skip-dirs", "node_modules", "--skip-dirs", ".venv", "--skip-dirs", ".venv311", ".",
        ],
    ],
}

ONLINE_REASONS: dict[str, str] = {
    "dependency-audit": (
        "pip-audit and npm audit query live advisory registries and require an isolated "
        "environment; pass --allow-online for outbound-permitted (inbound/bind denied) execution."
    ),
}

UNAVAILABLE: dict[str, str] = {}


def unavailable_reason(job: str, *, allow_online: bool = False) -> str | None:
    if job in UNAVAILABLE:
        return UNAVAILABLE[job]
    if not allow_online and job in ONLINE_REASONS:
        return ONLINE_REASONS[job]
    return None


def ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    mode = path.lstat().st_mode
    if not stat.S_ISDIR(mode) or mode & 0o077:
        raise RuntimeError("Local CI output directory must be private")


def open_private_write(path: Path) -> TextIO:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except OSError:
        os.close(fd)
        raise
    return os.fdopen(fd, "w", encoding="utf-8")


def write_private_text(path: Path, content: str) -> None:
    with open_private_write(path) as stream:
        stream.write(content)


def network_sandbox_profile(home: Path) -> str:
    profile = "(version 1) (allow default) (deny network-inbound) (deny network-bind)"
    directories = (
        ".ssh", ".oci", ".aws", ".kube", ".azure", ".docker", ".config/gh",
        ".config/gcloud", ".config/pip", ".local/share/keyrings", ".npm", "Library/Keychains",
        "Library/Application Support/pip",
    )
    for path in directories:
        profile += f" (deny file-read* (subpath {json.dumps(str(home / path))}))"
    files = (
        home / ".netrc", home / ".npmrc", home / ".pypirc", home / ".pip/pip.conf",
        ROOT / ".gate/venv/pip.conf", ROOT / ".venv/pip.conf", ROOT / ".venv311/pip.conf",
        Path("/private/etc/pip.conf"), Path("/Library/Application Support/pip/pip.conf"),
        Path("/opt/homebrew/etc/npmrc"), Path("/usr/local/etc/npmrc"),
    )
    for path in files:
        profile += f" (deny file-read* (literal {json.dumps(str(path))}))"
    profile += (
        f" (deny file-read* (require-all (subpath {json.dumps(str(ROOT.resolve()))}) "
        '(regex #".*/([.]env([.]|$)|[.]npmrc$)")))'
    )
    return profile


def added_line_scan() -> int:
    locations: set[tuple[str, int]] = set()
    for options in (
        ["--log-opts=origin/main..HEAD --diff-merges=first-parent"],
        ["--staged"],
        ["--pre-commit"],
    ):
        try:
            result = subprocess.run(
                [
                    "gitleaks", "git", "--redact=100", "--no-banner", "--exit-code", "42",
                    "--report-format", "json", "--report-path", "-", *options, ".",
                ],
                cwd=ROOT,
                capture_output=True,
                check=False,
                timeout=90,
            )
            findings = json.loads(result.stdout)
            if result.returncode not in (0, 42) or not isinstance(findings, list):
                raise ValueError
            if bool(findings) != (result.returncode == 42):
                raise ValueError
            for finding in findings:
                path = finding["File"]
                line = finding["StartLine"]
                if not isinstance(path, str) or not path or type(line) is not int or line < 1:
                    raise ValueError
                locations.add((path, line))
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
            print("added-line scan unavailable")
            return 1
    for path, line in sorted(locations):
        display_path = json.dumps(path, ensure_ascii=True)[1:-1]
        print(f"{display_path}:{line}")
    print(f"added-line findings: {len(locations)}")
    return int(bool(locations))


def run_commands(
    commands: list[list[str]],
    output: Path | str,
    *,
    allow_network: bool = False,
) -> list[dict[str, object]]:
    """Execute argv commands, retaining each failure; reject missing isolation."""
    if platform.system() != "Darwin" or not shutil.which("sandbox-exec"):
        raise RuntimeError("macOS sandbox-exec required; refusing unisolated execution")
    output = Path(output)
    ensure_private_directory(output)
    host_home = Path.home()
    gomodcache = os.environ.get("GOMODCACHE", str(host_home / "go/pkg/mod"))
    gocache = os.environ.get("GOCACHE", str(host_home / "Library/Caches/go-build"))
    policy = (
        network_sandbox_profile(Path(pwd.getpwuid(os.getuid()).pw_dir).resolve())
        if allow_network
        else "(version 1) (allow default) (deny network*)"
    )
    results: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="local-ci-home-") as home:
        env = {key: os.environ[key] for key in ALLOWED_ENV_KEYS if key in os.environ}
        path_parts = [
            part
            for part in (*_venv_bin_dirs(), *env.get("PATH", "").split(os.pathsep))
            if part
        ]
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
            PYTHONNOUSERSITE="1",
            PYTHONPATH=os.pathsep.join(pythonpath_entries),
            GOMODCACHE=gomodcache,
            GOCACHE=gocache,
            GOTOOLCHAIN="local",
            GOPROXY="off",
            GOSUMDB="off",
        )
        if allow_network:
            clean_home = Path(home)
            config_paths = {
                "PIP_CONFIG_FILE": clean_home / "pip.conf",
                "NPM_CONFIG_USERCONFIG": clean_home / "npm-user.conf",
                "NPM_CONFIG_GLOBALCONFIG": clean_home / "npm-global.conf",
            }
            for key, path in config_paths.items():
                write_private_text(path, "")
                env[key] = str(path)
            env.pop("PYTHONPATH")
            env.update(
                PYTHONSAFEPATH="1",
                TMPDIR=str(clean_home / "tmp"),
                XDG_CONFIG_HOME=str(clean_home / "config"),
                XDG_CACHE_HOME=str(clean_home / "cache"),
                XDG_CONFIG_DIRS=str(clean_home / "config"),
                GIT_CONFIG_NOSYSTEM="1",
                GIT_CONFIG_GLOBAL="/dev/null",
            )
            Path(env["TMPDIR"]).mkdir(mode=0o700)
        for index, command in enumerate(commands):
            log = output / f"{index:02d}.log"
            with open_private_write(log) as stream:
                try:
                    result = subprocess.run(
                        ["sandbox-exec", "-p", policy, *command],
                        cwd=ROOT,
                        env=env,
                        stdout=stream,
                        stderr=subprocess.STDOUT,
                        timeout=600 if allow_network else 300,
                        check=False,
                        umask=0o077,
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
    parser.add_argument("--added-line-scan", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--job", choices=JOBS, action="append")
    parser.add_argument(
        "--allow-online",
        action="store_true",
        help="Run online-dependent jobs under outbound-permitted (inbound/bind denied) Seatbelt policy",
    )
    parser.add_argument("--receipt", type=Path, default=ROOT / OUTPUT / "receipt.json")
    args = parser.parse_args(argv)
    if args.added_line_scan:
        return added_line_scan()
    if args.list:
        print(
            json.dumps(
                {
                    key: {
                        "commands": commands,
                        "available": unavailable_reason(key, allow_online=args.allow_online) is None,
                        "reason": unavailable_reason(key, allow_online=args.allow_online),
                    }
                    for key, commands in JOBS.items()
                },
                indent=2,
            )
        )
        return 0
    output = ROOT / OUTPUT
    ensure_private_directory(output)
    records: dict[str, dict[str, object]] = {}
    for job in args.job or JOBS:
        reason = unavailable_reason(job, allow_online=args.allow_online)
        if reason is not None:
            record: dict[str, object] = {
                "status": "unavailable",
                "reason": reason,
                "commands": [],
            }
        else:
            try:
                allow_net = args.allow_online and job in ONLINE_REASONS
                results = run_commands(
                    JOBS[job],
                    output / job,
                    allow_network=allow_net,
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
        "mode": "online-opt-in" if args.allow_online else "offline",
        "provider_verified": False,
        "release_accepted": False,
        "jobs": records,
    }
    write_private_text(args.receipt, json.dumps(receipt, indent=2) + "\n")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
