#!/bin/bash
set -Eeuo pipefail

VERSION=3.18.1
SOURCEAFIS_SHA256=850582c842a7c7bf4ab87d8e8785674d9e092eb379a021d46bc053d2fc6028c0
MAVEN_VERSION=3.9.11
MAVEN_URL=https://archive.apache.org/dist/maven/maven-3/$MAVEN_VERSION/binaries/apache-maven-$MAVEN_VERSION-bin.tar.gz
MAVEN_SHA512=bcfe4fe305c962ace56ac7b5fc7a08b87d5abd8b7e89027ab251069faebee516b0ded8961445d6d91ec1985dfe30f8153268843c89aa392733d1a3ec956c9978
PREFIX=${EGIS_SOURCEAFIS_PREFIX:-/opt/sourceafis-$VERSION}
SCRIPT_ROOT=$(cd "$(dirname "$0")" && pwd)

if [ "$EUID" -ne 0 ] && [[ "$PREFIX" == /opt/* ]]; then
  printf 'Run once as root: sudo ./install-sourceafis.sh\n' >&2
  exit 1
fi
for command in curl tar sha256sum sha512sum java javac; do
  command -v "$command" >/dev/null || { printf 'Missing build command: %s\n' "$command" >&2; exit 1; }
done

work=$(mktemp -d /tmp/egis-sourceafis-build.XXXXXX)
trap 'rm -rf -- "$work"' EXIT
curl --fail --location --proto '=https' --tlsv1.2 "$MAVEN_URL" -o "$work/maven.tar.gz"
printf '%s  %s\n' "$MAVEN_SHA512" "$work/maven.tar.gz" | sha512sum --check --status
tar -xzf "$work/maven.tar.gz" -C "$work"
mkdir -p "$work/project/src/main/java" "$work/project/lib"
install -m 0644 "$SCRIPT_ROOT/sourceafis/EgisSourceAfisWorker.java" "$work/project/src/main/java/"
install -m 0644 "$SCRIPT_ROOT/sourceafis/pom.xml" "$work/project/pom.xml"
"$work/apache-maven-$MAVEN_VERSION/bin/mvn" -q -f "$work/project/pom.xml" package dependency:copy-dependencies -DoutputDirectory="$work/project/lib"
printf '%s  %s\n' "$SOURCEAFIS_SHA256" "$work/project/lib/sourceafis-$VERSION.jar" | sha256sum --check --status

install_parent=$(dirname "$PREFIX")
mkdir -p "$install_parent"
stage=$(mktemp -d "$install_parent/.sourceafis-$VERSION.XXXXXX")
install -d -m 0755 "$stage/bin" "$stage/lib"
install -m 0644 "$work/project/target/classes/EgisSourceAfisWorker.class" "$stage/lib/"
install -m 0644 "$work/project/lib/"*.jar "$stage/lib/"
cat > "$stage/bin/egis-sourceafis-worker" <<'EOF'
#!/bin/sh
exec java -cp "$(dirname "$0")/../lib:$(dirname "$0")/../lib/*" EgisSourceAfisWorker
EOF
chmod 0755 "$stage/bin/egis-sourceafis-worker"
printf '%s\n' "$VERSION" > "$stage/VERSION"
chmod 0644 "$stage/VERSION"
if [ -e "$PREFIX" ]; then
  mv "$PREFIX" "${PREFIX}.backup.$(date -u +%Y%m%dT%H%M%SZ)"
fi
mv "$stage" "$PREFIX"
printf 'Installed SourceAFIS %s to %s\n' "$VERSION" "$PREFIX"
printf 'Use: export EGIS_SOURCEAFIS_HOME=%s\n' "$PREFIX"
