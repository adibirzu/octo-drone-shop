"""Local gate CLI contract: offline execution and truthful failure reporting."""

from __future__ import annotations

import importlib.util
import json
import stat
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/local_ci_gate.py"


def invoke(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def load_gate_module():
    spec = importlib.util.spec_from_file_location("local_ci_gate", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_lists_every_required_gate_job_and_rejects_shell_wrappers():
    result = invoke("--list")
    assert result.returncode == 0, result.stderr
    jobs = json.loads(result.stdout)

    expected_jobs = {
        "unit-tests",
        "python-sast",
        "dependency-audit",
        "docs-build",
        "secret-scan",
        "go-gateway",
        "terraform-fmt",
    }
    assert expected_jobs <= set(jobs)
    assert all(row["available"] is True for row in jobs.values())
    assert all(row["commands"] for row in jobs.values())

    all_commands = [cmd for row in jobs.values() for cmd in row["commands"]]
    for cmd in all_commands:
        assert isinstance(cmd, list) and cmd, f"Expected non-empty argv list: {cmd!r}"
        assert cmd[0] not in {"sh", "bash", "zsh", "/bin/sh", "/bin/bash"}
        assert "|| true" not in " ".join(cmd)
        assert not any( token.startswith("/Users/") for token in cmd ), f"Leaked host path in argv: {cmd!r}"

    assert any("pytest" in cmd and "services/async-worker/tests" not in cmd for cmd in jobs["unit-tests"]["commands"])
    assert any("pytest" in cmd and "services/async-worker/tests" in cmd for cmd in jobs["unit-tests"]["commands"])
    assert any(cmd[:4] == ["bandit", "-q", "-r", "server"] for cmd in jobs["python-sast"]["commands"])
    assert any(cmd[:2] == ["ruff", "check"] and "S" in cmd and "server" in cmd for cmd in jobs["python-sast"]["commands"])
    assert any(cmd[:4] == ["pip-audit", "-r", "requirements.txt", "--strict"] for cmd in jobs["dependency-audit"]["commands"])
    assert any("mkdocs" in cmd and "build" in cmd and "--strict" in cmd for cmd in jobs["docs-build"]["commands"])
    assert any(cmd[:4] == ["gitleaks", "detect", "--source", "."] and "--redact" in cmd for cmd in jobs["secret-scan"]["commands"])
    assert any(cmd[:4] == ["go", "-C", "services/workflow-gateway", "vet"] for cmd in jobs["go-gateway"]["commands"])
    assert any(cmd[:4] == ["go", "-C", "services/workflow-gateway", "build"] for cmd in jobs["go-gateway"]["commands"])
    assert any(cmd[:3] == ["terraform", "-chdir=deploy/terraform", "fmt"] and "-check" in cmd for cmd in jobs["terraform-fmt"]["commands"])


def test_unknown_job_fails_without_execution():
    result = invoke("--job", "deploy")
    assert result.returncode == 2


def test_unavailable_job_is_nonzero_and_records_missing_evidence(tmp_path: Path, monkeypatch):
    module = load_gate_module()
    monkeypatch.setitem(module.UNAVAILABLE, "dependency-audit", "advisory cache unavailable")
    receipt = tmp_path / "receipt.json"
    code = module.main(["--job", "dependency-audit", "--receipt", str(receipt)])
    assert code == 1
    record = json.loads(receipt.read_text())
    assert record["status"] == "failed"
    assert record["jobs"]["dependency-audit"]["status"] == "unavailable"
    assert record["provider_verified"] is False
    assert record["release_accepted"] is False


def test_executor_continues_after_failure_and_blocks_network(tmp_path: Path):
    module = load_gate_module()
    marker = tmp_path / "continued"
    commands = [
        [sys.executable, "-c", "raise SystemExit(7)"],
        [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
        [sys.executable, "-c", "import socket; s=socket.socket(); s.bind(('', 0))"],
    ]
    result = module.run_commands(commands, tmp_path)
    assert [row["exit_code"] for row in result] == [7, 0, 1]
    assert marker.exists()
    for row in result:
        log_path = tmp_path / row["log"]
        assert log_path.is_file()
        assert stat.S_IMODE(log_path.stat().st_mode) == 0o600


def test_config_uses_local_gate_and_disables_automatic_repairs():
    config = yaml.safe_load((ROOT / ".no-mistakes.yaml").read_text())
    assert set(config) == {"commands", "auto_fix"}
    assert "allow_repo_commands" not in config
    assert set(config["commands"]) == {"test", "lint"}
    assert config["commands"]["test"] == "python3.11 scripts/local_ci_gate.py"
    assert config["commands"]["lint"] == "python3.11 scripts/local_ci_gate.py --job python-sast --job secret-scan --job go-gateway --job terraform-fmt"
    assert config["auto_fix"] == {"review": 0, "test": 0, "lint": 0}


def test_gitignore_excludes_private_gate_and_cache_dirs():
    for candidate in (".gate/receipt.json", ".cache/local-ci/receipt.json"):
        check = subprocess.run(
            ["git", "check-ignore", "-q", candidate],
            cwd=ROOT,
            check=False,
        )
        assert check.returncode == 0, f"Expected {candidate} to be gitignored"
