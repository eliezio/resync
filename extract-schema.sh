#!/usr/bin/env bash
# Extract the Oracle schema to:
#   * catalog.json  — serialized catalog for the re-sync pipeline (extract.jq / build-config.sh)
#   * schema.png    — a slimmed foreign-key diagram: relationship columns only, no clutter
#
# Requires SchemaCrawler (with the Oracle plugin + JDBC driver); the PNG also needs GraphViz.
# Both outputs are derived from the real schema and are gitignored (schema.png is proprietary).
#
# Usage:
#   DB_HOST=... DB_PORT=1521 DB_SERVICE=ORCL DB_SCHEMA=MYSCHEMA \
#   DB_USER=... DB_PASSWORD=... ./extract-schema.sh
#
# Env knobs: OUT (default catalog.json); GRAPH (default schema.png; set GRAPH= to skip the diagram).
set -euo pipefail

: "${DB_HOST:?set DB_HOST}"
: "${DB_PORT:=1521}"
: "${DB_SERVICE:?set DB_SERVICE (service name or SID)}"
: "${DB_SCHEMA:?set DB_SCHEMA (schema to crawl)}"
: "${DB_USER:?set DB_USER}"
: "${DB_PASSWORD:?set DB_PASSWORD}"

OUT="${OUT:-catalog.json}"
GRAPH="${GRAPH-schema.png}"

CONN=(
  --server=oracle
  --host="$DB_HOST"
  --port="$DB_PORT"
  --database="$DB_SERVICE"
  --user="$DB_USER"
  --password="$DB_PASSWORD"
  --schemas="$DB_SCHEMA"
  --routines= --sequences= --synonyms=
)

# 1. Serialized catalog for the pipeline.
schemacrawler "${CONN[@]}" \
  --info-level=standard \
  --command=serialize \
  --output-format=json \
  --output-file="$OUT" \
  --portable-names
echo "Wrote $OUT"

# 2. Slimmed foreign-key diagram.
#    --command=brief keeps only significant columns (PK / FK / unique-index); the config file
#    strips constraint names and cardinality so boxes show just the key columns and the edges.
if [ -n "$GRAPH" ]; then
  cfg="$(mktemp)"; trap 'rm -f "$cfg"' EXIT
  cat > "$cfg" <<'PROPS'
schemacrawler.format.hide_foreignkey_names=true
schemacrawler.format.hide_primarykey_names=true
schemacrawler.format.hide_index_names=true
schemacrawler.format.hide_constraint_names=true
schemacrawler.format.show_unqualified_names=true
schemacrawler.format.show_ordinal_numbers=false
schemacrawler.graph.show_foreignkey_cardinality=false
schemacrawler.graph.show_primarykey_cardinality=false
PROPS
  schemacrawler "${CONN[@]}" \
    --info-level=standard \
    --command=brief \
    --output-format=png \
    --output-file="$GRAPH" \
    --config-file="$cfg" \
    --portable-names --no-info --no-remarks
  echo "Wrote $GRAPH"
fi

# Optional cycle check (grep the output for "cycles in table relationships"):
#   schemacrawler "${CONN[@]}" --info-level=standard --command=lint
