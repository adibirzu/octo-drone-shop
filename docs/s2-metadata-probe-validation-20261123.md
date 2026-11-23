# S2 metadata probe validation

Goal: restore instance-principal credential discovery with the smallest metadata-probe fix.

Branch: `adibirzu/s2-shop-metadata-probe`.
A fresh `git fetch origin main` confirmed the starting HEAD and origin/main both equal
`9ba480fd4cb5f4f8f5174196c3cf8b756f05582e`.
The legacy synchronization was not replayed.

## Change and regression evidence

**Code-backed and locally verified.** The socket probe now uses the standard OCI metadata
service destination on port 80. The matching failure diagnostic uses that destination.
The existing 1.5-second timeout and OSError handling remain in place.
A reachable TCP port establishes reachability only; it does not verify metadata authentication.

The test-only commit `64d7e57` precedes the fix. Before production changes,
`python3.11 -m pytest -q tests/test_create_la_source.py` exited 1:

```text
4 failed, 4 passed in 1.06s
```

The failures identify the wrong destination at the socket boundary.
After the two-line production fix, the same command exited 0:

```text
8 passed in 0.84s
```

Tests assert the destination, port, default timeout, custom timeout, connection cleanup,
and False returns for connection-refused, timeout and generic OS errors.
The former credential-failure subprocess test now uses mocked sockets and a fake OCI SDK.
It asserts exit 3 and no signer or client creation.
The review follow-up test additionally verifies successful signer construction, client wiring,
and source payload with a fake SDK and mocked socket. No metadata or cloud call was made.

Test Behavior, Not Implementation shaped the tests: assert the destination sent to the
socket boundary and the externally observable credential-fallback result.

## Environment and commands

The coordinator authorized a workspace-only environment and package downloads.
`python3.11 -m venv .venv` and
`.venv/bin/python -m pip install --cache-dir .gate/pip-cache -r requirements-dev.txt`
completed with exit 0. Python is 3.11.16.
The repository requirements contain exact pins and ranges; this is not a fully locked environment.
Private `.gate/venv-frozen.txt` records resolved non-editable versions.
No global packages were installed. The completed workspace pip download cache was removed
under the coordinator's disk instruction; the environment remains ignored by Git.
Disk was above 10 GiB at each checked boundary.

Test runners retained basic local process settings and removed inherited provider, telemetry,
identity and database variables. They set ENV=test, disabled bytecode, and added the repository,
cache-client src and async-worker src directories to PYTHONPATH.
Gate tools absent from the environment used their existing host executables.
Putting .venv/bin first on PATH does not change those tools' interpreter or prove isolated tooling.

Private raw outputs and exact command arrays with real exit codes are in untracked `.gate/`.
Do not stage that directory.

| Command | System Python/tool result | Repository environment result |
| --- | --- | --- |
| `python -m pytest -q` | 0; 261 passed before review follow-up | 0; 262 passed |
| `python -m pytest -q tests/test_create_la_source.py tests/test_sso_issuer.py tests/test_postgres_boolean_queries.py` | Not repeated | 0; 77 passed |
| `cd services/async-worker && python -m pytest -q tests` | Not repeated | 0; 12 passed |
| `python -m pip check` | Not run | 0; no broken requirements |
| Synthetic health handler smoke | Not repeated | 0; HTTP 200, status ok, no application lifespan |
| `gitleaks detect --source . --redact` | 0; no findings | 0; host executable, no findings |
| `pip-audit -r requirements.txt --strict` | 1; 30 reported findings across four packages | See installed-version audit below |
| `bandit -q -r server -x server/__pycache__ -ll -iii` | 0 | 0; host executable |
| `ruff check --select S server` | 0 | 0; host executable |
| `mkdocs build --strict --site-dir .gate/site` | 1 | 1; host executable, Material/Pymdownx import failure |
| `cyclonedx-py requirements requirements.txt --spec-version 1.6 -o .gate/source.cdx.json` | 127; executable unavailable | 127; executable unavailable |

The synthetic health check imports the actual health handler into a separate FastAPI app
and uses an in-process TestClient without application startup. It does not prove database readiness.

The installed-version audit
`pip-audit --path .venv/lib/python3.11/site-packages --strict` exited 1 because
the local editable packages are unpublished. Adding `--skip-editable` still exited 1,
with the tool rejecting an editable distribution. Those runs did not complete vulnerability assessment.

A subsequent audit used the environment's exact frozen non-editable versions:

```sh
.venv/bin/python -m pip freeze --exclude-editable --disable-pip-version-check > .gate/venv-frozen.txt
pip-audit -r .gate/venv-frozen.txt --no-deps --disable-pip --strict --cache-dir .gate/audit-cache -f cyclonedx-json -o .gate/venv.cdx.json
```

The freeze exited 0; the audit exited 1 and reported 30 findings in four packages.
The audit output includes repeated advisory IDs.
Its private CycloneDX 1.4 artifact contains 88 components and 24 unique vulnerability records.
It excludes the two editable local packages, Node, Go, containers and OS packages.
It is a supplementary inventory, not a pass for the unavailable specified SBOM command.
Dependency fixes remain outside S2.

## Gateway gate

The initial offline `go test ./...` in services/workflow-gateway exited 1 because
the required modules were absent from the workspace-local cache.
After coordinator-approved downloads using the checked-in module manifest,
the same command with workspace-local GOCACHE, GOMODCACHE and GOPATH exited 1 under Go 1.27.1.
The reachable failure is vet rejecting non-constant fmt.Errorf formats at
`internal/workflows/service.go:175` and `:196`.
Those source lines are unchanged by S2. No vet check was disabled.
The gateway gate remains failed.

## Review and delivery

Installed Claude CLI reviewed only the selected code/test diff on stdin.
Both review commands exited 0 with tools disabled, strict empty MCP configuration,
safe mode, slash commands disabled, empty settings sources and no session persistence.
The first review requested the successful fallback test. That finding was fixed.
The follow-up verdict was "No actionable findings."
A blank-line nit and unit marker suggestion were also addressed.
The review did not execute code or independently rerun gates.
No application comments were added; the requested no-helper constraint prevented a comment subagent.
The cursor-team-kit deslop command was unavailable; the narrow diff was inspected directly.

**Gate S remains failed.** Audit findings, documentation tooling, the specified SBOM tool
and gateway vet require separate follow-up. No provider verification or release acceptance is claimed.
Only the local test commit and the final fix/report commit are authorized.
No push, PR, deployment, image build, metadata call or cloud call occurred.
