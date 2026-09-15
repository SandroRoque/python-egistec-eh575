# Guided Private Windows Capture

The Windows capture contains fingerprint images and must never be uploaded,
attached to an issue, or committed. The Linux repository guard rejects the
capture directories, but the Windows directory must still be handled as
biometric data.

## Preparation

1. Install Wireshark with USBPcap from the official installer.
2. Copy `tools/windows-capture.ps1` to Windows.
3. Open an elevated PowerShell window.
4. Run `USBPcapCMD.exe` once without capture arguments to display the USBPcap
   root hubs and attached devices. Find `1c7a:0575` and record its control
   device (for example `\\.\USBPcap2`). The wizard follows changing device
   addresses within that root.

Run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\windows-capture.ps1 -UsbPcapDevice "\\.\USBPcap2"
```

The wizard first records and validates a device reinitialization. It then keeps
one root-wide capture open across all verification actions, validates that it
contains EH575 commands and full-size transfers, and only then records the
temporary enrollment. It exports the installed driver, hashes its files,
records device and WinBio configuration, and writes exact action times to
`timeline.jsonl`. Read every prompt before pressing Enter. Delete the temporary
enrollment afterward.

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
