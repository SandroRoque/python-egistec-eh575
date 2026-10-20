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

The intermediate byte is the bad-pixel fill value used by `FUN_1800082d4`,
which runs on every captured frame (stage 1 under "Recovered Frame
Processing"). The image-repair path below does not read it.

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

The stages between the USB frame and the repair are recovered below.

## Recovered Frame Processing

Recovered 2026-10-19 and ported to `egis_matcher/windows_image.py`. Every
routine was checked bit for bit against the driver's own code, executed under
CPU emulation (Unicorn, x86-64) on real v4 frames, synthetic ridge patterns,
noise, flat images, and random bad-pixel maps up to 15% density: 120 cases for
each stage primitive (genuine and impostor captures) and 480 for stage 1 and
the repair, with no mismatches.
A mutation check confirmed that the comparison detects single-constant
changes. `tests/test_windows_image.py` pins the full chain to digests the
emulated driver produced for deterministic synthetic (non-biometric) frames,
so CI enforces equivalence without the DLL.

The per-frame chain in state 0x15 (`FUN_18000a93c`) is:

1. **Exposure and minimum subtraction** (`FUN_1800099d4` → `FUN_18000938c` →
   `FUN_1800095e0`). `FUN_18000938c` is hardware exposure control: it samples a
   window and steps sensor registers 0x0f and 0x12 using a 16-entry step table
   when the window saturates or bottoms out; it changes no pixels. The frame is
   then read (`FUN_18000c220`), and `FUN_1800082d4` overwrites every pixel
   whose `bad_pixel` byte is exactly 1 with `calib[0x14fa] + 1` and returns
   the minimum of the other pixels. `FUN_1800095e0` subtracts that minimum
   from every byte with uint8 wraparound. No host-side `vdm_bk` subtraction
   occurs anywhere on this path: the calibration loader uploads `vdm_bk` to
   the sensor as the 5,356-byte `0x73` payload (see the replay notes under
   "Matcher Diagnostics"), so background subtraction happens on the sensor.
2. **Inversion** (`FUN_18000b5d0(image, 0)`): `p = 255 - p`.
3. **Corner correction** (`FUN_180008010(image, 103, 52, 0)`). Resample a copy
   to 160×80 (`FUN_180005e70`: separable area-weighted box resampling in
   1/1024 fixed point, vertical pass first, `(sum + step/2) / step`), then
   segment it (`FUN_1800138e0` mode 3; below) into a 10×5 block coherence
   map. For each image corner's 8×8 block, the coherence of the matching map
   corner (grid bytes 0, 9, 40, 49) selects a bucket: below 60 is background,
   above 120 is ridge, otherwise ignored (`FUN_18000a43c`). With `bg` and
   `fg` the bucket means, if any background corner exists and
   `255 - bg <= 255 - fg`, add `255 - bg` to every pixel with clamping
   (`FUN_18000610c`). If the resampled image's mean is at least 245 the
   segmentation is skipped and the corners are read from the resampled
   pixels themselves.
4. **Quality score** (`FUN_18000b670` → `FUN_180014dac`), on a 144×80 resample
   (9×5 blocks). It returns `(score, corners, edges)`:
   - if the image mean is at least 245, the score is 0;
   - a copy with its brightest eighth saturated (`FUN_18000623c`) gives an
     Otsu threshold `t` and upper-class mean `u` (`FUN_180005b34`); a 16×16
     block is "bright" when its mean is below `max(245, u - (u - t)/8)`;
     `corners` counts bright blocks among the four corners and `edges` among
     those four plus four edge blocks inset `(blocks_x - 4)/3` blocks;
   - segmentation yields orientation and coherence maps; each bright block
     whose mean is at least the image mean and whose map coherence is below
     80 is subtracted back out of the counts;
   - `score = 0` when at least two-thirds of the blocks have orientation
     outside 4..116 (near-horizontal); otherwise
     `score = 256 - 256 * background_blocks / blocks`, where a block is
     background when at most half of its 3×3 neighbourhood has coherence
     above 80 (`FUN_180013bbc`).
5. **Frame selection** (`FUN_18000a93c`). The processed finger-detect frame
   seeds the best image. Each new frame replaces it when both its score and
   `edges` are at least the best's. The loop stops on a score below 60, on the
   tenth frame, or from the second frame on when `edges > 7` and the score
   moved by less than 9. Best `edges` below 2 sets flag 0x1, below 8 flag 0x8.
6. **Normalisation** (`FUN_180005184(image, 103, 52, 0x8c)`). With `t` the Otsu
   threshold over pixels below 240 (`FUN_180005d38`; first strict maximum of
   the 64-bit between-class variance), `k = (255 - 0x8c) * 16 / (255 - t)`
   (16 when `t = 255`) and each pixel becomes
   `255 - clamp(((255 - p) * k + 8) >> 4, 0, 255)`.
7. The bad-pixel repair above, then the vertical flip in `GetImage`.

Segmentation (`FUN_1800138e0` / `FUN_180014dac`) applies, on 16×16 blocks:
a 3×3 box mean (`FUN_18001497c`); a 9×9 box mean of that (`FUN_180014b1c`);
a local contrast stretch of the 3×3 image around the 9×9 mean ± the mean
absolute deviation over a radius-64 window (`FUN_18001456c`); another 3×3
box mean; then per-block gradient moments (`FUN_180013d2c`). Box means clip
their window at the image edge and divide by the clipped sample count.
Gradients use 1-4-1 weights,
`gx = (b[x+1]-t[x-1]) + 4(m[x+1]-m[x-1]) + (t[x+1]-b[x-1])` and
`gy = (b[x+1]-t[x-1]) + 4(b[x]-t[x]) - (t[x+1]-b[x-1])`, accumulated in
32-bit integers over each block (excluding the outer image ring) and summed
over the clipped 3×3 block neighbourhood, divided by 2304 with rounding.
Orientation uses the same half-angle arctangent as the repair. Coherence is
`sqrt(((Sxx'-Syy')² + (2Sxy')²) / ((Sxx'+Syy')²/65025))` on the moments
divided by 36 and halved twice (truncated at each step), zero when the
divided-by-36 `Sxx + Syy` is below 50². The square root uses a tiered table whose entries are
exactly `round(sqrt((q >> s) << s))` for `s = 0, 4, 5, 6, 7`, followed by a
nearest-integer refinement. Neither table needs to be copied from the driver.

Two shipped-code quirks are reproduced deliberately. The repair's widening
step can write one byte past its 5,356-byte stack copy of the map, and the
orientation mask check (a 32-bit load) can read that byte; the port models it
as an extra map byte that starts at zero. The repair interpolator advances
its weight index only inside rows that are in range, so at the top and bottom
image rows the weights shift relative to their neighbours.

Hardware exposure control and the finger-detect state (`FUN_18000ac9c`,
including its `FUN_18001542c` touch test) are not ported; they decide when
frames are taken, not how pixels are computed.

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
5. process it through the stages under "Recovered Frame Processing", select
   the best of up to ten frames, normalise, repair bad pixels, and flip it
   vertically;
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

### Linux omits the background upload (2026-10-19)

Decoding the v4 genuine-touch PCAP command by command shows that the Linux
`EH575_PROFILE` init, final, rearm, and trigger strings are a byte-for-byte
replay of the Windows stream, with one omission: Windows sends `73 14 ec` and
the 5,356-byte `vdm_bk` payload immediately after `61 50 03` / `60 50 03` and
before `60 40 ec`; Linux never does. Since that payload is the sensor
background (see "Matcher Diagnostics"), Linux frames are never
background-subtracted on the sensor. The difference is visible in the data:

| Source | Frames | Mean | Pixels at 255 | Frames reaching 255 |
|--------|--------|------|---------------|---------------------|
| Windows v4 stream | 714 | 119 | 1.8% (p95 7.8%) | 27% |
| Linux sequences | 10,191 | 207 | 19.3% (p95 61%) | 96% |

Linux also replays the Windows exposure probe (`63 2c 02 00 57`, `62 67 03`
reads min/max/mean) but ignores the result; Windows reads register 0x0f and
would step it (`FUN_18000938c`) on saturation. In the v4 session the probe
never saturated, so Windows left 0x0f at 0x20.

`EgisDriver(background=...)` can now send the upload at the Windows position.
It is off by default: every Linux threshold and enrollment was built from
non-subtracted frames.

A live A/B on the same sensor (2026-10-20, 20 empty and 40 touch frames per
mode, same finger) confirmed the effect. The sensor acknowledged the upload
(`SIGE 14 ec 01`) and its output moved to Windows levels:

| | Current Linux init | With `vdm_bk` upload |
|---|---|---|
| Empty mean / within-frame std | 228 / 13.0 | 102 / 2.8 |
| Empty pixels at 255 | 2.3% | 0% |
| Touch mean | 193 | 121 |
| Touch pixels at 255 / frames reaching 255 | 5.5% / 100% | 0% / 0% |
| Windows quality score, median | 228 | 256 |

A repeat run minutes later reproduced it: current-init touch frames saturated
26.7% of pixels (every frame clipped) against 0% with the upload, and empty
frames again settled at mean 102, std 2.8. Linux clipping therefore varies
widely between touches; the upload removes it in both runs.

The empty-frame level matches the Windows exposure probe (min/mean/max 97–104
in the v4 PCAP). After the Windows chain, ridges in the uploaded frames are
continuous where the current frames show broken, noisy ridges. The payload
used was the v4 Windows upload for this sensor.

**Generating the table on Linux.** Windows builds `vdm_bk` once, at
calibration (`FUN_180007bc0`): pre-calibration `FUN_180009a40` reads the
min/max/mean statistics (`63 2c 02 00 15`, poll 0x2d, `62 67 03`) and writes
the mean to register 0x33 (`63 33 03 <mean> 10 01`, poll 0x35); then
`FUN_1800096d8` sets register 0x50 to 1 and reads 5,356 bytes with opcode
0x72 (`FUN_180017f64`, framed exactly like the `0x64` image read). No Windows
capture contains this exchange; every session reused the stored table. On
the live sensor the same sequence is acknowledged and the registers settle as
expected, but `0x72` returned 5,356 zero bytes in every state tried: after
init, after uploading the Windows table, and after the full pre-calibration.
Uploading an all-zero table then zeroed every frame, so `vdm_bk` is a
per-pixel level control rather than a subtracted image, and it persists
across re-initialisation until replaced.

The table is instead fitted directly. With uniform tables the empty-frame
output is linear in the table value for every pixel (slope 5.52, p5–p95
5.46–5.59; residual RMS 0.35 over levels 24–38). Solving each pixel's line for
the Windows empty level (102.6) gives a table with mean 31.11 and std 2.41,
against 31.09 and 2.31 for Windows; 99.87% of pixels are within one step of
the Windows table (correlation 0.976). Empty frames with the fitted table were
flatter than with Windows' own (std 2.09 versus 2.83). `EgisDriver.calibrate_background()`
implements this fit with levels 24, 31, and 38; it needs an untouched sensor.
`sudo ./egis-lab calibrate-sensor-background` runs it, rejects results whose
empty frames are not flat near the target (a finger on the sensor), and
installs the table as `egis-sensor-background.bin` under the data root. The
service and `record-sequence` upload that file when present, and recorded
sequences store its SHA-256 as `sensor_background_sha256`.

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

### Replay through the recovered Windows chain (2026-10-19)

The same v4 frames were replayed through `windows_image` (stages 1–3, the
quality gate at 60, normalisation, flip) with an all-zero bad-pixel map: the
v4 capture does not include the device registry key that holds `bad_pixel`
(written through WDF registry callbacks by `FUN_18000bb6c`/`FUN_18000bd4c`),
and no pixel deviated by more than 0x20 from its frame median in even 90% of
the 714 replayed frames, so no stuck pixels are evident. The Windows output has dark
ridges on a whitened background; the raw strips' row-stripe artifacts are
largely suppressed. The quality gate accepted 302 of the enrollment frames and
36–37 frames per genuine touch.

SourceAFIS at scale 3 against all 302 accepted enrollment frames, 25 evenly
spaced accepted frames per probe touch, compared with inverted raw frames on
the same frame selection:

| Probe | Raw max / ≥40 | Windows chain max / ≥40 |
|-------|---------------|-------------------------|
| genuine 1 | 16.0 / 0 | 7.6 / 0 |
| genuine 2 | 46.5 / 4 | 62.2 / 20 |
| genuine 3 | 11.1 / 0 | 22.4 / 0 |
| pinky 1–3 | ≤ 23.1 / 0 | ≤ 15.2 / 0 |

Scales 2 and 4 were worse for both inputs. The corrected images strengthen
one genuine touch markedly and lower impostor maxima, but two touches that
Windows accepted still have no matching frame. The Windows image correction is
therefore not the remaining gap; the Egis matcher's handling of these narrow
strips is. NBIS was not installed on the analysis host and was not run.

The loader also settles the `0x73` upload: `FUN_18000bb6c` reads `vdm_bk`
and passes it directly to `FUN_180009964` → `FUN_18000c490`, which sends
`width * height` bytes through transport-object slot 0x78. In the vtable at
0x18003f4c0 (installed by the constructor at 0x1800154d8) that slot is
`FUN_180019514`, the opcode-0x73 wrapper. The observed 5,356-byte `0x73`
payload is therefore the `vdm_bk` background, and background subtraction
happens on the sensor. Byte 0x14fa is not persisted; it
is the "vdm target mean" read back from the sensor by `FUN_180009748` during
calibration.

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
