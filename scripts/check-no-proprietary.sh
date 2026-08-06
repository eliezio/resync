#!/usr/bin/env bash
# Leak guard: fail if any proprietary identifier appears in a file that would be committed.
# Run before `git init`/commit and wire into CI and a pre-commit hook.
#
# The proprietary stems live in a gitignored denylist file (default: ./.denylist), NOT in this
# script — otherwise the script itself would leak the vocabulary. Copy scripts/denylist.example
# to ./.denylist and fill it with your real schema's naming stems. A public clone has no
# ./.denylist, so this guard is a no-op there (nothing proprietary to catch).
#
# Files matched by .gitignore (catalog.json, config.skeleton.json, resync.yaml, INSTANCE.md,
# .denylist, .venv, ...) are the private instance and are intentionally skipped.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"

DENYLIST="${DENYLIST:-.denylist}"
if [ ! -f "$DENYLIST" ]; then
  echo "no $DENYLIST present; nothing to check (public repo carries no proprietary stems)."
  exit 0
fi

# Build one case-insensitive alternation from the denylist (skip blank lines and # comments).
DENY="$(grep -vE '^[[:space:]]*(#|$)' "$DENYLIST" | paste -sd'|' -)"
[ -n "$DENY" ] || { echo "$DENYLIST is empty; nothing to check."; exit 0; }

# Files to check = committable = everything except gitignored paths and the denylist itself.
if git rev-parse --git-dir >/dev/null 2>&1; then
  mapfile -t FILES < <(git ls-files)
else
  mapfile -t FILES < <(find . -type f \
    -not -path './.venv/*' -not -path '*/__pycache__/*' -not -path './.git/*' \
    -not -name '*.pyc' -not -name '*.dmp' -not -name '*.sql' \
    -not -path './.denylist' \
    -not -path './catalog.json' -not -path './config.skeleton.json' \
    -not -path './resync.yaml' -not -path './INSTANCE.md')
fi

hits=0
for f in "${FILES[@]}"; do
  [ -f "$f" ] || continue
  if grep -IniE "$DENY" "$f" >/dev/null 2>&1; then
    echo "LEAK: proprietary identifier in $f"
    grep -IniE "$DENY" "$f" | head -5
    hits=1
  fi
done

if [ "$hits" -ne 0 ]; then
  echo
  echo "Refusing: proprietary identifiers found in committable files. Move them to INSTANCE.md"
  echo "(gitignored) or genericize with the Order/OrderLine sample."
  exit 1
fi
echo "OK: no proprietary identifiers in committable files."
