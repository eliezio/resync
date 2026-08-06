#!/usr/bin/env bash
# Flatten catalog.json (SchemaCrawler serialize output) into config.skeleton.json,
# then verify the FK graph is acyclic and print the parent-first load order.
#
# Requires: jq, and tsort (coreutils, standard on macOS/Linux).
#
# Usage:
#   ./build-config.sh [catalog.json] [config.skeleton.json]
set -euo pipefail

CATALOG="${1:-catalog.json}"
OUT="${2:-config.skeleton.json}"
HERE="$(cd "$(dirname "$0")" && pwd)"

[ -f "$CATALOG" ] || { echo "missing $CATALOG (run extract-schema.sh first)" >&2; exit 1; }

# 1. Flatten catalog -> config skeleton.
jq -f "$HERE/extract.jq" "$CATALOG" > "$OUT"
echo "Wrote $OUT"

# 2. Fail if any FK reference was left unresolved (uuid not mapped to a column).
unresolved=$(jq '[.edges[] | select(.parent==null or .child==null)] | length' "$OUT")
if [ "$unresolved" -ne 0 ]; then
  echo "ERROR: $unresolved unresolved FK edge(s) — column uuid map incomplete" >&2
  exit 1
fi

edges=$(jq '.edges | length' "$OUT")
tables=$(jq '.tables | length' "$OUT")
echo "tables=$tables edges=$edges unresolved=0"

# 3. Self-references (single-table cycles).
echo "== self-references =="
jq -r '.edges[] | select(.child==.parent) | "  \(.child)  [\(.fk)]"' "$OUT" || true

# 4. Cycle detection + topological (parent-first) load order via tsort.
echo "== load order (parents first) =="
if ! jq -r '.edges[] | select(.child!=.parent) | "\(.parent) \(.child)"' "$OUT" \
     | sort -u | tsort 2>/tmp/tsort.err | nl; then
  echo "CYCLE DETECTED:" >&2
  cat /tmp/tsort.err >&2
  exit 1
fi
