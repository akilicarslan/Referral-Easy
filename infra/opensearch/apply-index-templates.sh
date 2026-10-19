#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════════
# ReferralEasy — apply ISM policies + index templates to OpenSearch
#
# Idempotent: safe to run on every `make demo` / `make index`. Re-running
# creates a new ISM policy version (harmless) and overwrites the templates
# with the versions in this directory, which are the source of truth.
#
# Usage:
#   OPENSEARCH_URL=http://localhost:9200 infra/opensearch/apply-index-templates.sh
#
# Exits non-zero if any policy or template fails to apply, so a demo driver
# can rely on it as a real step rather than best-effort decoration.
# ════════════════════════════════════════════════════════════════════════════
set -euo pipefail

OPENSEARCH_URL="${OPENSEARCH_URL:-http://localhost:9200}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_DIR="${HERE}/index-templates"
POLICY_DIR="${HERE}/ism-policies"

echo "OpenSearch: ${OPENSEARCH_URL}"

# ── wait for the cluster to answer ──────────────────────────────────────────
for attempt in $(seq 1 60); do
  if curl -fsS "${OPENSEARCH_URL}/_cluster/health?wait_for_status=yellow&timeout=5s" -o /dev/null 2>/dev/null; then
    break
  fi
  if [ "${attempt}" -eq 60 ]; then
    echo "FAIL: OpenSearch did not become reachable at ${OPENSEARCH_URL}" >&2
    exit 1
  fi
  sleep 2
done
echo "cluster reachable"

put() {
  local url="$1" file="$2" label="$3" code
  code="$(curl -sS -o /tmp/apply-templates.out -w '%{http_code}' \
    -X PUT "${url}" \
    -H 'Content-Type: application/json' \
    --data-binary "@${file}")"
  case "${code}" in
    200|201)
      echo "  ok    ${label}  (HTTP ${code})"
      ;;
    *)
      echo "  FAIL  ${label}  (HTTP ${code})" >&2
      cat /tmp/apply-templates.out >&2
      exit 1
      ;;
  esac
}

# ── 1. ISM policies first: the templates reference them by id ───────────────
# MEASURED: a plain PUT of an existing policy returns HTTP 409
# (version_conflict_engine_exception). ISM requires the current `_seq_no` and
# `_primary_term` to update a policy in place, so the create path is tried
# first and the conflict path re-sends with those values — which is what makes
# re-running this script on every `make demo` safe.
put_policy() {
  local id="$1" file="$2" code seq prim
  code="$(curl -sS -o /tmp/apply-templates.out -w '%{http_code}' \
    -X PUT "${OPENSEARCH_URL}/_plugins/_ism/policies/${id}" \
    -H 'Content-Type: application/json' --data-binary "@${file}")"
  if [ "${code}" = "409" ]; then
    read -r seq prim < <(curl -sS "${OPENSEARCH_URL}/_plugins/_ism/policies/${id}" \
      | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["_seq_no"], d["_primary_term"])')
    if [ -z "${seq}" ] || [ -z "${prim}" ]; then
      echo "  FAIL  ${id}  (could not read _seq_no/_primary_term for the update)" >&2
      exit 1
    fi
    code="$(curl -sS -o /tmp/apply-templates.out -w '%{http_code}' \
      -X PUT "${OPENSEARCH_URL}/_plugins/_ism/policies/${id}?if_seq_no=${seq}&if_primary_term=${prim}" \
      -H 'Content-Type: application/json' --data-binary "@${file}")"
  fi
  case "${code}" in
    200|201) echo "  ok    ${id}  (HTTP ${code})" ;;
    *) echo "  FAIL  ${id}  (HTTP ${code})" >&2; cat /tmp/apply-templates.out >&2; exit 1 ;;
  esac
}

echo "ISM policies (retention):"
for f in "${POLICY_DIR}"/*.json; do
  [ -e "${f}" ] || continue
  put_policy "$(basename "${f}" .json)" "${f}"
done

# ── 1b. the ISM plugin's own config index ───────────────────────────────────
# MEASURED: .opendistro-ism-config is created with 1 replica. On a single-node
# cluster that replica can never be assigned, so the cluster sits at yellow
# forever and every health check has to be read as "yellow is fine". Retention
# bookkeeping is not worth a permanently yellow demo.
curl -sS -o /dev/null -w '  ok    ism-config replicas=0  (HTTP %{http_code})\n' \
  -X PUT "${OPENSEARCH_URL}/.opendistro-ism-config/_settings" \
  -H 'Content-Type: application/json' \
  -d '{"index":{"number_of_replicas":0}}' || true

# ── 2. index templates ──────────────────────────────────────────────────────
echo "Index templates:"
for f in "${TEMPLATE_DIR}"/*.json; do
  [ -e "${f}" ] || continue
  put "${OPENSEARCH_URL}/_index_template/$(basename "${f}" .json)" "${f}" "$(basename "${f}" .json)"
done

echo "done"
