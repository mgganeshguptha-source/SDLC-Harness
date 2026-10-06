#!/usr/bin/env bash
# Write a settings.xml so Maven resolves from a git library store
# (maven_libs_repo), laid out like a company artifact repository. Each
# top-level folder of the store is one repository:
#   <name>/         internal libraries of one release (e.g. petclinic-libs-PM)
#   maven-remote/   OPTIONAL copies of Maven Central libraries
# tools/ and sources/ are skipped. Internal folders are always added as
# repositories. Public libraries then come from:
#   - maven-remote/ when it exists: a catch-all mirror sends EVERY other
#     repository (Maven Central, and any repository a downloaded POM declares)
#     there - a fully offline build; a library missing from the store fails.
#   - Maven Central when it does not: the store holds only internal libraries.
#
# Usage: maven-store-settings.sh <store-dir> <settings-out>
set -euo pipefail
STORE="$(cd "${1:?store dir}" && pwd)"
OUT="${2:?settings output path}"

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

mirrors=""
if [ -d "$STORE/maven-remote" ]; then
  public="maven-remote (offline; Maven Central not used)"
  mirrors="
  <mirrors>
    <mirror>
      <id>maven-remote</id>
      <mirrorOf>*${excl}</mirrorOf>
      <url>file://$STORE/maven-remote</url>
    </mirror>
  </mirrors>"
else
  public="Maven Central"
fi

mkdir -p "$(dirname "$OUT")"
cat > "$OUT" <<XML
<settings>${mirrors}
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
echo "Library store: internal =${names:- (none)} | public libraries from $public"
