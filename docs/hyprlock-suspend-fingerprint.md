# Hyprlock Suspend Fingerprint Debugging

This note documents the June 2026 idle-suspend unlock failure and the fix that made it work.

## Symptom

Manual lock/unlock with fingerprint worked. Idle-triggered lock followed by suspend did not.

The failing sequence was:

1. Hypridle turned the display off.
2. Hypridle started `hyprlock`.
3. Hyprlock started fingerprint verification.
4. Hypridle suspended the system while that verification was active.
5. After wake, touching the sensor did not unlock.
6. With `pam_fprintd.so` in `/etc/pam.d/hyprlock`, password unlock was also slow.

The key observation was that after wake there was often no new bridge log like:

```text
[BRIDGE] Verify requested for user: <username>
```

That meant the sensor was not necessarily failing to detect a finger. In the failing path, the lock screen was not starting a fresh verification request after wake.

## Important False Leads

Several USB and resume-recovery hypotheses looked plausible because `fprintd-verify` worked immediately after logging in, while the lock screen did not.

The useful but incomplete changes were:

- More resume diagnostics: `seconds_since_resume`, resume generation, warmup contrast, no-touch contrast windows.
- USB reauthorization/reset attempts after resume.
- Avoiding full sensor reinitialization during normal verify preparation.
- Reducing long USB timeout paths so password fallback was not blocked by the bridge's D-Bus thread.
- A systemd `system-sleep` hook that stopped and restarted the fingerprint services.

The system-sleep hook was actively wrong for hyprlock's native fingerprint flow. It restarted the service stack, but hyprlock did not automatically create a new fingerprint verify request after wake.

## PAM Finding

`/etc/pam.d/hyprlock` originally included:

```pam
auth sufficient pam_fprintd.so
auth include login
```

Hyprlock also had native fingerprint enabled:

```conf
auth {
    fingerprint:enabled = true
}
```

This meant fingerprint was configured twice: once through hyprlock's native fingerprint path and once through PAM.

`pam_fprintd` is serial PAM. Password authentication waits until fingerprint authentication returns. Removing `pam_fprintd.so` from hyprlock PAM fixed the slow password fallback:

```pam
# PAM configuration file for hyprlock
auth include login
```

Hyprlock's native fingerprint integration should own fingerprint unlock.

## Root Cause

For idle suspend, hyprlock started one fingerprint verification before suspend. When suspend happened, the old bridge logic treated that as a terminal cancellation:

```text
[DEVICE] Terminating active fingerprint verify for suspend
[BRIDGE] Cancel requested
[BRIDGE] Emitting VerifyStatus result=verify-no-match done=True
```

After resume, hyprlock did not issue another `VerifyStart`. The lock screen still existed, but our service had ended the fingerprint transaction that hyprlock expected to keep using.

The core bug was therefore session lifecycle, not matching and not simple USB readiness.

## Idle Failure Counter

An armed verification with no finger present must remain silent. A previous
15-second no-touch timeout emitted terminal `verify-no-match` even though no
authentication had been attempted. Hyprlock could start another verification
after each terminal result, causing the lock screen to show several failed
attempts when the display was turned back on.

No-touch periods now keep the verification transaction armed indefinitely.
Periodic contrast logging and USB health recovery continue, but only an actual
finger scan may produce an authentication result.

## Fix

The working behavior is:

1. Preserve hyprlock's active claim across suspend.
2. Pause the active verify scan before releasing USB for sleep.
3. Reconnect/recover the sensor after resume.
4. Resume the same verify loop after recovery.
5. Do not emit terminal `verify-no-match` just because suspend happened.

The expected log shape is:

```text
[DEVICE] Preserving active fingerprint claim for suspend
[SERVICE] Pausing active verify for suspend
[SERVICE] Verify paused for suspend; no terminal status emitted
[DRIVER] Releasing USB resources for sleep
...
[SERVICE] Service resume: starting recovery
[SERVICE] Resume recovery: reconnecting sensor
[SERVICE] Resume recovery ready in ...
[SERVICE] Resuming suspended verify loop
```

There does not need to be a new post-wake `Verify requested` log. In the working path, the old hyprlock verification session is resumed.

## Keep During Trimming

These pieces are load-bearing for idle-suspend fingerprint unlock:

- Hyprlock PAM must not include `pam_fprintd.so` when hyprlock native fingerprint is enabled.
- `openfprintd.Device.Suspend()` must preserve an active claim instead of clearing it.
- `EgisService.suspend()` must pause active verify without emitting terminal `verify-no-match`.
- `EgisService.resume()` must restart a paused verify after resume recovery.
- The installer should remove any old `egis-fprint-system-sleep` hook.

These pieces are diagnostic or optional and can be reconsidered later:

- Very verbose matcher/contrast logging.
- Some USB sysfs reauthorization machinery, if future logs show normal reconnect is enough.
- Extra no-touch refresh logic, as long as resume recovery and paused-verify restart still work.
