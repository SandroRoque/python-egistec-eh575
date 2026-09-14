#!/bin/bash
set -Eeuo pipefail

VERSION=5.0.0
URL=https://nigos.nist.gov/nist/nbis/nbis_v5_0_0.zip
SHA256=0adf8ab0f6b0e4208de50ca00ba21d3d77112ecd66288757ddfed21f6bee92c3
PREFIX=${EGIS_NBIS_PREFIX:-/opt/nbis-5.0.0}

if [ "$EUID" -ne 0 ]; then
  printf 'Run once as root: sudo ./install-nbis.sh\n' >&2
  exit 1
fi

for command in curl unzip make gcc sha256sum; do
  command -v "$command" >/dev/null || {
    printf 'Missing build command: %s\n' "$command" >&2
    exit 1
  }
done

work=$(mktemp -d /tmp/egis-nbis-build.XXXXXX)
trap 'rm -rf -- "$work"' EXIT
archive="$work/nbis.zip"
curl --fail --location --proto '=https' --tlsv1.2 "$URL" -o "$archive"
printf '%s  %s\n' "$SHA256" "$archive" | sha256sum --check --status
unzip -q "$archive" -d "$work/source"
source_root="$work/source/Rel_5.0.0"
build_prefix="$work/install"
mkdir -p "$build_prefix"

# NBIS predates GCC 10's -fno-common default.
sed -i 's/-O2 -w -ansi/-O2 -fcommon -w -ansi/' "$source_root/rules.mak.src"
grep -q -- '-fcommon' "$source_root/rules.mak.src"
cd "$source_root"
./setup.sh "$build_prefix" --without-X11 --STDLIBS --64
make config
make -j"$(nproc)" it
make install

stage=$(mktemp -d /opt/.nbis-5.0.0.XXXXXX)
install -d -m 0755 "$stage/bin"
for command in cwsq mindtct bozorth3; do
  install -m 0755 "$build_prefix/bin/$command" "$stage/bin/$command"
done
printf '%s\n' "$VERSION" > "$stage/VERSION"
chmod 0644 "$stage/VERSION"
if [ -e "$PREFIX" ]; then
  mv "$PREFIX" "${PREFIX}.backup.$(date -u +%Y%m%dT%H%M%SZ)"
fi
mv "$stage" "$PREFIX"
printf 'Installed NBIS %s to %s\n' "$VERSION" "$PREFIX"
printf 'Use: export EGIS_NBIS_BIN=%s/bin\n' "$PREFIX"
