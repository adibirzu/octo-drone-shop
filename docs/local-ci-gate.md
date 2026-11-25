# Local CI gate

The gate runs on macOS using Seatbelt. Use Python 3.11 and install the development
requirements plus Bandit, pip-audit, and Ruff in `.gate/venv`. Make Git, Gitleaks
8.30+, Go, Node.js/npm, Semgrep, Terraform, TFLint, and Trivy available on PATH
or in `.gate/bin`. Preload the Go modules from the gateway's checked-in
`go.mod` and `go.sum` before running offline.

Save the Semgrep Registry YAML packs `p/owasp-top-ten`, `p/python`, and
`p/fastapi` under `.gate/semgrep-rules/` as `owasp-top-ten.yml`, `python.yml`,
and `fastapi.yml`. Acquire scanner binaries and rules as a separate setup step.
The runner makes no attempt to fetch or replace missing tools or rules.

| GitHub workflow job | Local job | Check |
| --- | --- | --- |
| `security-gates / python-sast` | `python-sast`, `dependency-audit` | Bandit and Ruff S on `server`, the three Semgrep packs, and strict pip-audit on `requirements.txt` |
| `security-gates / node-audit` | `dependency-audit` | npm audit at high severity using the committed lockfile, omitting dev dependencies and package scripts |
| `security-gates / secret-scan` | `secret-scan` | Gitleaks over added lines in commits ahead of `origin/main`, the index, and tracked working-tree edits, with unique file/line locations and a count |
| `security-gates / terraform-lint` | `terraform-fmt` | Recursive Terraform format check, TFLint initialization and recursive lint |
| `security-gates / container-scan` | `container-scan` | Trivy configuration scan with bundled checks at HIGH and CRITICAL severity |
| `Deploy Documentation / build` and `mkdocs-deploy / build-and-deploy` | `docs-build` | Strict MkDocs build with Material's Mermaid support |
| `Deploy Documentation / deploy` and Pages publication steps | GitHub Pages | Upload and deployment require the hosted workflow |
| Additional local checks | `unit-tests`, `go-gateway` | Root and async-worker pytest suites, plus gateway vet and build |

Run the full gate with `.gate/venv/bin/python scripts/local_ci_gate.py --allow-online`.
Only `dependency-audit` can connect out, over HTTPS with system DNS. It uses the public
PyPI and npm registries, a temporary home, and separate empty package-manager
configs. Seatbelt denies reads of workspace and host credential paths and denies
writes to the workspace and host home. Omitting `--allow-online` makes the audit
unavailable and the aggregate fails. The no-mistakes test command opts into
the audit. Its lint command runs offline.

The receipt and command logs live under `.gate/local-ci` by default. Keep them
private. A command or scanner failure makes the aggregate fail. Stage new files
to include them in the added-line scan.
