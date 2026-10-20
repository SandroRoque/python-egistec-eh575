# Windows EH575 Pipeline Evidence

This document records sanitized conclusions from the private Ghidra exports
and the driver package retained under `.egis-lab/windows-driver`; the exports
themselves, driver binaries, captures, and biometric payloads are not part of
the repository.

The analyzed package is `egistouchfp0575.inf` version 3.7.1.1 (2020-06-15).
`EgisTouchFP0575.dll` (the UMDF driver, SHA-256 `25704878...7b2631`) loads at
image base 0x180000000; virtual addresses below are in that image. "Line"
references point into the private `ghidra_dumps/EgisTouchFP0575.c` export,
whose function addresses match this build.

## Confirmed Sensor Calibration Layout

The EH575 image geometry is 0x67 by 0x34 pixels (103 by 52), or 0x14ec (5,356)
bytes. Calibration serialization near export lines 8308 and 8443 names four
one-byte sensor settings: `sensor_gain`, `sensor_vref_sel`, `sensor_dc_p`, and
`sensor_dc_c`.

The same structure serializes:

- `vdm_bk`: 5,356 bytes beginning at structure offset 0x0e;
- one intermediate byte at offset 0x14fa;
- `bad_pixel`: 5,356 binary bytes beginning at offset 0x14fb.

The intermediate byte is used as a replacement/fill value by the calibration
statistics path (`FUN_1800082d4`). The image-repair path below does not read
it.

Bad-pixel calibration (`FUN_180008bf4`) clears the map, captures three frames,
computes a frame reference value, and marks pixels whose absolute deviation
exceeds 0x20. The routine subsequently expands some adjacent marks and rejects
an excessive map. That calibration-time expansion is still unresolved; it only
matters when a new map is built, not when a stored map is applied.

## Recovered Bad-Pixel Repair

`FUN_18000c0f8(image, width, height, map, passes)` applies a stored map. Both
callers pass width 103, height 52, the calibration `bad_pixel` map, and
`passes = 2`. The stored map is never modified; the routine works on a stack
copy.

1. **Widening** (assembly-confirmed). Scan the copy in linear order; when
   `m[i] == 1`, set `m[i+1] = 1` and skip index `i+1`. Every flagged pixel
   therefore also flags its right neighbour, including across the row
   boundary, with no cascade beyond one step.
2. **Iterative repair** (`FUN_1800051c0`). For up to `passes` passes, visit
   every pixel in row-major order whose map byte has bit 0 set:
   - Estimate orientation with `FUN_180005360(image, map, x, y, w, h, 16)`:
     over the 16×16 window `x-8..x+7`, `y-8..y+7`, skipping image borders, take
     3×3 Prewitt (1,1,1) gradients `gx`, `gy` at every interior sample whose map bytes
     at `x..x+2` on rows `y-1..y+1` are all zero. The check is off-centre by
     one column in the shipped code (a 32-bit load at `x-1` masked with
     `0xffffff00`; assembly-confirmed). Accumulate `gxx`, `gyy`, `gxy`; with
     `n` samples and `avg(v) = (v + n/2) / n` in truncating C division,
     `a = 2*avg(gxy)` and `b = avg(gxx) - avg(gyy)`. The angle is `atan2`-like in units of
     1.5° (0..120 over 180°) from a 129-entry half-angle arctangent table at
     0x18002d5b0 (entry `i = round(16*atan(i/128)/2/1.5°)`, verified for all
     entries), with special results 30 when `b == 0, a <= 0` and 90 when
     `b == 0, a > 0`. No samples returns 0xff (skip).
   - Quantize to a direction bucket `((angle + 7) * 274 >> 12) mod 8`.
   - Interpolate with `FUN_1800057a4`: weighted mean of the eight 3×3
     neighbours using row `bucket` of the 8×8 weight table at 0x18002d640,
     skipping neighbours whose map bit 0 is set and columns outside
     `0..width-2`. Weight rows (neighbour order NW N NE W E SW S SE):
     `0:[0,0,0,16,16,0,0,0]` (horizontal), `2:[16,0,0,0,0,0,0,16]`,
     `4:[0,16,0,0,0,0,16,0]` (vertical), `6:[0,0,16,0,0,16,0,0]`, and odd rows
     split weight 8 across the two adjacent directions. A zero weight sum
     leaves the pixel flagged.
   - A repaired pixel is written into the image immediately and its map byte
     becomes 2, so later pixels in the same pass use the repaired value.
   A pass that repairs nothing returns early.

## Production Capture Path

The EH575 sensor object is not a plain `CET300`. Its constructor
`FUN_1800154fc` first runs the CET300 constructor 0x18000e894 and then
installs vtable 0x18003f128, names the device "Fingerprint ET510", resets the
image to 103×52, and shrinks the image buffer to 0x14ec bytes. The ET510
vtable overrides `GetImage` (slot 0xb8 → 0x180016b70) and the WinBio payload
(slot 0x90 → 0x1800161a4); it inherits slot 0x120 unchanged.

`IOCTL_BIOMETRIC_CAPTURE_DATA` (0x440014) reaches the image as follows, each
step a direct call unless noted:

1. IOCTL dispatch `FUN_180019af4` → `FUN_18001e06c`, which validates the
   request and starts worker thread `FUN_18001d85c` → `FUN_18001d19c`;
2. `FUN_18001bf68` → sensor slot 0xb8 (virtual) = ET510 `GetImage`
   0x180016b70;
3. `FUN_18000b9e4` capture loop → `FUN_18000b178` (source file
   `ET5XX\egis_fp_get_image.c`), a state machine: 0x13 initialise
   (`FUN_180009d0c`), 0x14 finger wait (`FUN_18000a1a0`, `FUN_18000ac9c`), 0x15
   frame processing `FUN_18000a93c` (calls `FUN_1800099d4`, `FUN_18000b5d0`,
   `FUN_180008010`, and quality score `FUN_18000b670`; a low-quality result
   0xe is tolerated here);
4. still in state 0x15, copy the 5,356-byte frame, run
   `FUN_180005184(image, 103, 52, 0x8c)`, then the bad-pixel repair
   `FUN_18000c0f8`;
5. back in `GetImage`, copy the result row by row from the last row upward, a
   vertical flip, into the 0x14ec image buffer;
6. the worker fills the WinBio payload through sensor slots 0x88 (0x30-byte
   header, inherited) and 0x90 (0x1800161a4: a 0x1524-byte record with a
   103×52, 363 dpi image copied from that buffer through sensor slot 0x20).

The bad-pixel repair is therefore on the live capture path. `FUN_180016468`
(ET510 slot 0xc0) is a separate route: it fetches an image logged as
`bootRaw` and processes it through `FUN_18000b980` → `FUN_18000a82c`, which
additionally subtracts the image minimum and returns 0xe without repair for
quality scores below 60. Its caller was not traced.

The remaining unrecovered stages between the USB frame and the repair are
`FUN_1800099d4`, `FUN_18000b5d0`, `FUN_180008010`, and `FUN_180005184`;
the repair cannot be reproduced on Linux until they are known.

## CET300 Difference Conversion (Not on the EH575 Path)

`CET300::GetImage` (0x1800107d0, export line 11484) belongs to the 128×128
base class, which the ET510 subclass overrides. It computes
`background_byte - raw_byte` over a 16 KiB background at object offset 0x390
when `FetchImageMode != 2` and passes each signed difference through virtual
slot 0x120. Neither the ET510 `GetImage` nor the production path above calls
slot 0x120, so this conversion does not apply to EH575 images. It is recorded
because an earlier revision of this document assumed it did.

The CET300 constructor (0x18000e894) installs vtable 0x18003e570; slot 0xb8
holds `GetImage`, confirming the base, and slot 0x120 holds 0x18001c870. That
target is a leaf function with no direct callers, so Ghidra does not define it;
the result below comes from its raw x86-64 disassembly. It reads four 32-bit
object fields:

| Field | Role | Default |
|-------|------|---------|
| `+0x18` | gain, in 1/16 units | 48 |
| `+0x1c` | difference offset | 0 |
| `+0x20` | range high | 128 |
| `+0x24` | range low | 0 |

For a signed difference `d`, with C (truncate-toward-zero) integer division:

```text
d' = d - offset
if d' < 0: return 255
t  = (gain * d' + 8) / 16
v  = t * 255 / (high - low) - low
return 255 - clamp(v, 0, 255)
```

The defaults are written as two 8-byte stores (`0x30`, `0x80`) by the base
constructor 0x18001ba70, which CET300's constructor calls first. No other
write to these fields was found in the export, and no registry or calibration
string names them, so the defaults are the values a CET300 instance uses. This
is static evidence only; a runtime override was not excluded by observation.

With the defaults the conversion is `255 - min(255, (3 * d) * 255 / 128)` for
`d >= 0` and 255 otherwise: non-positive differences are white, values fall by
about six per step, and differences of 43 or more are black. The 511 possible
inputs are this function's domain; there is no stored table.
`windows_difference_lut()` in `calibration.py` reproduces this function exactly
(tested against an instruction-level transcription of 0x18001c870), but it
describes CET300 output and must not be used as an EH575 `difference_lut`.

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
5. process it through the unrecovered stages listed under Production Capture
   Path, then repair bad pixels and flip it vertically;
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
