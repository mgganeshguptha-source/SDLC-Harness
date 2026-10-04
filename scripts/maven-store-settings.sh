#!/usr/bin/env bash
# Write a settings.xml so Maven resolves ONLY from a git library store
# (maven_libs_repo), the way a company build resolves only from its artifact
# repository. Each top-level folder of the store is one repository:
#   maven-remote/   copies of Maven Central libraries
#   <other>/        internal libraries of one release (e.g. petclinic-libs-PM)
# tools/ and sources/ are skipped. Internal folders are added as repositories;
# a catch-all mirror sends EVERY other repository - Maven Central, and any
# repository a downloaded POM declares - to maven-remote/. A library missing
# from the store therefore fails the build instead of being downloaded.
#
# Usage: maven-store-settings.sh <store-dir> <settings-out>
set -euo pipefail
STORE="$(cd "${1:?store dir}" && pwd)"
OUT="${2:?settings output path}"
[ -d "$STORE/maven-remote" ] || { echo "$STORE has no maven-remote/ folder" >&2; exit 1; }

repos=""; excl=""; names=""
for d in "$STORE"/*/; do
  n=$(basename "$d")
  case "$n" in tools|sources|maven-remote) continue ;; esac
  excl="$excl,!$n"; names="$names $n"
  repos="$repos
        <repository><id>$n</id><url>file://$STORE/$n</url>
          <releases><enabled>true</enabled></releases>
          <snapshots><enabled>true</enabled></snapshots>
        </repository>"
done

mkdir -p "$(dirname "$OUT")"
cat > "$OUT" <<XML
<settings>
  <mirrors>
    <mirror>
      <id>maven-remote</id>
      <mirrorOf>*${excl}</mirrorOf>
      <url>file://$STORE/maven-remote</url>
    </mirror>
  </mirrors>
  <profiles>
    <profile>
      <id>library-store</id>
      <repositories>$repos
      </repositories>
      <pluginRepositories>${repos//repository>/pluginRepository>}
      </pluginRepositories>
    </profile>
  </profiles>
  <activeProfiles><activeProfile>library-store</activeProfile></activeProfiles>
</settings>
XML
echo "Library store: maven-remote (all public repositories) +${names:- no internal folders}"
