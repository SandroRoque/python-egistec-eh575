# EgisTec EH575 Fingerprint Driver

Linux driver for the EgisTec EH575 fingerprint sensor (USB VID `0x1c7a`, PID `0x0575`).
Integrates with `open-fprintd` / `fprintd` for desktop fingerprint authentication.

## Supported Devices

Found in some ASUS and Lenovo laptops with onboard fingerprint readers using this sensor.

Check your device:
```bash
lsusb | grep -i '1c7a:0575'
```

## Architecture

```
Hardware (USB)  →  egis_driver.py  →  fingerprint_matcher.py  →  egis-bridge  →  D-Bus
                  (USB comm,           (SIFT candidate search,    (D-Bus service,  (open-fprintd
                   frame capture)       per-frame alignment,       scan loop,       integration,
                                        ridge/image checks)        state machine)   PolicyKit)
```

## Repository Layout

```
open-fprintd-eh575/
├── bin/
│   ├── egis-bridge          # D-Bus bridge service (main entry point)
│   ├── egis-calibrate       # Calibration and audit collection tool
│   └── open-fprintd         # Manager daemon
├── egis_driver/
│   ├── egis_driver.py       # USB hardware driver
│   └── fingerprint_matcher.py  # Fingerprint matching engine
├── openfprintd/
│   ├── device.py            # D-Bus device proxy
│   ├── manager.py           # D-Bus manager
│   └── polkit.py            # Authorization helper
├── setup.py                 # Python packaging
├── 70-egis-eh575.rules      # udev rule for USB access
├── *.service                # systemd units
├── *.conf                   # D-Bus policy
└── *.policy                 # PolKit permissions

debug_sensor.py              # Debug tool for capturing raw frames
install-stable.sh            # One-shot deployment script
```

Enrolled fingerprint templates are stored in `/var/lib/open-fprintd/egis`.
Calibration samples and generated thresholds are stored in `/var/lib/open-fprintd/egis-calibration`.

## Enrollment & Verification Algorithm

### Enrollment
1. 10 touches, each captures continuously at ~29 FPS while finger is on sensor
2. Quality-gated frame selection (contrast, sharpness, foreground, ridge clarity)
3. SSIM-based near-duplicate removal
4. Top ~40 diverse v3 templates stored with SIFT descriptors, normalized image patches, and ridge-orientation descriptors

### Verification
1. 3-frame ensemble capture per attempt
2. FLANN/SIFT matching proposes candidate templates for the requested user and finger
3. Each live frame is aligned independently with RANSAC
4. Aligned patches are checked for image correlation and ridge-orientation consistency
5. Acceptance requires the fixed identity policy, calibrated validation, and enough margin over competing enrolled fingers

The matcher logs a diagnostic score with distance weighting and ridge consistency. That score is for tuning/debugging; authentication uses calibrated metric thresholds.

Verification fails closed until validated calibrated thresholds exist. Matcher v3 ignores older template files because they do not contain the image/ridge data needed for hardened verification; re-enroll after installing this version.

## Installation

### Option 1: Install from source

```bash
git clone https://github.com/SandroRoque/python-egistec-eh575.git
cd python-egistec-eh575
sudo bash install-stable.sh
```

The installer copies the driver to `/opt/egis-driver`, installs systemd/D-Bus/PolKit/udev configuration, creates `/var/lib/open-fprintd/egis`, and restarts the services.

After installation:

```bash
systemctl status open-fprintd egis-bridge --no-pager
```

### Option 2: AUR helper

```bash
yay -S open-fprintd-eh575
# or
paru -S open-fprintd-eh575
```

Use this only after the AUR package has been published and updated to this fork.

### Option 3: Build from AUR manually

```bash
git clone https://aur.archlinux.org/open-fprintd-eh575.git
cd open-fprintd-eh575
makepkg -si
```

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

Via AUR helper:
```bash
yay -Syu open-fprintd-eh575
```

From source:
```bash
cd python-egistec-eh575
git pull
sudo bash install-stable.sh
```

Re-enrollment is required when the matching algorithm changes. Matcher v3 requires new enrollments and validated calibration.

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
