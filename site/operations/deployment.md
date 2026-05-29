# Deployment

The current unified deployment surface lives in
[`adibirzu/octo-apm-demo`](https://github.com/adibirzu/octo-apm-demo).
Use that repository for new OKE, private Compute, Resource Manager, or
single-VM deployments. This page remains as the service-level OKE
rollout reference for Drone Shop.

For the production-demo path without Kubernetes, use the
[Private Compute Deployment](https://adibirzu.github.io/octo-apm-demo/getting-started/compute-deployment/)
stack. It deploys Shop and CRM on private Podman Compute instances with
public OCI LB/WAF, private ATP, APM, OCI Logging, Log Analytics, DB
Management, Operations Insights, and Stack Monitoring Standard.

## Automated Deploy Script

```bash
./deploy/deploy.sh                  # Build + push + rollout
./deploy/deploy.sh --build-only     # Build + push, no rollout
./deploy/deploy.sh --rollout-only   # Rollout existing latest tag
```

## Manual Workflow

### 1. Sync to Build VM

```bash
rsync -az --exclude '.git' --exclude '__pycache__' \
  . remote-builder:/tmp/octo-drone-shop/
```

### 2. Build (Native x86_64)

```bash
TAG=$(date +%Y%m%d%H%M%S)
ssh remote-builder "cd /tmp/octo-drone-shop && \
  docker build -t ${OCIR_REPO}/octo-drone-shop:${TAG} \
               -t ${OCIR_REPO}/octo-drone-shop:latest ."
```

### 3. Push to OCIR

```bash
ssh remote-builder "docker push ${OCIR_REPO}/octo-drone-shop:${TAG} && \
  docker push ${OCIR_REPO}/octo-drone-shop:latest"
```

### 4. Rollout on OKE

```bash
kubectl set image deployment/octo-drone-shop \
  app=${OCIR_REPO}/octo-drone-shop:${TAG} -n octo-drone-shop

kubectl rollout status deployment/octo-drone-shop -n octo-drone-shop
```

## K8s Configuration

- **Replicas**: 2
- **Resources**: 250m CPU / 512Mi (request), 1 CPU / 1Gi (limit)
- **Liveness**: `/health` every 15s (12s initial delay)
- **Readiness**: `/ready` every 12s (18s initial delay)
- **Image pull**: OCIR with `ocir-pull-secret`
- **ATP wallet**: Mounted as read-only volume

## Cap Profile Runbook (`example.test`)

`example.test` currently runs on the cap profile. Use explicit context and
profile flags:

```bash
kubectl --context <kube-context> ...
oci --profile cap ...
```

Do not use `DEFAULT` for `example.test`; it is reserved for later tests with
different domains.

Current live objects:

| Host | Namespace | Deployment | Service | Ingress |
|---|---|---|---|---|
| `shop.example.test` | `mushop-portal` | `mushop-portal` | `mushop-portal` | `octodemo-shop` |
| `crm.example.test` | `enterprise-crm` | `enterprise-crm-portal` | `enterprise-crm-portal` | `octodemo-crm` |

Smoke check:

```bash
./scripts/demo/cap_smoke.sh
```

If either public root returns nginx `404 Not Found`, check ingress presence
first:

```bash
kubectl --context <kube-context> get ingress -A | grep example
```

If `/api/dashboard/summary` returns 500 and logs mention
`payment_provider_reference`, run the current shop image or migration against
the shared ATP; the live schema must include `orders.payment_provider` and
`orders.payment_provider_reference`.

### Temporary CRM Metrics Hotfix

The live cap CRM deployment currently mounts `configmap/crm-metrics-hotfix`
over `/app/server/observability/metrics.py`. This disables unsupported OTLP
metric export to OCI APM while keeping:

- OCI APM traces
- OCI APM RUM
- Prometheus `/metrics`

Remove the mount after rebuilding and promoting a CRM image that includes the
same code change:

```bash
kubectl --context <kube-context> get deploy enterprise-crm-portal -n enterprise-crm \
  -o jsonpath='{range .spec.template.spec.containers[0].volumeMounts[*]}{.name}{" "}{.mountPath}{"\n"}{end}'
```

The current hotfix mount is named `metrics-hotfix` and points at
`configmap/crm-metrics-hotfix`. After the fixed image is active, remove the
mount and delete the ConfigMap:

```bash
kubectl --context <kube-context> patch deploy enterprise-crm-portal \
  -n enterprise-crm \
  --type=strategic \
  -p '{"spec":{"template":{"spec":{"containers":[{"name":"app","volumeMounts":[{"name":"metrics-hotfix","$patch":"delete"}]}],"volumes":[{"name":"metrics-hotfix","$patch":"delete"}]}}}}'

kubectl --context <kube-context> rollout status deploy/enterprise-crm-portal \
  -n enterprise-crm
kubectl --context <kube-context> delete configmap crm-metrics-hotfix \
  -n enterprise-crm
```

### Image Promotion and Rollback

Record the current image before each cap rollout:

```bash
kubectl --context <kube-context> get deploy mushop-portal -n mushop-portal \
  -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
kubectl --context <kube-context> get deploy enterprise-crm-portal -n enterprise-crm \
  -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
```

Promote immutable tags instead of relying only on `latest`:

```bash
kubectl --context <kube-context> set image deploy/mushop-portal \
  -n mushop-portal \
  mushop=<region>.ocir.io/<tenancy-namespace>/octo-drone-shop:<tag>

kubectl --context <kube-context> set image deploy/enterprise-crm-portal \
  -n enterprise-crm \
  app=<region>.ocir.io/<tenancy-namespace>/enterprise-crm-portal:<tag>
```

Rollback uses the Kubernetes rollout history:

```bash
kubectl --context <kube-context> rollout undo deploy/mushop-portal -n mushop-portal
kubectl --context <kube-context> rollout undo deploy/enterprise-crm-portal -n enterprise-crm
```
