#!/usr/bin/env bash
# Report where the build's libraries came from. Maven writes a
# _remote.repositories file into every library folder it downloads, naming
# the repository the file came from ("x.jar>central=", "y.pom>petclinic-libs-PM=").
# Summarising them proves which libraries came from Maven Central and which
# from the library store's internal folders.
#
# Usage: maven-library-sources.sh [local-repo]   (default ~/.m2/repository)
set -euo pipefail
REPO="${1:-$HOME/.m2/repository}"
[ -d "$REPO" ] || { echo "No local Maven repository at $REPO (build did not run?)"; exit 0; }

TMP=$(mktemp)
find "$REPO" -name _remote.repositories | while read -r f; do
  dir=$(dirname "$f")
  grep -h '>' "$f" | sed -E 's/^[^>]*>([^=]*)=.*$/\1/' | sort -u \
    | while read -r src; do echo "${src:-local}|${dir#"$REPO"/}"; done
done > "$TMP"

echo "Libraries by source (one line per library version):"
cut -d'|' -f1 "$TMP" | sort | uniq -c | sort -rn | sed 's/^/  /'
echo
echo "Libraries NOT from Maven Central:"
grep -v '^central|' "$TMP" | sort | sed -E 's/^([^|]*)\|(.*)$/  [\1] \2/' || echo "  (none)"
rm -f "$TMP"
