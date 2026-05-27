#!/usr/bin/env bash
# Apply repeatable hardening for the live cap-profile octodemo.cloud stack.
#
# This overlay intentionally targets the cap compatibility topology:
#   shop: mushop-portal/mushop-portal
#   CRM:  enterprise-crm/enterprise-crm-portal
#
# It keeps egress unrestricted and only restricts pod ingress, so ATP, APM,
# OCIR, DNS, and other outbound OCI dependencies continue to work.

set -euo pipefail

KUBE_CONTEXT="${KUBE_CONTEXT:-emdemo}"
SHOP_NAMESPACE="${SHOP_NAMESPACE:-mushop-portal}"
SHOP_DEPLOYMENT="${SHOP_DEPLOYMENT:-mushop-portal}"
SHOP_CONTAINER="${SHOP_CONTAINER:-mushop}"
SHOP_SERVICE="${SHOP_SERVICE:-mushop-portal}"
SHOP_IMAGE="${SHOP_IMAGE:-${OCIR_REGION}.ocir.io/${OCIR_TENANCY}/octo-drone-shop@sha256:<SHOP_IMAGE_DIGEST>}"

CRM_NAMESPACE="${CRM_NAMESPACE:-enterprise-crm}"
CRM_DEPLOYMENT="${CRM_DEPLOYMENT:-enterprise-crm-portal}"
CRM_CONTAINER="${CRM_CONTAINER:-app}"
CRM_IMAGE="${CRM_IMAGE:-${OCIR_REGION}.ocir.io/${OCIR_TENANCY}/enterprise-crm-portal@sha256:<CRM_IMAGE_DIGEST>}"

PIN_IMAGES="${PIN_IMAGES:-true}"
APPLY_SHOP_LOGGING_HOTFIX="${APPLY_SHOP_LOGGING_HOTFIX:-true}"

require_tool() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'missing required tool: %s\n' "$1" >&2
    exit 1
  fi
}

kubectl_cap() {
  kubectl --context "${KUBE_CONTEXT}" "$@"
}

require_tool kubectl

kubectl_cap apply -f - <<YAML
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ${SHOP_DEPLOYMENT}
  namespace: ${SHOP_NAMESPACE}
automountServiceAccountToken: false
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ${CRM_DEPLOYMENT}
  namespace: ${CRM_NAMESPACE}
automountServiceAccountToken: false
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: ${SHOP_DEPLOYMENT}
  namespace: ${SHOP_NAMESPACE}
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: ${SHOP_DEPLOYMENT}
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: ${CRM_DEPLOYMENT}
  namespace: ${CRM_NAMESPACE}
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: ${CRM_DEPLOYMENT}
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: ${SHOP_DEPLOYMENT}
  namespace: ${SHOP_NAMESPACE}
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: ${SHOP_DEPLOYMENT}
  minReplicas: 2
  maxReplicas: 4
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: ${CRM_DEPLOYMENT}
  namespace: ${CRM_NAMESPACE}
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: ${CRM_DEPLOYMENT}
  minReplicas: 2
  maxReplicas: 4
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: ${SHOP_DEPLOYMENT}-ingress
  namespace: ${SHOP_NAMESPACE}
spec:
  podSelector:
    matchLabels:
      app: ${SHOP_DEPLOYMENT}
  policyTypes:
    - Ingress
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: ingress-nginx
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: ${CRM_NAMESPACE}
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: ${SHOP_NAMESPACE}
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: observability
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: ops-portal
      ports:
        - protocol: TCP
          port: 8080
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: ${CRM_DEPLOYMENT}-ingress
  namespace: ${CRM_NAMESPACE}
spec:
  podSelector:
    matchLabels:
      app: ${CRM_DEPLOYMENT}
  policyTypes:
    - Ingress
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: ingress-nginx
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: ${SHOP_NAMESPACE}
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: ${CRM_NAMESPACE}
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: observability
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: ops-portal
      ports:
        - protocol: TCP
          port: 8080
YAML

kubectl_cap -n "${SHOP_NAMESPACE}" patch deploy "${SHOP_DEPLOYMENT}" --type strategic -p "{
  \"spec\": {\"template\": {\"spec\": {
    \"serviceAccountName\": \"${SHOP_DEPLOYMENT}\",
    \"automountServiceAccountToken\": false,
    \"securityContext\": {\"runAsNonRoot\": true, \"runAsUser\": 10001, \"runAsGroup\": 10001, \"seccompProfile\": {\"type\": \"RuntimeDefault\"}},
    \"containers\": [{\"name\": \"${SHOP_CONTAINER}\", \"securityContext\": {\"allowPrivilegeEscalation\": false, \"capabilities\": {\"drop\": [\"ALL\"]}}}]
  }}}
}"

kubectl_cap -n "${CRM_NAMESPACE}" patch deploy "${CRM_DEPLOYMENT}" --type strategic -p "{
  \"spec\": {\"template\": {\"spec\": {
    \"serviceAccountName\": \"${CRM_DEPLOYMENT}\",
    \"automountServiceAccountToken\": false,
    \"securityContext\": {\"runAsNonRoot\": true, \"runAsUser\": 1000, \"runAsGroup\": 1000, \"seccompProfile\": {\"type\": \"RuntimeDefault\"}},
    \"containers\": [{\"name\": \"${CRM_CONTAINER}\", \"securityContext\": {\"allowPrivilegeEscalation\": false, \"capabilities\": {\"drop\": [\"ALL\"]}}}]
  }}}
}"

if [[ "${PIN_IMAGES}" == "true" ]]; then
  kubectl_cap -n "${SHOP_NAMESPACE}" set image "deployment/${SHOP_DEPLOYMENT}" "${SHOP_CONTAINER}=${SHOP_IMAGE}"
  kubectl_cap -n "${CRM_NAMESPACE}" set image "deployment/${CRM_DEPLOYMENT}" "${CRM_CONTAINER}=${CRM_IMAGE}"
fi

kubectl_cap -n "${SHOP_NAMESPACE}" annotate "service/${SHOP_SERVICE}" \
  kubectl.kubernetes.io/last-applied-configuration- \
  oci.oraclecloud.com/load-balancer-type- \
  oci.oraclecloud.com/oci-load-balancer-health-check-interval- \
  oci.oraclecloud.com/oci-load-balancer-health-check-retries- \
  oci.oraclecloud.com/oci-load-balancer-health-check-timeout- \
  oci.oraclecloud.com/oke-migration-bridge- \
  service.beta.kubernetes.io/oci-load-balancer-shape- \
  service.beta.kubernetes.io/oci-load-balancer-shape-flex-max- \
  service.beta.kubernetes.io/oci-load-balancer-shape-flex-min- \
  service.beta.kubernetes.io/oci-load-balancer-subnet1- \
  --overwrite >/dev/null 2>&1 || true

kubectl_cap -n "${SHOP_NAMESPACE}" patch "service/${SHOP_SERVICE}" \
  --type=json -p '[{"op":"remove","path":"/metadata/finalizers"}]' >/dev/null 2>&1 || true

if [[ "${APPLY_SHOP_LOGGING_HOTFIX}" == "true" ]]; then
  kubectl_cap -n "${SHOP_NAMESPACE}" create configmap shop-logging-sdk-hotfix \
    --from-file=logging_sdk.py=server/observability/logging_sdk.py \
    --dry-run=client -o yaml | kubectl_cap -n "${SHOP_NAMESPACE}" apply -f -

  if ! kubectl_cap -n "${SHOP_NAMESPACE}" get deploy "${SHOP_DEPLOYMENT}" \
    -o jsonpath='{.spec.template.spec.volumes[*].name}' | tr ' ' '\n' | grep -qx logging-sdk-hotfix; then
    kubectl_cap -n "${SHOP_NAMESPACE}" patch deploy "${SHOP_DEPLOYMENT}" --type=json -p '[
      {"op":"add","path":"/spec/template/spec/volumes/-","value":{"name":"logging-sdk-hotfix","configMap":{"name":"shop-logging-sdk-hotfix"}}},
      {"op":"add","path":"/spec/template/spec/containers/0/volumeMounts/-","value":{"name":"logging-sdk-hotfix","mountPath":"/app/server/observability/logging_sdk.py","subPath":"logging_sdk.py","readOnly":true}}
    ]'
  fi
fi

kubectl_cap -n "${SHOP_NAMESPACE}" rollout status "deployment/${SHOP_DEPLOYMENT}" --timeout=180s
kubectl_cap -n "${CRM_NAMESPACE}" rollout status "deployment/${CRM_DEPLOYMENT}" --timeout=180s

printf 'cap hardening applied successfully\n'
