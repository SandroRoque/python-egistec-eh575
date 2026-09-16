#!/bin/bash
set -Eeuo pipefail

INSTALL_DIR="/opt/egis-driver"
DATA_ROOT="/var/lib/open-fprintd"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR/open-fprintd-eh575"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="${INSTALL_DIR}.backup.${STAMP}"
STAGE_DIR=""
CONFIG_BACKUP=""
INSTALLED=0
COMMITTED=0
OPEN_FPRINTD_ACTIVE=""
OPEN_FPRINTD_ENABLED=""
EGIS_BRIDGE_ACTIVE=""
EGIS_BRIDGE_ENABLED=""

PAYLOAD_TEXT="$(PYTHONPATH="$PROJECT_DIR" python3 -c \
  'from egis_driver.runtime_payload import EXECUTABLES; print(*EXECUTABLES, sep="\n")')"
PACKAGES_TEXT="$(PYTHONPATH="$PROJECT_DIR" python3 -c \
  'from egis_driver.runtime_payload import PACKAGES; print(*PACKAGES, sep="\n")')"
mapfile -t PAYLOAD <<< "$PAYLOAD_TEXT"
mapfile -t PACKAGES <<< "$PACKAGES_TEXT"
CONFIG_SOURCES=(
  "70-egis-eh575.rules"
  "io.github.uunicorn.Fprint.Device.Egis.conf"
  "net.reactivated.fprint.policy"
  "open-fprintd.service"
  "egis-bridge.service"
)
CONFIG_TARGETS=(
  "/etc/udev/rules.d/70-egis-eh575.rules"
  "/usr/share/dbus-1/system.d/io.github.uunicorn.Fprint.Device.Egis.conf"
  "/usr/share/polkit-1/actions/net.reactivated.fprint.policy"
  "/etc/systemd/system/open-fprintd.service"
  "/etc/systemd/system/egis-bridge.service"
)

check_python_module() {
  python3 - "$1" <<'PY'
import importlib
import sys
importlib.import_module(sys.argv[1])
PY
}

preflight() {
  local missing=()
  local module
  for module in cv2 numpy skimage usb dbus gi; do
    if ! check_python_module "$module" >/dev/null 2>&1; then
      missing+=("$module")
    fi
  done
  if [ "${#missing[@]}" -ne 0 ]; then
    printf 'Missing Python modules: %s\n' "${missing[*]}" >&2
    return 1
  fi

  local source
  for source in "${CONFIG_SOURCES[@]}"; do
    [ -f "$PROJECT_DIR/$source" ] || {
      printf 'Missing installation source: %s\n' "$PROJECT_DIR/$source" >&2
      return 1
    }
  done
  for source in "${PAYLOAD[@]}"; do
    [ -f "$PROJECT_DIR/bin/$source" ] || {
      printf 'Missing payload executable: %s\n' "$PROJECT_DIR/bin/$source" >&2
      return 1
    }
  done
  for source in "${PACKAGES[@]}"; do
    [ -d "$PROJECT_DIR/$source" ] || {
      printf 'Missing payload package: %s\n' "$PROJECT_DIR/$source" >&2
      return 1
    }
  done
  [ -f "$SCRIPT_DIR/egis-doctor" ]
}

restore_configs() {
  local index target backup
  for index in "${!CONFIG_TARGETS[@]}"; do
    target="${CONFIG_TARGETS[$index]}"
    backup="$CONFIG_BACKUP$target"
    if [ -e "$backup" ]; then
      install -D -m "$(stat -c %a "$backup")" "$backup" "$target"
    else
      rm -f "$target"
    fi
  done
}

rollback() {
  local status="$1"
  [ "$COMMITTED" -eq 0 ] || return 0
  trap - EXIT
  printf 'Installation failed; restoring the previous service stack.\n' >&2
  systemctl stop egis-bridge open-fprintd >/dev/null 2>&1 || true
  if [ "$INSTALLED" -eq 1 ]; then
    [ -d "$INSTALL_DIR" ] && mv "$INSTALL_DIR" "${INSTALL_DIR}.failed.${STAMP}"
    [ -d "$BACKUP_DIR" ] && mv "$BACKUP_DIR" "$INSTALL_DIR"
  fi
  [ -n "$CONFIG_BACKUP" ] && restore_configs
  systemctl daemon-reload >/dev/null 2>&1 || true
  restore_service_state open-fprintd "$OPEN_FPRINTD_ACTIVE" "$OPEN_FPRINTD_ENABLED"
  restore_service_state egis-bridge "$EGIS_BRIDGE_ACTIVE" "$EGIS_BRIDGE_ENABLED"
  [ -n "$STAGE_DIR" ] && rm -rf "$STAGE_DIR"
  [ -n "$CONFIG_BACKUP" ] && rm -rf "$CONFIG_BACKUP"
  exit "$status"
}

restore_service_state() {
  local service="$1" active="$2" enabled="$3"
  if [ "$enabled" = "enabled" ]; then
    systemctl enable "$service" >/dev/null 2>&1 || true
  else
    systemctl disable "$service" >/dev/null 2>&1 || true
  fi
  if [ "$active" = "active" ]; then
    systemctl start "$service" >/dev/null 2>&1 || true
  else
    systemctl stop "$service" >/dev/null 2>&1 || true
  fi
}

if [ "${1:-}" = "--check" ]; then
  preflight
  printf 'Source installation preflight passed.\n'
  exit 0
fi

if [ "$EUID" -ne 0 ]; then
  printf 'Run once as root: sudo ./install-stable.sh\n' >&2
  exit 1
fi

preflight
OPEN_FPRINTD_ACTIVE="$(systemctl is-active open-fprintd 2>/dev/null || true)"
OPEN_FPRINTD_ENABLED="$(systemctl is-enabled open-fprintd 2>/dev/null || true)"
EGIS_BRIDGE_ACTIVE="$(systemctl is-active egis-bridge 2>/dev/null || true)"
EGIS_BRIDGE_ENABLED="$(systemctl is-enabled egis-bridge 2>/dev/null || true)"
trap 'rollback $?' EXIT

STAGE_DIR="$(mktemp -d /opt/.egis-driver.stage.XXXXXX)"
CONFIG_BACKUP="$(mktemp -d /tmp/egis-config-backup.XXXXXX)"

for executable in "${PAYLOAD[@]}"; do
  install -m 0755 "$PROJECT_DIR/bin/$executable" "$STAGE_DIR/$executable"
done
install -m 0755 "$SCRIPT_DIR/egis-doctor" "$STAGE_DIR/egis-doctor"
PACKAGE_SOURCES=()
for package in "${PACKAGES[@]}"; do
  PACKAGE_SOURCES+=("$PROJECT_DIR/$package")
done
cp -a "${PACKAGE_SOURCES[@]}" "$STAGE_DIR/"
find "$STAGE_DIR" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$STAGE_DIR" -type f -name '*.pyc' -delete

for index in "${!CONFIG_TARGETS[@]}"; do
  target="${CONFIG_TARGETS[$index]}"
  if [ -e "$target" ]; then
    install -D -m "$(stat -c %a "$target")" "$target" "$CONFIG_BACKUP$target"
  fi
done

systemctl stop egis-bridge open-fprintd >/dev/null 2>&1 || true
if [ -d "$INSTALL_DIR" ]; then
  mv "$INSTALL_DIR" "$BACKUP_DIR"
fi
mv "$STAGE_DIR" "$INSTALL_DIR"
STAGE_DIR=""
INSTALLED=1

for index in "${!CONFIG_TARGETS[@]}"; do
  install -D -m 0644 \
    "$PROJECT_DIR/${CONFIG_SOURCES[$index]}" \
    "${CONFIG_TARGETS[$index]}"
done

install -d -m 0700 "$DATA_ROOT/egis" "$DATA_ROOT/egis-atlas" \
  "$DATA_ROOT/egis-gallery" "$DATA_ROOT/egis-calibration"
udevadm control --reload-rules
udevadm trigger --subsystem-match=usb
systemctl daemon-reload
systemctl enable open-fprintd egis-bridge >/dev/null
systemctl start open-fprintd egis-bridge

deadline=$((SECONDS + 20))
while [ "$SECONDS" -lt "$deadline" ]; do
  if systemctl is-active --quiet open-fprintd egis-bridge && \
     busctl --system call net.reactivated.Fprint \
       /net/reactivated/Fprint/Manager \
       net.reactivated.Fprint.Manager GetDefaultDevice 2>/dev/null | \
       grep -q '/net/reactivated/Fprint/Device/'; then
    COMMITTED=1
    break
  fi
  sleep 0.5
done

if [ "$COMMITTED" -ne 1 ]; then
  printf 'Fingerprint services did not become ready within 20 seconds.\n' >&2
  exit 1
fi

rm -rf "$CONFIG_BACKUP"
CONFIG_BACKUP=""
trap - EXIT
printf 'Installed EH575 driver to %s\n' "$INSTALL_DIR"
[ -d "$BACKUP_DIR" ] && printf 'Previous payload: %s\n' "$BACKUP_DIR"
