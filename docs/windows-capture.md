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
   root hubs and attached devices. Find `1c7a:0575`, then record its control
   device (for example `\\.\USBPcap2`) and numeric device address.
5. Do not select a root hub or address by guesswork. USB addresses can change
   after reboot or device restart.

Run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\windows-capture.ps1 -UsbPcapDevice "\\.\USBPcap2" -DeviceAddress 2
```

The wizard exports the installed driver, hashes its files, records device and
WinBio configuration, and explains one physical action at a time. Read every
prompt before pressing Enter. The temporary enrollment requested by the wizard
must be deleted afterward.

## Return to Linux

Copy the resulting directory without opening individual captures:

```bash
mkdir -p .egis-lab/windows-driver/fresh
chmod 700 .egis-lab/windows-driver/fresh
```

Place the Windows files there, install `tshark`, and decode them privately:

```bash
./egis-lab analyze-windows-capture \
  .egis-lab/windows-driver/fresh \
  --output .egis-lab/windows-driver/analysis
```

The decoder keeps extracted candidate frames separate from its JSON report and
marks their boundary as unconfirmed until packet sizes and command transitions
are consistent across the repeated phases.

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
