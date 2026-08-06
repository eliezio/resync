#!/usr/bin/env bash
# Extract the Oracle schema to catalog.json using SchemaCrawler.
#
# Produces the minimal subset needed for the re-sync: tables, columns (name/type/nullable),
# primary keys, unique constraints, foreign keys — as a machine-readable serialized catalog.
#
# Requires: SchemaCrawler on PATH (the `schemacrawler` launcher) and the Oracle JDBC driver
# bundled with its Oracle plugin.
#
# Usage:
#   DB_HOST=... DB_PORT=1521 DB_SERVICE=ORCL DB_SCHEMA=MYSCHEMA \
#   DB_USER=... DB_PASSWORD=... ./extract-schema.sh
set -euo pipefail

: "${DB_HOST:?set DB_HOST}"
: "${DB_PORT:=1521}"
: "${DB_SERVICE:?set DB_SERVICE (service name or SID)}"
: "${DB_SCHEMA:?set DB_SCHEMA (schema to crawl)}"
: "${DB_USER:?set DB_USER}"
: "${DB_PASSWORD:?set DB_PASSWORD}"

OUT="${OUT:-catalog.json}"

schemacrawler \
  --server=oracle \
  --host="$DB_HOST" \
  --port="$DB_PORT" \
  --database="$DB_SERVICE" \
  --user="$DB_USER" \
  --password="$DB_PASSWORD" \
  --schemas="$DB_SCHEMA" \
  --info-level=standard \
  --routines= --sequences= --synonyms= \
  --command=serialize \
  --output-format=json \
  --output-file="$OUT" \
  --portable-names

echo "Wrote $OUT"

# Optional: independent cycle check via SchemaCrawler's lint command.
# Grep for "cycles in table relationships" — no such line means acyclic.
#   schemacrawler ... --info-level=standard --command=lint
