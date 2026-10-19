#!/usr/bin/env bash
# Regenerates ALL contract bindings from packages/contracts/schemas/.
#
# `packages/contracts/schemas/*.json` is the single source of truth (§1.1). Nothing under a
# `generated/` directory is hand-written; this script is the only thing that produces them.
#
# Invoked by `pnpm gen:contracts` (root package.json) and by the CI drift check, which
# regenerates and then fails if `git diff` is non-empty.
#
# Usage:  packages/contracts/generate.sh [--check]
#           --check   regenerate into a temp dir and diff against the committed output
set -euo pipefail

CONTRACTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$CONTRACTS_DIR/../.." && pwd)"
SCHEMAS="$CONTRACTS_DIR/schemas"
PY_OUT="$CONTRACTS_DIR/python/src/referraleasy_contracts/generated"
TS_OUT="$CONTRACTS_DIR/ts/src/generated"

# Shared flags. --use-title-as-name is load-bearing: without it the generator names inline
# enums after the field (`booking.Status1`, `task.Class`) instead of the schema `title`,
# which makes the bindings hard to use correctly.
CODEGEN_FLAGS=(
  --input-file-type jsonschema
  --output-model-type pydantic_v2.BaseModel
  --target-python-version 3.12
  --use-standard-collections
  --use-union-operator
  --use-schema-description
  --field-constraints
  --use-title-as-name
  --disable-timestamp
)

generate_python() {
  local out="$1"
  rm -rf "$out"
  mkdir -p "$(dirname "$out")"
  cd "$REPO_ROOT"
  uv run --package referraleasy-contracts datamodel-codegen \
    --input "$SCHEMAS" \
    --output "$out" \
    "${CODEGEN_FLAGS[@]}" 2>&1 | grep -viE 'futurewarning|warn_deprecated' || true
}

generate_ts() {
  local out="$1"
  cd "$REPO_ROOT"
  CONTRACTS_TS_OUT="$out" pnpm --filter @referraleasy/contracts gen
}

# Generated source only — __pycache__ is bytecode from test runs, not output.
diff_opts=(-r -x __pycache__ -x '*.pyc')

check_drift() {
  local label="$1" committed="$2" fresh="$3"
  if ! diff "${diff_opts[@]}" "$committed" "$fresh" >/dev/null 2>&1; then
    echo "FAIL: committed $label bindings differ from a fresh generation." >&2
    echo "      Run 'pnpm gen:contracts' and commit the result." >&2
    diff "${diff_opts[@]}" "$committed" "$fresh" | head -40 >&2 || true
    return 1
  fi
  echo "OK: $label bindings are up to date."
}

if [[ "${1:-}" == "--check" ]]; then
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  echo "Regenerating into $TMP to check for drift…"
  generate_python "$TMP/python"
  generate_ts "$TMP/ts" >/dev/null
  check_drift "Python" "$PY_OUT" "$TMP/python" || exit 1
  check_drift "TypeScript" "$TS_OUT" "$TMP/ts" || exit 1
  exit 0
fi

echo "Generating Python bindings → $PY_OUT"
generate_python "$PY_OUT"
echo "Generating TypeScript bindings → $TS_OUT"
generate_ts "$TS_OUT"
echo "Done. Commit the regenerated output alongside any schema change."
