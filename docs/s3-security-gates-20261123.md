# S3 Shop security gates, 2026-11-23

Goal: clear code-owned gate S failures while preserving Shop behavior.

Branch: `adibirzu/s3-shop-security-gates`.
Base: fresh `origin/main`, `9ba480fd4cb5f4f8f5174196c3cf8b756f05582e`.
The S2 branch remains separate at `b9ba898`; S2 was not cherry-picked.
Evidence is **code-backed** and **locally verified**, not provider verified or release accepted.
No push, PR, deployment, cloud call or instance-metadata call was performed.
External requests were limited to package-index/advisory access and the requested Claude reviews.

## 1. Requirements audit

Baseline `pip-audit -r requirements.txt --strict` exited 1 with 30 reported findings
across python-multipart, opentelemetry-api, PyJWT and cryptography, including repeated advisory IDs. After the changes it exits 0,
"No known vulnerabilities found."

| Dependency set | Before | After | Reason |
| --- | --- | --- | --- |
| OTel API, SDK, OTLP HTTP exporter | 1.42.1 | 1.45.2 | API advisory; SDK/exporter coupling |
| OTel instrumentation and Prometheus exporter | 0.63b1 | 0.66b2 | Exact semantic-convention coupling |
| OCI SDK | 2.175.0 | 2.184.1 | Admits fixed cryptography 50 |
| cryptography | resolved 46.0.7 | exact 50.0.0 | cryptography advisories |
| PyJWT[crypto] | 2.13.0 | 2.15.1 | JWT advisories |
| python-multipart | 0.0.29 | 0.0.31 | Multipart advisories |

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
and async-worker source on PYTHONPATH, with both local packages installed editable. Provider, database, identity
and telemetry variables inherited from the host are omitted by the runner.
Tests additionally load a private sitecustomize that makes socket.create_connection
raise OSError. This prevents the original main-branch --apply subprocess test from
probing its documentation-example destination. This private test guard is not part
of the product or a substitute for S2's mocked regression tests.
The root suite does not run application lifespan. Shared database, identity-provider and OCI verification remain unverified.
The test count must be re-baselined once S2 and its mocked regression tests land.

## 2. Bandit and Ruff security checks

Both baseline and post-dependency scans are clean:
`bandit -q -r server -x server/__pycache__ -ll -iii` and
`ruff check --select S server` exit 0.
No code issue, demo exception or new suppression was found or added.
This item records verified scanner results only; it does not modify application code.
A clean configured scanner result is not proof that the demo is production-safe.

## 3. Strict documentation build

The reused environment initially had no MkDocs module; its strict-build command exited 1.
The older host tool also failed importing Material/Pymdownx.
requirements-dev.txt includes the shared requirements-docs.txt, which pins MkDocs 1.6.1, Material 9.8.2,
mkdocs-mermaid2-plugin 1.2.3 and pymdown-extensions 13.1.2.
The local strict build now exits 0 with the existing mkdocs.yml.
The full Shop suite passes 257 tests and pip check exits 0.
No content, navigation warning level or strict setting was weakened.
Build output stays private under .gate/s3/docs/site.
These pins reproduce the docs-tool selection, not every transitive dependency.

## 4. Workflow gateway vet

Before the change, the actual `go test ./...` command exited 1 because vet rejected
dynamic fmt.Errorf format strings at the two workflow error-return sites.
Both now use errors.New to preserve the returned database message literally,
including any percent signs. No query, database configuration or scheduling behavior changed.
After the change, `go test ./...` exits 0 with vet enabled. All gateway packages
currently report no test files, so this proves compilation and vet, not runtime
database or gateway behavior. No new abstraction or external test dependency was added.

Go 1.27.1 uses workspace-local GOMODCACHE, GOCACHE and GOPATH under .gate.
The module versions were downloaded from checked-in go.mod/go.sum during S2 under
coordinator authority; this S3 run reused that cache with GOPROXY=off and GOSUMDB=off.
No new module, dependency version or live database connection was needed.
Private RED/GREEN logs are .gate/s3/go-red.log and .gate/s3/go-green.log.

## 5. Development environment inventory and coverage

[Resolved Python inventory](security/s3-resolved-20261123.cdx.json) is the unmodified
CycloneDX 1.4 output of the installed pip-audit CLI, using:

```sh
pip-audit --path .venv/lib/python3.11/site-packages --cache-dir .gate/s3/audit-cache -f cyclonedx-json -o .gate/s3/inventory/resolved.cdx.json
cp .gate/s3/inventory/resolved.cdx.json docs/security/s3-resolved-20261123.cdx.json
```

The audit exited 1, not 0; copying the output exited 0. The tracked SHA-256 is
`eb31c2ac3fd6672af582c108ccb05b71bea2035b1e72abf6d4496b3cecb4c282`.
Byte comparison against the original output passed.
This reused inventory includes runtime, development and documentation packages; it is not a shipped-image inventory.
The 114 components match the resolved versions of 114 installed distributions.
A comparison against 116 unique installed distribution names found no extra component or
version mismatch. The two unpublished editable local packages, octo-cache-client and
octo-async-worker, are omitted by pip-audit's environment collector; they are not
represented as audited PyPI components. This is an explicit inventory limitation.
The source scanners and local tests cover their use separately; that is not a dependency audit.
The SBOM contains flat component references, not a verified complete dependency graph,
and excludes Node, Go, container and operating-system inventories.

The remaining SBOM finding is GHSA-xmj3-9gf7-h52w on workspace build tool
setuptools 84.0.0, concerning its legacy Windows wheel-installation path.
It is not a repository runtime requirement and was also documented in C3.
No vulnerability was ignored or suppressed, and no unrequested build-tool upgrade was made.
The requirements audit remains independently clean with --strict.
A clean resolved-environment audit is therefore not claimed.

Coverage commands use the existing pytest-cov installation and no invented minimum.
All commands exited 0:

| Scope | Tests | Statement coverage | Branch coverage | Combined coverage.py result |
| --- | --- | --- | --- | --- |
| Root suite, server and tools, branch measurement enabled | 257 passed | 4719 / 7748, 60.91% | 612 / 1540, 39.74% | 57.40% |
| Separate async-worker suite, octo_async_worker | 12 passed | 183 / 453, 40.40% | 11 / 96, 11.46% | 35.34% |
| Go gateway packages | No test files | 0.0% | Not measured | Compilation and vet only |

The root command is
`python -m pytest -q --cov=server --cov=tools --cov-branch --cov-report=json:<private-output> --cov-report=term:skip-covered`.
The worker command selects services/async-worker/tests and uses --cov=octo_async_worker
with the same branch and report options.
Go uses `go test -cover ./...` with the same workspace-local offline cache settings.
Root coverage includes 80 files. Subprocess-only script execution is not measured by
this coverage configuration; tools/create_la_source.py consequently reports zero coverage.
These numbers do not establish live integrations or comprehensive security coverage.
Private JSON coverage reports and raw logs are under .gate/s3/coverage/.

## Commit and gate receipts

Each numbered item has its own local commit. The full root suite ran after each commit,
with actual command/exit receipts in .gate/s3/itemN-committed/.
Items 1 through 4 each passed 257 tests with exit 0.
The earlier dependency check before item 1 also passed 257 tests.
Bandit, Ruff S and strict requirements audit were already verified after dependency changes;
strict MkDocs and gateway vet became green in their respective items.
Final committed-head receipts apply to the sixth review-fix commit and are recorded
under .gate/s3/final/, after all five numbered item commits.

The full development-tool environment is reused, not a lockfile or hermetic reconstruction.
Only the declared runtime security sets and four documentation tools were changed.
Runtime package verification and docs tooling ran in the workspace .venv; the existing
host pip-audit, Bandit, Ruff, gitleaks, Go and Claude executables were reused.
All installs were workspace-local, sequential, --no-cache-dir and preceded by disk checks.
Free disk remained above the 10 GiB stop threshold.

Code-owned gate S checks are locally green. The resolved-environment SBOM audit remains
failed on setuptools, and the editable-package inventory gap remains explicit.
No provider, identity-client, shared-database or release acceptance is claimed.

## Review follow-up

Claude reviewed the selected complete diff on stdin with safe mode, no built-in tools,
strict empty MCP configuration, no slash commands or session persistence.
It exited 0 and found no blocking defects. It did not execute checks.
Accepted clarifications name advisory-driven versus coupling-driven upgrades, record
the actual SBOM command/copy/hash, and label the development inventory and test limits.
The HTTP-transport and OTLP-common packages in the inventory are exact requirements of
the new OTLP HTTP exporter; they are not unexplained leftovers.
Redis instrumentation is required by services/async-worker/pyproject.toml.
The local package OTel lower bounds admit the upgraded family, and pip check passes.

An additional offline compatibility smoke exited 0. It constructed the OTel SDK,
OTLP HTTP exporter, Prometheus reader and HTTPX/FastAPI/SQLite instrumentation,
emitted spans to an in-memory exporter, and signed and verified a generated RSA
PyJWT token using cryptography 50. OCI and Oracle driver imports succeeded.
Configured OTLP transmission, OCI client calls and real database/IdP verification remain unverified.
Private receipt: .gate/s3/offline-compat.log.

The review's publication concern does not match the checked configuration:
mkdocs.yml sets docs_dir to site, so this report and SBOM under docs are outside
the built site. No publication action was taken.
The review also identified that both existing docs workflows installed unpinned tools.
A focused configuration check exited 1 before the correction because their install
specifications did not match the development docs pins.
Both workflows now install requirements-docs.txt; requirements-dev.txt includes that
same file. Changes to the shared pins trigger each existing docs pipeline.
The same focused check now exits 0. No deployment step, permission or concurrency
configuration was changed, and neither deployment workflow was executed.
The latest working-tree strict MkDocs build exited 0, recorded in
.gate/s3/review-fixes/docs.json. The final committed-head rerun is recorded
under .gate/s3/final/docs.json; the final root suite receipt is .gate/s3/final/full.json.
This extra review-fix commit follows the five numbered item commits.

The follow-up Claude review exited 0 and found no actionable correctness or security
findings. It statically checked the final workflow, requirements, Go, SBOM and report
diff, and did not independently verify execution receipts.
Its final wording suggestions clarified instance-metadata versus package metadata
and the sixth-commit receipt scope.

| Item | Commit |
| --- | --- |
| Runtime security dependencies | 3c639a2 |
| Clean Bandit/Ruff evidence | 6ceeec8 |
| Pinned local docs tooling | 563611d |
| Gateway vet correction | 1c7b681 |
| Resolved inventory and coverage | 96cc204 |
| Shared CI docs pins and review corrections | Commit containing this final report |

The final commit hash and full final command exits are recorded in the private
.gate/s3/final-receipts.md and the coordinator completion report.
