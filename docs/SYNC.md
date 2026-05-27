# Sync Runbook: octo-observability-demo (monorepo) → octo-drone-shop (standalone)

This standalone repo is a downstream of the **`shop/`** subtree of
[`adibirzu/octo-observability-demo`](https://github.com/adibirzu/octo-observability-demo)
(formerly `octo-apm-demo`) plus two vendored shared Python packages.

Run this procedure whenever the standalone falls behind the monorepo, or when
the monorepo's `shop/`, `services/cache/client`, or `services/async-worker`
get meaningful updates.

## Prerequisites

- Local clones of both repos:
  - `~/dev/octo-apm-demo` (monorepo source of truth)
  - `~/dev/octo-drone-shop` (standalone sync target)
- Python ≥3.12 for venv smoke
- Docker (optional, for build smoke)
- `gh` CLI authenticated against `adibirzu/octo-drone-shop`

## Scope: what to sync

| Source (monorepo) | Destination (standalone) |
|---|---|
| `shop/*` (all contents) | repo root (flattened) |
| `services/cache/client/` | `services/cache/client/` |
| `services/async-worker/` | `services/async-worker/` |

`shop/services/workflow-gateway/` rides along with the `shop/` flatten — it
becomes `services/workflow-gateway/` in the standalone, sibling to the two
vendored packages.

**Do not sync** the rest of `services/*` from the monorepo — those
(`apm-java-demo`, `auto-remediator`, `browser-runner`, etc.) belong to the
broader demo and are not used by the drone shop.

## Local-only files to preserve

These exist only in the standalone repo (cap-tenancy operational tooling):

- `deploy/k8s/apply-cap-hardening.sh`
- `scripts/demo/cap_smoke.sh`
- `tests/test_database_migrations.py`

Before any `rsync --delete`, back these up:

```bash
mkdir -p /tmp/octo-drone-shop-preserve
cp deploy/k8s/apply-cap-hardening.sh   /tmp/octo-drone-shop-preserve/
cp scripts/demo/cap_smoke.sh           /tmp/octo-drone-shop-preserve/
cp tests/test_database_migrations.py   /tmp/octo-drone-shop-preserve/
```

After sync, restore them.

## Procedure

### 1. Inspect dirty tree

```bash
cd ~/dev/octo-drone-shop
git status --short
```

For every `M` file, check whether the change is local-only or stale
relative to the monorepo:

```bash
diff -q tests/test_logging_sdk.py ~/dev/octo-apm-demo/shop/tests/test_logging_sdk.py
```

Hack-patches that pre-date a proper monorepo migration (e.g. inline DDL in
`server/database.py`) should be **discarded** during sync — the monorepo's
alembic migrations supersede them.

### 2. Sync the shop tree

```bash
rsync -a --delete \
  --exclude='/.git' \
  --exclude='__pycache__/' \
  --exclude='node_modules/' \
  --exclude='playwright-report/' \
  --exclude='test-results/' \
  --exclude='build/' \
  --exclude='site/' \
  --exclude='.env' \
  --exclude='.env.*' \
  --exclude='wallet/' \
  --exclude='*.log' \
  ~/dev/octo-apm-demo/shop/ \
  ~/dev/octo-drone-shop/
```

`site/` is excluded because it contains cap-tenancy-specific docs the
standalone owns separately.

### 3. Vendor the two shared Python packages

```bash
rsync -a --delete --exclude='__pycache__/' --exclude='*.egg-info/' \
  ~/dev/octo-apm-demo/services/cache/client/ \
  ~/dev/octo-drone-shop/services/cache/client/

rsync -a --delete --exclude='__pycache__/' --exclude='*.egg-info/' \
  ~/dev/octo-apm-demo/services/async-worker/ \
  ~/dev/octo-drone-shop/services/async-worker/
```

### 4. Restore preserved cap-tenancy files

```bash
cp /tmp/octo-drone-shop-preserve/apply-cap-hardening.sh       deploy/k8s/
cp /tmp/octo-drone-shop-preserve/cap_smoke.sh                 scripts/demo/
cp /tmp/octo-drone-shop-preserve/test_database_migrations.py  tests/
chmod +x deploy/k8s/apply-cap-hardening.sh scripts/demo/cap_smoke.sh
```

### 5. Rewrite paths for standalone layout

The monorepo references `shop/` and `../services/` paths that must become
root-relative in the standalone.

**`Dockerfile`** — replace:
- `COPY shop/requirements.txt shop/requirements.txt` → `COPY requirements.txt requirements.txt`
- `pip install ... -r shop/requirements.txt` → `pip install ... -r requirements.txt`
- `COPY shop/server /app/server` → `COPY server /app/server`
- Update header comment: "BUILD CONTEXT IS THE REPO ROOT (octo-drone-shop/)"

**`requirements-dev.txt`** — replace:
- `-e ../services/cache/client` → `-e ./services/cache/client`
- `-e ../services/async-worker` → `-e ./services/async-worker`

### 6. Redaction gates (mandatory before commit)

Per `~/.claude/CLAUDE.md`, run these on the staged diff:

The exact grep patterns live in `~/.claude/CLAUDE.md` ("Redaction Convention").
Run them against the staged diff:

```bash
git add -A
~/.claude/scripts/redaction-gate.sh   # or paste the patterns from CLAUDE.md
```

If a hit appears, redact in-place using placeholders. The real values resolve
via the private (mode `0600`, never committed) `~/.claude/private/octo-apm-redactions.md`.
Typical substitutions for this repo:

- Tenancy namespace → `${OCIR_TENANCY}`
- Region host → `${OCIR_REGION}.ocir.io`
- Image SHA256 digests → `<SHOP_IMAGE_DIGEST>`, `<CRM_IMAGE_DIGEST>`

### 7. Smoke tests

**Syntax:**

```bash
python3 -c "
import ast, pathlib, sys
errs = 0
for p in pathlib.Path('server').rglob('*.py'):
    try: ast.parse(p.read_text())
    except SyntaxError as e: print(f'{p}: {e}'); errs += 1
sys.exit(1 if errs else 0)
"
```

Also grep for unresolved merge conflict markers (a real bug was caught this
way in the May 2026 sync — see commit `b022d46`):

```bash
grep -rn '^<<<<<<< \|^>>>>>>> ' --include='*.py' --include='*.md' . | grep -v node_modules
```

**Compose:**

```bash
touch .env.local deploy/credentials.env
docker compose config >/dev/null && echo OK
rm -f .env.local; [ -s deploy/credentials.env ] || rm -f deploy/credentials.env
```

**Venv import + pytest collect:**

```bash
python3.12 -m venv /tmp/v && source /tmp/v/bin/activate
pip install --quiet --upgrade pip setuptools wheel
pip install --quiet -r requirements-dev.txt
python -c "import server.main"
python -m pytest --collect-only -q
deactivate; rm -rf /tmp/v
```

**Docker build (ARM-native sanity only — DO NOT build `--platform linux/amd64`
locally per global CLAUDE.md; use `control-plane-oci` VM for x86 deploy
builds):**

```bash
docker build -t octo-drone-shop:smoke-arm .
docker image rm octo-drone-shop:smoke-arm
```

### 8. Commit and push

Single commit referencing the monorepo SHA being synced:

```bash
git commit -m "sync: pull latest application code from octo-apm-demo monorepo @ <SHA>

<changelog summary>"

unset GITHUB_TOKEN  # if GITHUB_TOKEN env var is invalid; keyring will be used
gh auth setup-git
git push origin main
```

## Common rewrites that have come up

| Monorepo path | Standalone path |
|---|---|
| `shop/requirements.txt` | `requirements.txt` |
| `shop/server/` | `server/` |
| `shop/Dockerfile` | `Dockerfile` |
| `shop/services/workflow-gateway/` | `services/workflow-gateway/` |
| `../services/cache/client` | `./services/cache/client` |
| `../services/async-worker` | `./services/async-worker` |
| `docker build -f shop/Dockerfile -t … .` (from monorepo root) | `docker build -t … .` (from standalone root) |

## Notes

- The monorepo origin is `https://github.com/adibirzu/octo-observability-demo.git`
  (renamed from `octo-apm-demo`). Old URL still 301-redirects.
- The standalone repo origin is `https://github.com/adibirzu/octo-drone-shop.git`.
  Despite the monorepo rename, the standalone keeps its `drone-shop` name.
- `services/cache/client` and `services/async-worker` are vendored, not
  pip-installed from a registry. They build clean Python wheels (`octo-cache-client`,
  `octo-async-worker`) during the standalone Dockerfile's builder stage.
- The `apm-java-demo` and other non-shop services in the monorepo stay
  monorepo-only — they belong to the broader demo, not the shop.
