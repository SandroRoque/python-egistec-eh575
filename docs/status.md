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
  its bad-pixel repair are traced in [windows-pipeline.md](windows-pipeline.md);
  four frame-processing stages before the repair (`FUN_1800099d4`,
  `FUN_18000b5d0`, `FUN_180008010`, `FUN_180005184`) remain unrecovered, so
  `calibration.py` still cannot reproduce Windows images.

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
and final vertical flip are recovered. What remains is the four frame-processing
stages before the repair, then corrected NBIS/SourceAFIS replay.

If those stages are unrecoverable and no evidence-backed
sensor-configuration difference exists, stop matcher investment. Keep the single SIFT path at its
experimental ceiling, keep password authentication available, and leave
SourceAFIS offline.

## Resume Checklist

1. Run `./tools/check`.
2. Confirm the actual CI conclusion on `main`; inspecting `ci.yml` is not enough.
3. Run `egis-doctor --json`.
4. Confirm `EGIS_MATCH_MODE=window` is still effective.
5. Decide explicitly whether to fund recovery of the remaining frame-processing stages.
