#!/usr/bin/env bash
# Render the prod overlay and write the normalized result to rendered/prod.yaml.
#
# Usage: tools/ci/render-prod.sh [--check]
#   (no flag)  build + normalize, overwrite rendered/prod.yaml
#   --check    build + normalize, diff against the committed rendered/prod.yaml; exit 1 on drift
#
# The committed rendered/prod.yaml is the artifact the CAB reviews. This script is the only
# thing that writes it, and the PR check runs it with --check so the file can never drift from
# environments/prod.
set -euo pipefail
cd "$(dirname "$0")/../.."

ROOT=environments/prod
OUT=rendered/prod.yaml

if command -v kustomize >/dev/null 2>&1; then
  BUILD=(kustomize build "$ROOT")
else
  BUILD=(kubectl kustomize "$ROOT")
fi

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
"${BUILD[@]}" | python3 tools/ci/normalize-render.py > "$tmp"
[ -s "$tmp" ] || { echo "render-prod: empty render, refusing" >&2; exit 2; }

if [ "${1:-}" = "--check" ]; then
  if diff -u "$OUT" "$tmp"; then
    echo "render-prod: rendered/prod.yaml matches $ROOT"
  else
    echo "render-prod: rendered/prod.yaml is STALE. Run tools/ci/render-prod.sh and commit." >&2
    exit 1
  fi
else
  mkdir -p rendered
  cp "$tmp" "$OUT"
  echo "render-prod: wrote $OUT ($(grep -c '^# =====' "$OUT") objects)"
fi
