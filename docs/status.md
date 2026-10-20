# Project Status — 2026-09-16

This is the pause point for a multi-week break. The working revision is
`38de042` on `main`. [CI run 35149146998](https://github.com/SandroRoque/python-egistec-eh575/actions/runs/35149146998)
passed every job, and `./tools/check` passes 178 tests.

## Installed State

`/opt/egis-driver` matches the published payload; this was verified by diff.
The systemd drop-in
`/etc/systemd/system/egis-bridge.service.d/90-window-mode.conf` sets
`EGIS_MATCH_MODE=window`. Runtime configuration rejects every other value.
Because the unit uses `Restart=always`, a stray `shadow` value would crash-loop
rather than degrade gracefully.

Rollback restores the `/opt/egis-driver.backup.*` directory reported by
`install-stable.sh`. Deployment and rollback ownership are described in
[architecture.md](architecture.md).

## Measured Baseline

- Warm bridge: 195 MB, three processes, no JVM; the former shadow path used
  1,227 MB.
- Successful unlock: about 378 ms from touch to decision, typically three
  attempts.
- A badly read touch consumes the full 3.0-second contact budget and returns
  retry, producing a bimodal latency distribution.
- After the switch, 3 of 9 sessions were retries. All three had
  `deadline_expired=true`; one reached two of three consecutive accepts.

## Operational Notes

- Enrolled fingers: `right-index-finger` and `right-thumb` only.
- If `SUPER+CTRL+L` does nothing, a live `hyprlock` can make
  `omarchy-system-lock` silently no-op. Check with `pidof hyprlock`; recover a
  confirmed wedged instance with `killall hyprlock`. Never add a blind
  `pkill hyprlock` to the lock path: killing an active lock would unlock the
  machine. Use `hyprctl layers` to distinguish a wedged process from an active
  lock. See [hyprlock-suspend-fingerprint.md](hyprlock-suspend-fingerprint.md).
- `/etc/pam.d/hyprlock` deliberately omits `pam_fprintd.so`; hyprlock drives
  fingerprint authentication natively. Do not re-add it.

## Known Limits

- Authentication thresholds have never passed an untouched out-of-sample
  holdout; the promotion gates were fit to the data they graded.
- Only one identity is enrolled per finger, so `min_margin` cannot
  discriminate.
- SourceAFIS and NBIS remain offline-only and non-authoritative.
- The vtable slot `0x120` difference conversion was recovered on 2026-10-19,
  but a later trace showed it belongs to the 128×128 `CET300` base class. The
  EH575 (`ET510` subclass) capture path never calls it, so
  `windows_difference_lut()` is not an EH575 correction. The EH575 path and
  its bad-pixel repair are traced in [windows-pipeline.md](windows-pipeline.md).
- The EH575 frame-processing chain (minimum subtraction, inversion, corner
  correction, quality score and best-frame selection, normalisation, repair,
  flip) was recovered on 2026-10-19 and ported bit-exactly to
  `egis_matcher/windows_image.py`, verified against the driver under
  emulation. It is not yet wired into live capture: the service does not
  apply it, run the Windows exposure loop, or select among frames.
- Replaying the v4 Windows frames through that chain with SourceAFIS
  strengthened one genuine touch (20 of 25 frames ≥ 40, versus 4 raw) but
  left two Windows-accepted touches unmatched, so image correction alone does
  not close the matcher gap.
- Linux never sends the sensor background (`vdm_bk`, EGIS opcode 0x73) that
  Windows uploads during init, so Linux frames are not background-subtracted:
  19% of Linux pixels saturate versus 1.8% on Windows. An opt-in
  `EgisDriver(background=...)` upload was verified live on 2026-10-20: it
  removed all saturation and brought frames to Windows levels. Linux can fit
  its own table (`sudo ./egis-lab calibrate-sensor-background`; 99.87% of
  pixels within one step of the Windows table). The service and
  `record-sequence` upload it only when
  `/var/lib/open-fprintd/egis-sensor-background.bin` exists; installing it
  changes the frame domain, so existing enrollments and thresholds must be
  rebuilt afterwards.
- On 2026-10-20 the background was installed in production on the
  development laptop (table mean 31.15, empty frames 102.8 ± 2.1), right index
  and right thumb were re-enrolled, and thresholds were rebuilt. The SIFT
  matcher then scored all 8 genuine right-index samples (median score 104,
  max 209) while 7 of 8 right-middle impostor samples produced no alignment at
  all; validation passed 6/8 genuine and 0/8 impostor, and `fprintd-verify`
  matched on the first touch. The right ring finger, which previously
  unlocked as the right index, was then collected as a second impostor set:
  0 of 8 samples produced any alignment and none passed. This is a
  single-session result, not a holdout.
- `egis-calibrate collect` defaulted to 3 frames per sample while `analyze`
  requires two consecutive 3-frame attempts, so no default sample could pass;
  the default is now 6. `install-stable.sh` left `/opt/egis-driver` mode 0700,
  so `egis-enroll` failed for normal users; it now stages the payload 0755.

The durable design and research context lives in
[architecture.md](architecture.md),
[offline-development.md](offline-development.md), and
[windows-pipeline.md](windows-pipeline.md). Private captures and reports remain
under `.egis-lab/` and must not be published.

## Deferred Work and Stop Condition

Part B, Windows calibration recovery, is limited to two hours with a mandatory
60-minute checkpoint. Part C must produce one sequence representation that both
engines can replay before any holdout is collected.

As of 2026-10-19 the production capture path is traced from
`IOCTL_BIOMETRIC_CAPTURE_DATA` to the WinBio payload, and the bad-pixel repair
and final vertical flip are recovered, and the same day the frame-processing
stages before the repair were recovered and ported (`windows_image.py`).
SourceAFIS replay through that chain improved one genuine touch only. It also
exposed an evidence-backed sensor-configuration difference: Linux omits the
`vdm_bk` background upload. A live test confirmed the upload removes Linux
saturation, and Linux can now fit the table itself. What remains is installing
the table, re-recording enrollment and probes with it, and wiring the Windows
chain into capture.

If matching on uploaded, Windows-processed frames still does not separate
genuine from impostor touches, stop matcher investment. Keep the single SIFT path at its
experimental ceiling, keep password authentication available, and leave
SourceAFIS offline.

## Resume Checklist

1. Run `./tools/check`.
2. Confirm the actual CI conclusion on `main`; inspecting `ci.yml` is not enough.
3. Run `egis-doctor --json`.
4. Confirm `EGIS_MATCH_MODE=window` is still effective.
5. Install the sensor background (`sudo ./egis-lab calibrate-sensor-background`)
   and re-record enrollment and probes with it before any further matcher work.
