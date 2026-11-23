# S3 Shop security gates, 2026-11-23

Goal: clear code-owned gate S failures while preserving Shop behavior.

Branch: `adibirzu/s3-shop-security-gates`.
Base: fresh `origin/main`, `9ba480fd4cb5f4f8f5174196c3cf8b756f05582e`.
The S2 branch remains separate at `b9ba898`; S2 was not cherry-picked.
Evidence is **code-backed** and **locally verified**, not provider verified or release accepted.
No push, PR, deployment, cloud call or metadata call is authorized or performed.

## 1. Requirements audit

Baseline `pip-audit -r requirements.txt --strict` exited 1 with 30 reported findings
across four packages, including repeated advisory IDs. After the changes it exits 0,
"No known vulnerabilities found."

| Dependency set | Before | After |
| --- | --- | --- |
| OTel API, SDK, OTLP HTTP exporter | 1.42.1 | 1.45.2 |
| OTel instrumentation and Prometheus exporter | 0.63b1 | 0.66b2 |
| OCI SDK | 2.175.0 | 2.184.1 |
| cryptography | resolved 46.0.7 | exact 50.0.0 |
| PyJWT[crypto] | 2.13.0 | 2.15.1 |
| python-multipart | 0.0.29 | 0.0.31 |

Live PyPI release metadata confirmed SDK 1.45.2 pins API 1.45.2 and semantic
conventions 0.66b2; instrumentation uses the matching 0.66b2 family.
OCI 2.184.1 admits cryptography 50 and requires compatible OpenSSL support and crc32c.
These follow the C3 security-fixed sets. Unrelated direct requirements remain unchanged.
Shop already uses PyJWT and requires exp, iat, iss and aud. No python-jose replacement
or JWT claim-policy change was needed.

The reused workspace .venv was updated from repository requirements using
`python -m pip install --no-cache-dir -r requirements-dev.txt`, exit 0.
Free disk was checked above 10 GiB before the install.
The full offline Shop suite passed 257 tests afterward, exit 0.
`python -m pip check` exited 0. Bandit and Ruff S also exited 0.
Requirements/advisory failure before the update and clean audit afterward are the
RED/GREEN check for these dependency-only changes; existing SSO behavior tests ran in the full suite.
Private exact command/exit receipts are under `.gate/s3/dependencies/`.

## Test isolation

Commands use Python 3.11.16 in the reused workspace .venv, ENV=test, disabled bytecode,
and local cache-client/async-worker sources on PYTHONPATH. Provider, database, identity
and telemetry variables inherited from the host are omitted by the runner.
Tests additionally load a private sitecustomize that makes socket.create_connection
raise OSError. This prevents the original main-branch --apply subprocess test from
probing its documentation-example destination. This private test guard is not part
of the product or a substitute for S2's mocked regression tests.
No application startup, shared database, identity provider or OCI verification is claimed.
