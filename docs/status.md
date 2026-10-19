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
- The Windows difference conversion at vtable slot `0x120` was recovered on
  2026-10-19: it is a gain/offset/clamp formula, not a stored table, and
  `windows_difference_lut()` in `calibration.py` generates the 511-entry LUT
  from it (see [windows-pipeline.md](windows-pipeline.md)). Windows
  bad-pixel replacement remains unresolved, so `calibration.py` still fails
  closed for any profile whose bad-pixel map is non-empty.

The durable design and research context lives in
[architecture.md](architecture.md),
[offline-development.md](offline-development.md), and
[windows-pipeline.md](windows-pipeline.md). Private captures and reports remain
under `.egis-lab/` and must not be published.

## Deferred Work and Stop Condition

Part B, Windows calibration recovery, is limited to two hours with a mandatory
60-minute checkpoint. Part C must produce one sequence representation that both
engines can replay before any holdout is collected.

The conversion half of Part B is done (2026-10-19). What remains is the
bad-pixel adjacency traversal and production replacement rule, then corrected
NBIS/SourceAFIS replay with a real `vdm_bk` background.

If bad-pixel replacement is unrecoverable and no evidence-backed
sensor-configuration difference exists, stop matcher investment. Keep the single SIFT path at its
experimental ceiling, keep password authentication available, and leave
SourceAFIS offline.

## Resume Checklist

1. Run `./tools/check`.
2. Confirm the actual CI conclusion on `main`; inspecting `ci.yml` is not enough.
3. Run `egis-doctor --json`.
4. Confirm `EGIS_MATCH_MODE=window` is still effective.
5. Decide explicitly whether to fund the remaining bad-pixel half of Part B.
