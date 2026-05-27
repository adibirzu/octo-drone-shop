# Kubernetes deploy artefacts

Manifests and helper scripts for deploying the drone shop to OKE.

## Files

| File | Purpose |
|---|---|
| `deployment.yaml` | Main app + workflow-gateway Deployment, Service, Ingress |
| `workflow-gateway.yaml` | Standalone workflow-gateway Deployment |
| `workflow-gateway-ingress.yaml` | Ingress for workflow-gateway |
| `secret-provider-class.yaml` | OCI Vault → CSI driver wiring |
| `apply-cap-hardening.sh` | Idempotent overlay applying cap-tenancy hardening |

## Environment variables (cap-tenancy overlay)

`apply-cap-hardening.sh` reads image refs from env vars to keep the script
free of tenancy-private identifiers. Set them before running:

| Variable | Value source | Example resolution |
|---|---|---|
| `OCIR_REGION` | Region of the OCIR registry | `eu-frankfurt-1` |
| `OCIR_TENANCY` | Tenancy namespace for OCIR | resolve via `~/.claude/private/octo-apm-redactions.md` ("OCIR namespace, cap") |
| `SHOP_IMAGE_DIGEST` | SHA256 digest of the pinned shop image | `kubectl get deploy mushop-portal -n mushop-portal -o jsonpath='{.spec.template.spec.containers[0].image}'` then take the `@sha256:…` portion |
| `CRM_IMAGE_DIGEST` | SHA256 digest of the pinned CRM image | same lookup against `enterprise-crm-portal` deployment |

Real values are not committed to this repo. They resolve via:

- Local Mac → `~/.claude/private/octo-apm-redactions.md` (mode `0600`, never synced)
- Control-plane VM → environment variables exported during cluster bootstrap

Once exported, the script substitutes them into `SHOP_IMAGE` and `CRM_IMAGE`:

```bash
export OCIR_REGION=…
export OCIR_TENANCY=…
export SHOP_IMAGE_DIGEST=…
export CRM_IMAGE_DIGEST=…
./apply-cap-hardening.sh
```

The script targets the **cap** profile only — it pins shop and CRM deployments
to specific image digests and applies ingress-only NetworkPolicies. Egress is
intentionally unrestricted so ATP, APM, OCIR, DNS, and other outbound OCI
dependencies continue to work.

## Other tenancies

For `emdemo` (production) or `DEFAULT` (oci4cca test) deployments, do **not**
use the cap-hardening script. Apply the base manifests directly:

```bash
kubectl apply -f deployment.yaml -n <namespace>
```

See the top-level `deploy/oci/` and `deploy/terraform/` directories for the
tenancy-aware deployment paths.
