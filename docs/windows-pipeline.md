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

A subsequent continuous capture isolated the capture failure: EH575 traffic
ended exactly when the device was disabled during initialization, while the
PCAP continued for another 189 seconds. USBPcap's upstream command interface
distinguishes `--capture-from-all-devices` from
`--capture-from-new-devices`; both are required to retain a re-enumerated
sensor. The capture workflow now resets the sensor before capture, enables both
flags defensively, and rejects each empty biometric timeline phase rather than
validating aggregate session traffic.

The validated v4 Windows session establishes the image transfer boundary. Each
103 by 52 image arrives on endpoint `0x82` as a 5,120-byte prefix immediately
followed by a 236-byte suffix. Across 756 images there were no missing,
misordered, cross-device, or late fragment pairs. The private decoder therefore
reassembles only same-device `5120 + 236` pairs separated by at most 100 ms and
fails closed on replacement prefixes, intervening status payloads, or orphaned
suffixes.

The session produced 358 enrollment frames, genuine sequences of 47, 47, and
48 frames, and right-pinky impostor sequences of 87, 63, and 64 frames. Both
untouched controls produced zero frames. The same 5,356-byte `0x73` calibration
upload (SHA-256 `183c3fad...fb676`) recurred byte-for-byte across independent
processes and touches, which establishes that it is stable device calibration
state rather than touch image data.

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

The validated v4 capture and a narrower call-graph trace resolve part of that
ambiguity. `CTouchSensor::EngineAdapterAcceptSampleData` first copies the input
image into its original-image buffer, runs `FUN_180004540` to resample it to the
sensor engine's configured dimensions, copies that result into a second image
buffer, and passes the second buffer to `FUN_180007060`. The latter invokes the
proprietary feature extractor, serializes its result, and writes that record to
the skeleton buffer. Verification compares serialized feature records through
`FUN_180006010`; it does not compare the debug image buffers directly.

Enrollment calls `FUN_180005b10` once per accepted sample. Its state machine
distinguishes accepted, redundant, completed, and failed samples and updates an
enrollment object until the configured sample target is reached. On completion,
`FUN_180006ed0` produces the final serialized enrollment record. This is direct
evidence for feature-record accumulation across multiple presentations, not for
building one photographic mosaic across the whole enrollment stream.

These findings narrow, but do not eliminate, the unresolved work. The internal
feature record layout, the sample redundancy test, the feature merge operation,
and the final matcher score are proprietary routines and have not been ported.

## Validated Authentication Timeline

The v4 Windows Biometric Operational log independently confirms the labeled
capture outcomes. Enrollment ran during the enrollment PCAP and completed with
event 1010 at 16:31:34 UTC. Each right-index capture overlaps an event-1004
success at 16:32:51, 16:33:15, and 16:33:38 UTC. The three pinky captures contain
no corresponding success event; their later event-1005 records occur while the
attempt remains unsuccessful. These correlations agree with the capture-session
notes and rule out mislabeled successful probes as the explanation for the image
evaluation results.

## Matcher Diagnostics on the v4 Images

Private offline evaluation tested SourceAFIS against all 358 enrollment frames,
all 142 genuine frames, and all 214 pinky frames. Images were kept private; only
aggregate conclusions are recorded here.

At the best tested raw-frame scale, one genuine touch produced a continuous run
of 35 frames above score 20 and a maximum score of 58.69. A second genuine touch
produced weaker evidence (maximum 14.17), and the remaining genuine touch had no
usable match (maximum 7.22), despite all three succeeding under Windows. Pinky
trials produced isolated maxima up to 21.93, but no frame exceeded 20 in two of
the three trials and no pinky produced a long run of strong scores.

This establishes two separate facts:

- coherent identity evidence exists in some individual 103 by 52 Windows
  frames, so stitching is not a prerequisite for every useful comparison;
- SourceAFIS extraction from raw strips is not sufficient to reproduce the
  Windows decisions, even with exhaustive enrollment-frame comparison.

The current largest-component stitch is also lossy for this capture: it retained
only 48 of 358 enrollment frames because the Windows enrollment contains many
separate presentations. Treating the entire enrollment as one coordinate system
or selecting only its largest mosaic therefore discards most enrolled evidence.

Naive transforms using the repeated 5,356-byte calibration upload—including
offset background subtraction in both polarities, per-frame normalization, and
CLAHE—did not recover the two weak genuine trials or create a safe separation.
They remain diagnostics and must not be promoted into authentication.

The implementation direction supported by current evidence is a gallery of
per-presentation feature records with stream-level temporal evidence. SIFT can
still estimate motion within a coherent swipe segment, but a single stitched
image must not stand in for the full enrollment. Production integration remains
blocked on either recovering the Windows correction/feature behavior or finding
an extractor that works reliably on these narrow strips; merely lowering a
SourceAFIS threshold would admit observed pinky scores.

## Evidence Rules

- **Confirmed:** direct size, named-field, arithmetic, or call-order evidence.
- **Corroborated:** confirmed statically and observed in repeated USB captures.
- **Unresolved:** inferred purpose, indirect virtual call, or behavior lacking a
  dynamic observation.

Only corroborated transformations may produce authentication input. Confirmed
but incomplete behavior may be implemented as diagnostics. Unresolved behavior
is documented and omitted.
