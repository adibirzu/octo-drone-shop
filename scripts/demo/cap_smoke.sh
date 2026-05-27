#!/usr/bin/env bash
# Smoke checks for the live cap-profile octodemo.cloud deployment.
#
# Defaults intentionally target the cap runtime:
#   - kubectl context: emdemo
#   - shop: https://shop.octodemo.cloud
#   - CRM:  https://crm.octodemo.cloud
#
# Override SHOP_URL, CRM_URL, KUBE_CONTEXT, SHOP_NAMESPACE, SHOP_DEPLOYMENT,
# CRM_NAMESPACE, or CRM_DEPLOYMENT to reuse the checks elsewhere.

set -euo pipefail

SHOP_URL="${SHOP_URL:-https://shop.octodemo.cloud}"
CRM_URL="${CRM_URL:-https://crm.octodemo.cloud}"
KUBE_CONTEXT="${KUBE_CONTEXT:-emdemo}"
SHOP_NAMESPACE="${SHOP_NAMESPACE:-mushop-portal}"
SHOP_DEPLOYMENT="${SHOP_DEPLOYMENT:-mushop-portal}"
CRM_NAMESPACE="${CRM_NAMESPACE:-enterprise-crm}"
CRM_DEPLOYMENT="${CRM_DEPLOYMENT:-enterprise-crm-portal}"
CURL_MAX_TIME="${CURL_MAX_TIME:-15}"

step() { printf '\n== %s ==\n' "$*"; }

require_tool() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'missing required tool: %s\n' "$1" >&2
    exit 1
  fi
}

check_url() {
  local label="$1"
  local url="$2"

  printf '[HTTP] %-34s %s\n' "$label" "$url"
  curl --noproxy '*' -fsS --max-time "$CURL_MAX_TIME" "$url" >/dev/null
}

require_tool curl
require_tool kubectl

step "Public pages"
check_url "shop root" "${SHOP_URL}/"
check_url "crm login" "${CRM_URL}/login"

step "Readiness and integration"
check_url "shop ready" "${SHOP_URL}/ready"
check_url "crm ready" "${CRM_URL}/ready"
check_url "shop dashboard summary" "${SHOP_URL}/api/dashboard/summary"
check_url "shop to CRM health" "${SHOP_URL}/api/integrations/crm/health"

step "Kubernetes rollouts"
kubectl --context "$KUBE_CONTEXT" rollout status \
  "deploy/${SHOP_DEPLOYMENT}" -n "$SHOP_NAMESPACE" --timeout=90s
kubectl --context "$KUBE_CONTEXT" rollout status \
  "deploy/${CRM_DEPLOYMENT}" -n "$CRM_NAMESPACE" --timeout=90s

step "Ingress mapping"
kubectl --context "$KUBE_CONTEXT" get ingress -A | grep -E 'octodemo-(shop|crm)'

printf '\ncap smoke checks passed\n'
