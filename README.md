# EgisTec EH575 Fingerprint Driver

Linux driver for the EgisTec EH575 fingerprint sensor (USB VID `0x1c7a`, PID `0x0575`).
Integrates with `open-fprintd` / `fprintd` for desktop fingerprint authentication.

This is a fork of the original [python-egistec-eh575](https://github.com/abbhinavjayaraman/python-egistec-eh575) / `open-fprintd-eh575` work.
It keeps the upstream reverse-engineering lineage intact while extending the driver,
matcher, calibration flow, and lock-screen integration.

**Support status:** experimental. Revision `1072` has a local known-good
baseline. Support is not yet established across multiple independent laptops;
see [Compatibility Validation](docs/compatibility.md) before relying on it as the
only authentication path.

## Supported Devices

Found in some ASUS and Lenovo laptops with onboard fingerprint readers using this sensor.

Check your device:
```bash
lsusb | grep -i '1c7a:0575'
```

## Architecture

```
DeviceProfile → USB backend → frame features → matcher → service state machine
                                                    ↓
fprint client ← D-Bus device ← vendored manager ← egis-bridge
```

The repository stays a monorepo because these layers are released and acceptance
tested together, but their interfaces are explicit. The manager is vendored with
recorded provenance and remains isolated from the EH575 protocol. See
[Architecture and Repository Boundaries](docs/architecture.md).

## Repository Layout

```
open-fprintd-eh575/
├── bin/
│   ├── egis-bridge          # D-Bus bridge service (main entry point)
│   ├── egis-calibrate       # Calibration and audit collection tool
│   └── open-fprintd         # Manager daemon
├── egis_driver/
│   ├── device_profile.py    # Immutable EH575 protocol and geometry
│   ├── egis_driver.py       # Validated USB backend
│   ├── interfaces.py        # Backend contract consumed by services
│   ├── services.py          # Scan and suspend/resume state machine
│   └── fingerprint_matcher.py  # Fingerprint matching engine
├── openfprintd/
│   ├── device.py            # D-Bus device proxy
│   ├── manager.py           # D-Bus manager
│   └── polkit.py            # Authorization helper
├── setup.py                 # Compatibility shim for Python packaging
├── 70-egis-eh575.rules      # udev rule for USB access
├── *.service                # systemd units
├── *.conf                   # D-Bus policy
└── *.policy                 # PolKit permissions

compatibility/               # Public schemas and sanitized known-good evidence
packaging/                   # Arch and Fedora release recipes
tools/check                  # Canonical local/CI validation command
tools/build-release          # Deterministic release archive builder
egis-doctor                  # Sanitized compatibility diagnostic
install-stable.sh            # Transactional source deployment with rollback
```

Enrolled fingerprint templates are stored in `/var/lib/open-fprintd/egis`.
Calibration samples and generated thresholds are stored in `/var/lib/open-fprintd/egis-calibration`.

## Enrollment & Verification Algorithm

### Enrollment
1. 10 touches, each captures continuously at ~29 FPS while finger is on sensor
2. Quality-gated frame selection (contrast, sharpness, foreground, ridge clarity)
3. SSIM-based near-duplicate removal
4. Top ~40 diverse schema-v4 templates stored with SIFT descriptors, normalized image patches, and ridge-orientation descriptors

### Verification
1. 3-frame ensemble capture per attempt
2. FLANN/SIFT matching proposes candidate templates for the requested user and finger
3. Each live frame is aligned independently with RANSAC
4. Aligned patches are checked for image correlation and ridge-orientation consistency
5. Acceptance requires the fixed identity policy, calibrated validation, and enough margin over competing enrolled fingers

The matcher logs a diagnostic score with distance weighting and ridge consistency. That score is for tuning/debugging; authentication uses calibrated metric thresholds.

Verification fails closed until validated calibrated thresholds exist. Matcher v5 builds the nearest-neighbor index for the claimed username before applying the ratio test, keeps candidate slots available for competing enrolled fingers, and measures identity margin between fingers rather than between templates of the same finger. Existing schema-v4 enrollments remain compatible, but calibration thresholds from earlier matcher versions are rejected and must be regenerated. Older template schemas are ignored because they do not contain the image/ridge data needed for hardened verification.

## Installation

### Install from source

```bash
git clone https://github.com/SandroRoque/python-egistec-eh575.git
cd python-egistec-eh575
./install-stable.sh --check
sudo ./install-stable.sh
```

The nonprivileged preflight checks dependencies and the exact installation
inputs. The single privileged invocation stages the payload, backs up the
previous code and configuration, installs checked-in systemd/D-Bus/PolKit/udev
files, and rolls back automatically if the D-Bus device does not become ready.
It preserves enrollment and calibration state.

After installation:

```bash
systemctl status open-fprintd egis-bridge --no-pager
egis-doctor
```

Do not install an unverified AUR package under this project name. Release archives
contain checksum-pinned Arch and Fedora recipes generated from the same Git tree;
see [Release Procedure](docs/releasing.md).

## Dependencies

| Package | Arch | Fedora |
|---------|------|--------|
| Python 3 | `python` | `python3` |
| OpenCV | `python-opencv` | `python3-opencv` |
| NumPy | `python-numpy` | `python3-numpy` |
| scikit-image | `python-scikit-image` | `python3-scikit-image` |
| PyUSB | `python-pyusb` | `python3-pyusb` |
| D-Bus | `python-dbus` | `python3-dbus` |
| GLib | `python-gobject` | `python3-gobject` |

Install on Arch:
```bash
sudo pacman -S python-opencv python-numpy python-scikit-image python-pyusb python-dbus python-gobject
```

## Updating

From a source installation:
```bash
cd python-egistec-eh575
git pull
./tools/check
sudo ./install-stable.sh
```

Re-enrollment is required when the template schema changes. Matcher v5 can reuse schema-v4 enrollments, but requires calibration analysis to be rerun so validated matcher-v5 thresholds are written.

## Usage

```bash
# Enroll the default finger (right index)
fprintd-enroll

# Enroll a specific finger
fprintd-enroll -f right-thumb
fprintd-enroll -f left-index-finger

# Verify
fprintd-verify

# List enrolled fingers
fprintd-list "$USER"

# Delete fingerprints
fprintd-delete "$USER"
```

Common finger names:
```text
left-thumb
left-index-finger
left-middle-finger
left-ring-finger
left-little-finger
right-thumb
right-index-finger
right-middle-finger
right-ring-finger
right-little-finger
```

## Calibration

Calibration validates the fixed identity policy. Genuine samples confirm the enrolled print is readable. Wrong-finger samples are an audit set only: they are used to detect false accepts, not to train or tune what the matcher should reject.

Example for an enrolled right index finger:
```bash
# Stop the bridge so the calibration tool can access the USB device
sudo systemctl stop egis-bridge

# Collect genuine samples from the enrolled finger
sudo PYTHONPATH=/opt/egis-driver /opt/egis-driver/egis-calibrate collect \
  --username "$USER" \
  --target-finger right-index-finger \
  --actual-finger right-index-finger \
  --label genuine \
  --samples 8

# Collect impostor samples from other fingers against that target
sudo PYTHONPATH=/opt/egis-driver /opt/egis-driver/egis-calibrate collect \
  --username "$USER" \
  --target-finger right-index-finger \
  --actual-finger right-middle-finger \
  --label impostor \
  --samples 8

sudo PYTHONPATH=/opt/egis-driver /opt/egis-driver/egis-calibrate collect \
  --username "$USER" \
  --target-finger right-index-finger \
  --actual-finger left-index-finger \
  --label impostor \
  --samples 8

# Analyze and write /var/lib/open-fprintd/egis-calibration/thresholds.json if validation passes
sudo PYTHONPATH=/opt/egis-driver /opt/egis-driver/egis-calibrate analyze \
  --write-thresholds

sudo systemctl start egis-bridge
```

The analyzer writes `/var/lib/open-fprintd/egis-calibration/report.json`. If an audit wrong-finger sample passes the fixed identity policy, it refuses to write validated thresholds and authentication remains disabled. Do not loosen thresholds to fit negative samples; improve enrollment quality or the matcher itself.

The current identity policy is intentionally global, not per-finger. `thresholds_by_target` is expected to be empty unless a future matcher version explicitly introduces a stricter per-target policy.

## Offline Matcher Development

Matcher experiments should not be deployed to the live lock-screen stack. The
private lab workflow creates a single privileged snapshot and then performs all
replay, benchmarking, comparison, and candidate construction without root:

```bash
sudo ./egis-lab snapshot --role development
./egis-lab evaluate --config lab-configs/baseline.json
./egis-lab evaluate --config lab-configs/username-index.json
```

After a candidate passes the development dataset, one `sudo ./egis-lab holdout`
session collects the untouched physical acceptance matrix. Only a passing holdout
report can produce an artifact for the final manual promotion command. See
[`docs/offline-development.md`](docs/offline-development.md) for the complete
workflow and rollback behavior.

## Reproducibility and Compatibility

Run the complete nonprivileged repository check with:

```bash
./tools/check
```

CI exercises Python 3.12-3.14, the D-Bus contract, privacy guards, deterministic
source archives, and Arch/Fedora package builds. Hardware and desktop lifecycle
behavior cannot be proven in CI. Use `egis-doctor` and the sanitized compatibility
workflow in [Compatibility Validation](docs/compatibility.md) to contribute
evidence without publishing biometric data.

## Debugging

```bash
# View service logs
sudo journalctl -u open-fprintd -u egis-bridge -f

# Capture raw frames for inspection (stop the bridge first)
sudo systemctl stop egis-bridge
sudo python3 debug_sensor.py
# Frames saved to /tmp/egis_debug/
sudo systemctl start egis-bridge
```

Useful checks:
```bash
systemctl status open-fprintd egis-bridge --no-pager
sudo journalctl -u egis-bridge -n 80 --no-pager
ls -ld /var/lib/open-fprintd/egis
ls -ld /var/lib/open-fprintd/egis-calibration
```

For the idle-suspend hyprlock fingerprint failure and the session-lifecycle fix, see
[`docs/hyprlock-suspend-fingerprint.md`](docs/hyprlock-suspend-fingerprint.md).
