# Enhancement Plan

This page defines the recommended rollout for turning OCTO into a complete OCI
observability showcase rather than just an instrumented application set.

## Goals

- demonstrate complex cross-service business calls
- expose every important workflow in OCI APM Trace Explorer and Topology
- land structured logs in OCI Logging, then route them into Log Analytics
- create usable drilldowns between APM, Log Analytics, DB Management, and OPSI
- make the whole flow reproducible from the published docs

## Current cap baseline

As of 2026-05-04, `octodemo.cloud` is restored on the cap profile:

- cap kubectl context: `emdemo`
- cap OCI profile: `cap`
- shop host: `shop.octodemo.cloud`
- CRM host: `crm.octodemo.cloud`
- shared ATP alias: `ocidemoatp_low`

Use `DEFAULT` only for later tests with different domains.

The cap deployment is healthy when these checks return 200:

```bash
./scripts/demo/cap_smoke.sh
```

CRM currently has APM traces and RUM enabled, with unsupported OTLP metric
export disabled. Until the next CRM image is rebuilt, cap carries a temporary
`crm-metrics-hotfix` ConfigMap mount for that change.

## Golden workflows

The platform should always be able to showcase these four workflows:

1. Shop browse -> cart -> checkout -> order persistence -> CRM sync
2. CRM product or storefront update -> shop catalog synchronization
3. Shop AI assistant -> workflow gateway -> Oracle ATP query execution
4. Simulated failure -> correlated trace, log, and SQL evidence

## Rollout sequence

### Phase 1. Instrument business flows

- normalize span names around business actions, not only HTTP routes
- standardize attributes such as `workflow.id`, `workflow.step`, `order.id`,
  `product.sku`, `shop.id`, `sync.status`, and `DbOracleSqlId`
- ensure the shop, CRM, and workflow gateway all propagate `traceparent`

### Phase 2. Make OCI APM the first operator surface

- verify Trace Explorer shows every golden workflow
- verify Topology shows Shop <-> CRM <-> ATP and workflow gateway edges
- create query-based APM dashboard widgets for:
  - checkout latency and failures
  - CRM sync success / failure
  - AI assistant latency and error rate
  - database-bound traces and slow SQL spans

### Phase 3. Complete the logging path

- emit structured JSON logs from both apps
- preserve `trace_id`, `span_id`, `oracleApmTraceId`, `workflow_id`, and
  service metadata
- send logs to OCI Logging first
- route those log groups into Log Analytics
- publish saved searches keyed to the trace id and workflow id

### Phase 4. Publish drilldowns

- APM trace -> Log Analytics search by `oracleApmTraceId`
- APM SQL span -> DB Management Performance Hub via `DbOracleSqlId`
- app dashboards -> OCI console URLs surfaced by `/api/observability/360`
- operator runbooks for checkout, CRM sync, AI assistant, and simulated faults

### Phase 5. Enable DB Management and OPSI

- enroll ATP in Database Management
- enroll ATP in Operations Insights
- verify session tagging with `MODULE`, `ACTION`, and `CLIENT_IDENTIFIER`
- validate SQL Monitor, Performance Hub, and SQL Warehouse visibility for app
  traffic from both services

### Phase 6. Freeze the demo path

- drive the golden workflows with k6 and smoke scripts
- capture the verification checkpoints in the docs
- keep the README, install guide, and published site aligned

## Next steps after cap recovery

1. Promote durable images:
   - rebuild/push CRM with the metrics exporter fix as an immutable
     `linux/amd64` tag
   - rebuild/push shop with the ATP `orders` payment-provider migration as an
     immutable `linux/amd64` tag
   - roll `deployment/enterprise-crm-portal` in `enterprise-crm` using
     container `app`
   - roll `deployment/mushop-portal` in `mushop-portal` using container
     `mushop`
   - remove the live `crm-metrics-hotfix` mount after CRM is rebuilt

2. Normalize cap deployment:
   - decide whether shop stays in `mushop-portal` for cap compatibility or
     moves to `octo-drone-shop`
   - encode that choice in deployment scripts and docs
   - keep `octodemo.cloud` separate from future `DEFAULT` profile test domains

3. Run automated smoke checks:
   - use `./scripts/demo/cap_smoke.sh` before demos and after cap deploys
   - have future CI call the same script with explicit `SHOP_URL`, `CRM_URL`,
     and `KUBE_CONTEXT`

4. Prove the observability story:
   - checkout trace visible in APM
   - CRM order-sync trace visible in APM
   - trace id searchable in logs
   - SQL visible in DB Management or OPSI by service module

5. Document rollback:
   - previous image digests
   - rollout undo command for both deployments
   - ConfigMap hotfix removal command after CRM image promotion

Promotion and rollback reference:

```bash
kubectl --context emdemo set image deploy/enterprise-crm-portal \
  -n enterprise-crm \
  app=${OCIR_REGION}.ocir.io/${OCIR_TENANCY}/enterprise-crm-portal:<tag>
kubectl --context emdemo rollout status deploy/enterprise-crm-portal \
  -n enterprise-crm

kubectl --context emdemo set image deploy/mushop-portal \
  -n mushop-portal \
  mushop=${OCIR_REGION}.ocir.io/${OCIR_TENANCY}/octo-drone-shop:<tag>
kubectl --context emdemo rollout status deploy/mushop-portal \
  -n mushop-portal

kubectl --context emdemo rollout undo deploy/enterprise-crm-portal \
  -n enterprise-crm
kubectl --context emdemo rollout undo deploy/mushop-portal \
  -n mushop-portal
```

Hotfix removal reference after the fixed CRM image is active:

```bash
kubectl --context emdemo patch deploy enterprise-crm-portal \
  -n enterprise-crm \
  --type=strategic \
  -p '{"spec":{"template":{"spec":{"containers":[{"name":"app","volumeMounts":[{"name":"metrics-hotfix","$patch":"delete"}]}],"volumes":[{"name":"metrics-hotfix","$patch":"delete"}]}}}}'

kubectl --context emdemo rollout status deploy/enterprise-crm-portal \
  -n enterprise-crm
kubectl --context emdemo delete configmap crm-metrics-hotfix \
  -n enterprise-crm
```

## Deliverables

| Workstream | Deliverable |
| --- | --- |
| Complex calls | repeatable scripts that produce demonstrable cross-service traces |
| APM | dashboard widgets, trace queries, topology validation steps |
| Logging | OCI Logging log groups + Service Connector + Log Analytics searches |
| Drilldowns | documented pivots from trace -> logs -> SQL evidence |
| Database | DB Management and OPSI enablement plus validation screenshots or checks |
| Docs | updated README, install guide, observability pages, and home page links |

## Acceptance criteria

- one demo run produces traces for checkout, CRM sync, AI assistant, and a
  simulated failure
- those traces are visible in OCI APM with business-level attributes
- the same trace id can be searched in Log Analytics
- at least one SQL span can be traced into DB Management
- OPSI shows workload aggregated for the same database and app modules

## Related pages

- [Add-Ons Guide](addons.md)
- [Traces (APM)](traces.md)
- [Logs](logs.md)
- [Cross-Service Tracing](distributed-traces.md)
- [APM Drill-Down](../observability-v2/apm-drilldown.md)
