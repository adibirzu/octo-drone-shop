"""Local gate CLI contract: strict offline default, explicit online opt-in, and minimal child env."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/local_ci_gate.py"
OFFLINE_JOBS = {
    "unit-tests",
    "python-sast",
    "secret-scan",
    "go-gateway",
    "terraform-fmt",
    "docs-build",
    "container-scan",
}
ONLINE_OPT_IN_JOBS = {
    "dependency-audit",
}


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


def test_lists_offline_and_online_availability_and_rejects_shell_wrappers():
    offline_list = invoke("--list")
    assert offline_list.returncode == 0, offline_list.stderr
    jobs = json.loads(offline_list.stdout)

    assert OFFLINE_JOBS | ONLINE_OPT_IN_JOBS == set(jobs)
    for name in OFFLINE_JOBS:
        assert jobs[name]["available"] is True
        assert jobs[name]["reason"] is None
        assert jobs[name]["commands"]
    for name in ONLINE_OPT_IN_JOBS:
        assert jobs[name]["available"] is False
        assert jobs[name]["reason"] and "--allow-online" in jobs[name]["reason"]
        assert jobs[name]["commands"]

    online_list = invoke("--list", "--allow-online")
    assert online_list.returncode == 0, online_list.stderr
    online_jobs = json.loads(online_list.stdout)
    assert all(row["available"] is True and row["reason"] is None for row in online_jobs.values())

    all_commands = [cmd for row in jobs.values() for cmd in row["commands"]]
    for cmd in all_commands:
        assert isinstance(cmd, list) and cmd, f"Expected non-empty argv list: {cmd!r}"
        assert cmd[0] not in {"sh", "bash", "zsh", "/bin/sh", "/bin/bash"}
        assert "|| true" not in " ".join(cmd)
        assert not any(token.startswith("/Users/") for token in cmd), f"Leaked host path in argv: {cmd!r}"

    assert any("pytest" in cmd and "services/async-worker/tests" not in cmd for cmd in jobs["unit-tests"]["commands"])
    assert any("pytest" in cmd and "services/async-worker/tests" in cmd for cmd in jobs["unit-tests"]["commands"])
    assert any(cmd[:4] == ["bandit", "-q", "-r", "server"] for cmd in jobs["python-sast"]["commands"])
    assert any(cmd[:2] == ["ruff", "check"] and "S" in cmd and "server" in cmd for cmd in jobs["python-sast"]["commands"])
    assert any(cmd[:4] == ["pip-audit", "-r", "requirements.txt", "--strict"] for cmd in jobs["dependency-audit"]["commands"])
    assert any("mkdocs" in cmd and "build" in cmd and "--strict" in cmd for cmd in jobs["docs-build"]["commands"])
    assert any(cmd[0] == "semgrep" and "--error" in cmd for cmd in jobs["python-sast"]["commands"])
    assert any(cmd[:2] == ["npm", "audit"] and "--omit=dev" in cmd and "--audit-level=high" in cmd for cmd in jobs["dependency-audit"]["commands"])
    assert any("--added-line-scan" in cmd for cmd in jobs["secret-scan"]["commands"])
    assert any(cmd[0] == "tflint" and "--recursive" in cmd for cmd in jobs["terraform-fmt"]["commands"])
    assert any(cmd[:2] == ["trivy", "config"] and "--skip-check-update" in cmd for cmd in jobs["container-scan"]["commands"])
    assert any(cmd[:4] == ["go", "-C", "services/workflow-gateway", "vet"] for cmd in jobs["go-gateway"]["commands"])
    assert any(cmd[:4] == ["go", "-C", "services/workflow-gateway", "build"] for cmd in jobs["go-gateway"]["commands"])
    assert any(cmd[:3] == ["terraform", "-chdir=deploy/terraform", "fmt"] and "-check" in cmd for cmd in jobs["terraform-fmt"]["commands"])


def test_unknown_job_fails_without_execution():
    result = invoke("--job", "deploy")
    assert result.returncode == 2


def test_default_offline_marks_online_jobs_unavailable_even_with_nonempty_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    module = load_gate_module()
    fake_output = tmp_path / "gate-out"
    fake_output.mkdir(mode=0o700)
    partial_cache = fake_output / "audit-cache"
    partial_cache.mkdir()
    (partial_cache / "partial-entry.json").write_text("{}")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "OUTPUT", "gate-out")

    executed_jobs: list[str] = []

    def fail_if_called(*_args, **_kwargs):
        executed_jobs.append("called")
        raise AssertionError("Online jobs must not execute without --allow-online")

    monkeypatch.setattr(module, "run_commands", fail_if_called)

    receipt = tmp_path / "receipt.json"
    code = module.main(["--job", "dependency-audit", "--receipt", str(receipt)])
    assert code == 1
    assert executed_jobs == []

    record = json.loads(receipt.read_text())
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    assert record["status"] == "failed"
    assert record["provider_verified"] is False
    assert record["release_accepted"] is False
    for job_name in ONLINE_OPT_IN_JOBS:
        assert record["jobs"][job_name]["status"] == "unavailable"
        assert record["jobs"][job_name]["commands"] == []
        assert "--allow-online" in record["jobs"][job_name]["reason"]


def test_allow_online_selects_outbound_policy_only_for_online_jobs_across_repeat_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    module = load_gate_module()
    fake_output = tmp_path / "gate-out"
    fake_output.mkdir(mode=0o700)
    partial_cache = fake_output / "audit-cache"
    partial_cache.mkdir()
    (partial_cache / "cached.txt").write_text("partial")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "OUTPUT", "gate-out")

    observed_policies: list[tuple[str, bool]] = []

    def fake_run_commands(commands, output, *, allow_network: bool = False):
        observed_policies.append((Path(output).name, allow_network))
        return [{"command": cmd, "exit_code": 0, "log": "00.log"} for cmd in commands]

    monkeypatch.setattr(module, "run_commands", fake_run_commands)

    for run_index in range(2):
        receipt = tmp_path / f"receipt-{run_index}.json"
        code = module.main(["--allow-online", "--receipt", str(receipt)])
        assert code == 0
        data = json.loads(receipt.read_text())
        assert data["status"] == "passed"

    for job_name, allow_net in observed_policies:
        if job_name in ONLINE_OPT_IN_JOBS:
            assert allow_net is True, f"{job_name} should use outbound-permitted policy with --allow-online"
        else:
            assert allow_net is False, f"{job_name} must remain strictly offline under (deny network*)"


def test_executor_continues_after_failure_and_blocks_network(tmp_path: Path):
    module = load_gate_module()
    marker = tmp_path / "continued"
    commands = [
        [sys.executable, "-c", "raise SystemExit(7)"],
        [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
        [sys.executable, "-c", "import socket; s=socket.socket(); s.bind(('', 0))"],
        [sys.executable, "-c", "import socket; s=socket.socket(); s.connect(('localhost', 443))"],
    ]
    result = module.run_commands(commands, tmp_path)
    assert [row["exit_code"] for row in result] == [7, 0, 1, 1]
    assert marker.exists()
    for row in result:
        log_path = tmp_path / str(row["log"])
        assert log_path.is_file()
        assert stat.S_IMODE(log_path.stat().st_mode) == 0o600


def test_minimal_allowlist_env_strips_provider_and_db_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    module = load_gate_module()
    synthetic_secrets = {
        "GH_TOKEN": "synthetic-gh-token",
        "GITHUB_TOKEN": "synthetic-github-token",
        "ARM_CLIENT_SECRET": "synthetic-arm-secret",
        "PGPASSWORD": "synthetic-pg-password",
        "DB_PASSWORD": "synthetic-db-password",
        "PIP_INDEX_URL": "https://user:synthetic-token@example.invalid/simple",
        "PIP_EXTRA_INDEX_URL": "https://user:synthetic-extra@example.invalid/simple",
        "OCI_TOKEN": "synthetic-oci-token",
        "AWS_SECRET_ACCESS_KEY": "synthetic-aws-secret",
    }
    for key, val in synthetic_secrets.items():
        monkeypatch.setenv(key, val)

    env_dump = tmp_path / "child_env.json"
    cmd = [
        sys.executable,
        "-c",
        f"import json, os, pathlib; pathlib.Path({str(env_dump)!r}).write_text(json.dumps(dict(os.environ)))",
    ]
    result = module.run_commands([cmd], tmp_path / "env-job")
    assert result[0]["exit_code"] == 0
    child_env = json.loads(env_dump.read_text())

    for secret_key in synthetic_secrets:
        assert secret_key not in child_env, f"Credential variable leaked into child env: {secret_key}"

    assert child_env["HOME"] != os.environ.get("HOME")
    assert child_env["OCI_AUTH_MODE"] == "config"
    assert child_env["OCI_CONFIG_FILE"] == "/dev/null"
    assert child_env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert child_env["GOPROXY"] == "off"
    assert child_env["GOSUMDB"] == "off"
    assert str(ROOT) in child_env["PYTHONPATH"].split(os.pathsep)


def test_missing_executable_timeout_and_atomic_private_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    module = load_gate_module()

    real_run = subprocess.run

    def fake_run(argv, **kwargs):
        if "trigger-oserror-binary" in argv:
            raise FileNotFoundError("No such file or directory: trigger-oserror-binary")
        if "trigger-timeout-binary" in argv:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=300)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    results = module.run_commands(
        [["trigger-oserror-binary"], ["trigger-timeout-binary"]],
        tmp_path / "err-job",
    )
    assert [row["exit_code"] for row in results] == [127, 124]
    for row in results:
        log_file = tmp_path / "err-job" / str(row["log"])
        assert stat.S_IMODE(log_file.stat().st_mode) == 0o600

    symlink_target = tmp_path / "target.txt"
    symlink_target.write_text("preserve")
    symlink_receipt = tmp_path / "symlink-receipt.json"
    symlink_receipt.symlink_to(symlink_target)
    with pytest.raises(OSError):
        module.write_private_text(symlink_receipt, "{}\n")
    assert symlink_target.read_text() == "preserve"


def test_config_runs_every_job_including_the_isolated_audit_and_disables_automatic_repairs():
    config = yaml.safe_load((ROOT / ".no-mistakes.yaml").read_text())
    assert set(config) == {"commands", "auto_fix"}
    assert "allow_repo_commands" not in config
    assert set(config["commands"]) == {"test", "lint"}
    assert config["commands"]["test"] == "python3.11 scripts/local_ci_gate.py --allow-online"
    assert config["commands"]["lint"] == (
        "python3.11 scripts/local_ci_gate.py --job python-sast --job secret-scan "
        "--job go-gateway --job terraform-fmt --job docs-build --job container-scan"
    )
    assert "--allow-online" not in config["commands"]["lint"]
    assert config["auto_fix"] == {"review": 0, "test": 0, "lint": 0}


def test_gitignore_excludes_private_gate_and_cache_dirs():
    for candidate in (".gate/receipt.json", ".cache/local-ci/receipt.json"):
        check = subprocess.run(
            ["git", "check-ignore", "-q", candidate],
            cwd=ROOT,
            check=False,
        )
        assert check.returncode == 0, f"Expected {candidate} to be gitignored"


def test_audit_reads_only_clean_package_configs(tmp_path, monkeypatch):
    module = load_gate_module()
    observed = []

    def inspect(argv, **kwargs):
        env = kwargs["env"]
        paths = [
            Path(env[key])
            for key in ("PIP_CONFIG_FILE", "NPM_CONFIG_USERCONFIG", "NPM_CONFIG_GLOBALCONFIG")
        ]
        assert len(set(paths)) == len(paths)
        assert all(path.read_bytes() == b"" and stat.S_IMODE(path.stat().st_mode) == 0o600 for path in paths)
        assert all(path.is_relative_to(env["HOME"]) for path in paths)
        assert "deny file-read*" in argv[2]
        assert all(name in argv[2] for name in (".ssh", ".oci", ".aws", ".kube", "Keychains", ".npmrc", "env"))
        observed.append(argv[2])
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(module.subprocess, "run", inspect)
    assert module.run_commands([["audit"]], tmp_path, allow_network=True)[0]["exit_code"] == 0
    assert len(observed) == 1


@pytest.mark.parametrize("staged", [True, False])
def test_added_line_scan_reports_only_locations_and_count(tmp_path, monkeypatch, capsys, staged):
    module = load_gate_module()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    candidate = tmp_path / "sample.txt"
    candidate.write_text("safe\n")
    subprocess.run(["git", "add", "sample.txt"], cwd=tmp_path, check=True)
    subprocess.run([
        "git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-q", "-m", "fixture",
    ], cwd=tmp_path, check=True)
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=tmp_path, check=True)
    marker = "gh" + "p_" + "A1b2C3d4E5f67890G1h2I3j4K5l6M7n8O9p0"
    candidate.write_text("safe\n" + marker + "\n")
    if staged:
        subprocess.run(["git", "add", "sample.txt"], cwd=tmp_path, check=True)

    assert module.added_line_scan() == 1
    output = capsys.readouterr()
    assert output.out.splitlines() == ["sample.txt:2", "added-line findings: 1"]
    assert output.err == ""


def test_seatbelt_protects_workspace_and_home_credentials(tmp_path, monkeypatch):
    module = load_gate_module()
    workspace = tmp_path / "workspace+symbols"
    synthetic_home = tmp_path / "host-home"
    credentials = (
        workspace / ".env",
        workspace / "nested/.env.local",
        workspace / "deploy/credentials.env",
        workspace / ".npmrc",
        workspace / "nested/credentials/token",
        synthetic_home / ".ssh/key",
        synthetic_home / ".netrc",
        synthetic_home / ".git-credentials",
    )
    allowed = workspace / "ordinary.txt"
    for candidate in (*credentials, allowed):
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.touch()
    monkeypatch.setattr(module, "ROOT", workspace)
    policy = module.network_sandbox_profile(synthetic_home.resolve())
    preflight = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", policy, "/usr/bin/true"],
        capture_output=True,
        check=False,
    )
    if preflight.returncode == 71 and b"sandbox_apply: Operation not permitted" in preflight.stderr:
        pytest.skip("An outer sandbox forbids applying the outbound-enabled Seatbelt profile")
    assert preflight.returncode == 0, preflight.stderr
    for candidate in (*credentials, allowed):
        result = subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", policy, "/bin/cat", str(candidate)],
            capture_output=True,
            check=False,
        )
        assert (result.returncode != 0) == (candidate in credentials)
        if candidate in credentials:
            assert b"Operation not permitted" in result.stderr
    for candidate in (*credentials, synthetic_home / ".zprofile", workspace / "new.txt"):
        result = subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", policy, "/usr/bin/touch", str(candidate)],
            capture_output=True,
            check=False,
        )
        assert result.returncode != 0 and b"Operation not permitted" in result.stderr
    direct_dns = subprocess.run(
        [
            "/usr/bin/sandbox-exec", "-p", policy, sys.executable, "-c",
            "import errno, socket; s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); "
            "raise SystemExit(s.connect_ex(('localhost', 53)) not in (errno.EPERM, errno.EACCES))",
        ],
        capture_output=True,
        check=False,
    )
    assert direct_dns.returncode == 0, direct_dns.stderr


def test_executor_refuses_existing_public_output_directory(tmp_path, monkeypatch):
    module = load_gate_module()
    public = tmp_path / "public"
    public.mkdir()
    public.chmod(0o755)
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: pytest.fail("executed command"))
    with pytest.raises(RuntimeError, match="private"):
        module.run_commands([["nope"]], public)
    assert stat.S_IMODE(public.stat().st_mode) == 0o755
