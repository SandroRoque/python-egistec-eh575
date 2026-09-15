# Guided Private Windows Capture

The Windows capture contains fingerprint images and must never be uploaded,
attached to an issue, or committed. The Linux repository guard rejects the
capture directories, but the Windows directory must still be handled as
biometric data.

## Preparation

1. Install Wireshark with USBPcap from the official installer.
2. Copy `tools/windows-capture.ps1` to Windows.
3. Insert the handoff USB drive as `D:` and open an elevated PowerShell window.

Run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\windows-capture.ps1
```

`USBPcap2` is the root established for this machine. Override it only if the
hardware moves to another controller: `-UsbPcapDevice "\\.\USBPcapN"`.

The wizard resets and waits for the EH575, starts USBPcap with both
`--capture-from-all-devices` and `--capture-from-new-devices`, records the
baselines, locks Windows for each verification attempt, detects return from the
lock screen, opens enrollment settings, timestamps phases, validates every
biometric phase separately, and copies the validated result to `D:`. The user
only presents the named finger, enters the PIN after an expected rejection, and
completes/closes the Windows enrollment UI.

The per-phase gate is intentional: initialization commands or calibration data
cannot make an otherwise empty verification capture pass.

## Return to Linux

Copy the resulting directory without opening individual captures:

```bash
mkdir -p .egis-lab/windows-driver/fresh
chmod 700 .egis-lab/windows-driver/fresh
```

Place the Windows files there and decode them privately. Classic `.pcap` files
use the built-in decoder; `.pcapng` requires `tshark`:

```bash
./egis-lab analyze-windows-capture \
  .egis-lab/windows-driver/fresh \
  --output .egis-lab/windows-driver/analysis
```

The decoder identifies EH575 addresses from injected descriptors, filters other
USB devices, pairs protocol payloads with EGIS commands, and segments continuous
captures using `timeline.jsonl`. Raw frames stay separate from the JSON report.

After recording paired Linux sequences, compare raw transport statistics with:

```bash
./egis-lab compare-windows-linux \
  --windows-analysis .egis-lab/windows-driver/analysis \
  --linux-sequences .egis-lab/sequences/SEQUENCE... \
  --output .egis-lab/windows-driver/raw-comparison.json
```

USBPcap sees USB transport payloads, not host-corrected Windows images. A raw
comparison can establish whether Windows requests different sensor data; it
cannot by itself validate background correction.
