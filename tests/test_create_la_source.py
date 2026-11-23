"""Tests for tools/create_la_source.py — the Log Analytics source/parser
registrar for app JSON logs.

The script is plan-only by default: it must print the request payload that
WOULD be sent to OCI and exit 0 without calling out. --apply is required
for the actual SDK call.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools" / "create_la_source.py"


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    e = dict(os.environ)
    if env is not None:
        e.update(env)
    return subprocess.run(
        ["python3", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=e,
        timeout=20,
    )


@pytest.mark.portability
def test_script_exists() -> None:
    assert SCRIPT.exists(), f"missing: {SCRIPT}"


@pytest.mark.portability
def test_dry_run_emits_payload_json() -> None:
    result = _run(
        "--la-namespace",
        "acme",
        "--la-log-group-id",
        "ocid1.loganalyticsloggroup.oc1..xxxx",
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["name"] == "octo-shop-app-json"
    assert "parser" in payload
    # Core fields we expect the parser to extract
    for field in ("timestamp", "level", "trace_id", "oracleApmTraceId"):
        assert field in json.dumps(payload)


@pytest.mark.portability
def test_dry_run_does_not_require_oci_sdk() -> None:
    """Dry run must not import oci.log_analytics."""
    result = _run(
        "--la-namespace",
        "acme",
        "--la-log-group-id",
        "ocid1.loganalyticsloggroup.oc1..xxxx",
    )
    assert "ModuleNotFoundError" not in result.stderr
    assert result.returncode == 0


@pytest.mark.portability
def test_apply_without_credentials_fails_clearly(monkeypatch, capsys) -> None:
    from tools import create_la_source

    sdk = SimpleNamespace(
        config=SimpleNamespace(from_file=MagicMock(side_effect=OSError("no config"))),
        auth=SimpleNamespace(signers=SimpleNamespace(
            InstancePrincipalsSecurityTokenSigner=MagicMock()
        )),
        log_analytics=SimpleNamespace(LogAnalyticsClient=MagicMock()),
    )
    monkeypatch.setitem(sys.modules, "oci", sdk)
    with patch.object(create_la_source.socket, "create_connection", side_effect=OSError):
        result = create_la_source._main([
            "--apply", "--la-namespace", "example",
            "--la-log-group-id", "example-log-group",
        ])
    assert result == 3
    assert "cannot resolve credentials" in capsys.readouterr().err
    sdk.auth.signers.InstancePrincipalsSecurityTokenSigner.assert_not_called()
    sdk.log_analytics.LogAnalyticsClient.assert_not_called()


@pytest.mark.unit
def test_imds_probe_targets_metadata_with_short_timeout() -> None:
    from tools import create_la_source

    with patch.object(create_la_source.socket, "create_connection") as connect:
        assert create_la_source._imds_reachable() is True
    connect.assert_called_once_with(("169.254.169.254", 80), timeout=1.5)
    connect.return_value.__exit__.assert_called_once()


@pytest.mark.unit
@pytest.mark.parametrize("error", [ConnectionRefusedError, TimeoutError, OSError])
def test_imds_probe_connection_errors_return_false(error) -> None:
    from tools import create_la_source

    with patch.object(create_la_source.socket, "create_connection", side_effect=error) as connect:
        assert create_la_source._imds_reachable(timeout=0.25) is False
    connect.assert_called_once_with(("169.254.169.254", 80), timeout=0.25)
