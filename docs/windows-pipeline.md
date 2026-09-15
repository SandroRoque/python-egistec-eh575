# Windows EH575 Pipeline Evidence

This document records sanitized conclusions from the private Ghidra exports.
Addresses identify the decompiled `EgisTouchFP0575` and
`EgisTouchFPEngine0575` images retained under `.egis-lab/windows-driver`; the
exports themselves, driver binaries, captures, and biometric payloads are not
part of the repository.

## Confirmed Sensor Calibration Layout

The EH575 image geometry is 0x67 by 0x34 pixels (103 by 52), or 0x14ec (5,356)
bytes. Calibration serialization near FP offsets 0x8308 and 0x8443 names four
one-byte sensor settings: `sensor_gain`, `sensor_vref_sel`, `sensor_dc_p`, and
`sensor_dc_c`.

The same structure serializes:

- `vdm_bk`: 5,356 bytes beginning at structure offset 0x0e;
- one intermediate byte at offset 0x14fa;
- `bad_pixel`: 5,356 binary bytes beginning at offset 0x14fb.

The intermediate byte is used as a replacement/fill value by the calibration
statistics path. That does not yet prove that the production image path uses
the same bad-pixel replacement rule.

Bad-pixel calibration clears the map, captures three frames, computes a frame
reference value, and marks pixels whose absolute deviation exceeds 0x20. The
routine subsequently expands some adjacent marks and rejects an excessive map.
The exact adjacency traversal and the production replacement consumer remain
unresolved, so neither is implemented yet.

The capture path near FP offset 0x11484 computes `background_byte - raw_byte`
for every pixel when `FetchImageMode != 2`. It passes each signed difference
through the sensor object's unresolved virtual method at vtable offset 0x120
before writing the image submitted to the next stage. Consequently, subtraction
direction is confirmed, but clipping, offset, and nonlinear conversion are not.
The implementation exposes the signed difference only as diagnostics and fails
closed if asked for corrected pixels without a recovered 511-entry conversion
table.

Calibration metadata includes `CALIBRATE_VERSION(Sensor)`,
`CALIBRATE_TYPE(Sensor)`, and `CALIBRATE_DATE(Sensor)`. Calibration and image
acquisition also consult `FetchImageMode`, `FingerOnMode`, sensor/detect modes,
and remote-wakeup configuration.

## Capture Lifecycle

The observed Windows-side lifecycle is:

1. leave detection mode before pre-calibration;
2. select detect or sensor calibration mode;
3. configure sensor registers and image mode;
4. fetch one 103×52 raw image;
5. optionally subtract `vdm_bk` through the unresolved conversion;
6. submit the resulting image to the Windows biometric engine;
7. restore detection/remote-wakeup behavior.

Linux already preserves continuous ordering above repeated atomic
rearm/trigger/read/drain cycles. Fresh USBPcap evidence is required to correlate
these high-level Windows operations with exact wire commands and response
boundaries.

## USB Capture Findings

The first root-wide Windows capture corroborates the following initialization
transaction on bus 1, device address 3:

1. host sends `EGIS 73 14 ec` on endpoint `0x01`;
2. host sends exactly 5,356 payload bytes on endpoint `0x01`;
3. device returns `SIGE 14 ec 01` on endpoint `0x82`.

The uploaded bytes have mean 31.09 and standard deviation 2.31. Their size and
low contrast are consistent with a sensor background/calibration map, but the
buffer's producer has not yet been recovered, so that identity remains
unresolved. `FUN_18001925c` constructs opcode `0x73`, passes the caller's buffer
and length through the transport object, and validates the seven-byte EGIS
acknowledgement. Its wrapper is `FUN_180019514`; its indirect caller remains to
be identified.

The sensor re-enumerated from address 3 to address 5. Later capture files
contained the address-5 descriptor but bulk traffic only for unrelated address
1. They contain no Windows verification or enrollment sensor stream and must
not be used to infer matcher behavior.

## Enrollment Engine

The engine reads Windows' `Minimum Fingerprint Samples`. Values from 8 through
20 are accepted; other values fall back to 16, followed by a mode-dependent
adjustment of one or two samples. Vendor configuration includes `Optimization`,
`SmartLearn`, and twenty enrollment-policy registry values.

The engine has buffers logged as `Verify_Skeleton` and the misspelled
`Verify_Orininal`, clears enrollment state explicitly, and maintains separate
original/skeleton-sized buffers. These labels do not establish whether the
skeleton is the authoritative template, a diagnostic representation, or an
intermediate. Their call graph and mutation behavior must be resolved before
implementing stream accumulation.

## Evidence Rules

- **Confirmed:** direct size, named-field, arithmetic, or call-order evidence.
- **Corroborated:** confirmed statically and observed in repeated USB captures.
- **Unresolved:** inferred purpose, indirect virtual call, or behavior lacking a
  dynamic observation.

Only corroborated transformations may produce authentication input. Confirmed
but incomplete behavior may be implemented as diagnostics. Unresolved behavior
is documented and omitted.
