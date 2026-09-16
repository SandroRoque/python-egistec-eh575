# Offline Matcher Development

The lab workflow keeps matcher research separate from the live authentication
stack. Raw biometric data stays private and is never committed.

## One-Time Development Snapshot

Create a private, immutable copy of the current templates and labeled samples:

```bash
sudo ./egis-lab snapshot --role development
```

The snapshot is stored under `.egis-lab/snapshots`, owned by the invoking user,
mode `0700`/read-only, checksummed, and selected through `.egis-lab/current`.
This command does not stop or modify the fingerprint services.

Run the restored baseline twice and write JSON/Markdown reports:

```bash
./egis-lab evaluate --config lab-configs/baseline.json
./egis-lab evaluate --config lab-configs/username-index.json
./egis-lab evaluate --config lab-configs/production-parity.json
```

A non-zero exit is expected when a matcher fails an acceptance gate. Candidate
experiments use copied JSON configurations or code changes and the same snapshot.
Compare reports with:

```bash
./egis-lab compare BASELINE.json CANDIDATE.json
```

Development gates are zero impostor accepts, at least 75% genuine success for
every target, deterministic repeated decisions, and p95 matching latency no
greater than 250 ms.

`baseline.json` and `username-index.json` remain historical experiments. Use
`production-parity.json` for a candidate intended for promotion. It matches the
live policy of three frames per attempt and three consecutive accepted attempts
for the same identity.
Reports record this policy, and candidate construction rejects reports that do
not match it.

Offline evaluation calls the independent `egis_matcher` library through a
persistence adapter. Matcher rejection reasons describe completed frame
windows. Live incomplete windows, I/O failures, unavailable devices, and
cancellations are capture outcomes and are measured separately.

## Ordered Touch Research

Record a complete contact, including low-contrast observations and its explicit
end marker:

```bash
sudo ./egis-lab record-sequence --label right-index-sweep \
  --finger right-index-finger --role development
```

The bridge is stopped only while this command owns the sensor and is restarted in
all exit paths. Data is written asynchronously under `.egis-lab/sequences` with
owner-only permissions. A full writer queue or capture timeout marks the manifest
incomplete. Recordings contain biometric data and must never be copied into the
repository, issues, logs, or compatibility reports.

Analyze the sequence without hardware access:

```bash
./egis-lab analyze-sequence .egis-lab/sequences/TIMESTAMP-LABEL
```

This writes a private JSON report plus one image per connected registration
component under the sequence's `analysis` directory. The report retains per-frame
transforms, references, inliers, spatial support, ridge agreement, quality, and
confidence. Discontinuities force a new component rather than inventing an
alignment. Use these reports to tune registration and compare coverage before
introducing an atlas enrollment format or streaming identity evidence into live
authentication.

Promotion order is: validate registration on development sequences, freeze the
implementation and thresholds, test unseen holdout sequences, then integrate the
new representation behind the existing matcher interface. Any representation
change may deliberately require fresh enrollment; there is no migration gate.

## Experimental Atlas Enrollment and Streaming Replay

Collect enrollment touches separately from development and holdout probes:

```bash
sudo ./egis-lab record-sequence --label right-index-enrollment \
  --finger right-index-finger --role enrollment
sudo ./egis-lab record-sequence --label left-index-enrollment \
  --finger left-index-finger --role enrollment
sudo ./egis-lab record-sequence --label right-thumb-enrollment \
  --finger right-thumb --role enrollment
sudo ./egis-lab record-sequence --label left-thumb-enrollment \
  --finger left-thumb --role enrollment
```

For a guided run that avoids finger mix-ups, use the four-finger session. It
names the required finger before every touch, waits for release, and retains
all private attempts for quality-based selection:

```bash
sudo ./egis-lab capture-enrollment-session
```

The default requires three clean touches and uses an eight-attempt safety cap
per finger. Use `--max-captures 12` or `--max-captures 0` to compare larger or
uncapped collections. Afterward, prepare atlases and cross-finger replay data
without recapturing impostors:

```bash
./egis-lab prepare-enrollment-session SESSION_ID \
  --output .egis-lab/atlases/SESSION_ID
```

Record separate development probes for each enrolled finger and for cross-finger
impostors. The probe metadata is checked against the atlas, so a genuine replay
must use the atlas finger and an impostor replay must use a different one.

Move the finger to expose complementary overlapping regions during each touch.
For each of the four fingers, record at least three separate enrollment touches
with different placements. Use labels such as
`right-index-enrollment-1`, `right-index-enrollment-2`, and
`right-index-enrollment-3`, then build one atlas per finger. This supplies
multiple enrollment keys and preserves separate touches for replay validation.
Build an atlas from one or more enrollment recordings:

```bash
./egis-lab enroll-sequences \
  .egis-lab/sequences/ENROLLMENT-TOUCH-1 \
  .egis-lab/sequences/ENROLLMENT-TOUCH-2 \
  --finger right-index-finger --output .egis-lab/atlases/right-index-v1
```

Enrollment requires complete recordings labeled `enrollment`. It retains quality
keyframes that add spatial feature coverage and omits redundant views from the
derived atlas. The original recordings remain unchanged. Components from different
touches are not assumed to align and their coverage is reported separately.

Replay independently recorded genuine and wrong-finger probes:

```bash
./egis-lab match-sequence .egis-lab/sequences/INDEX-PROBE \
  --atlas .egis-lab/atlases/index-v1 --expected genuine
./egis-lab match-sequence .egis-lab/sequences/THUMB-PROBE \
  --atlas .egis-lab/atlases/index-v1 --expected impostor
```

The incremental matcher reuses features and admits evidence only for new supported
regions with sufficient motion and consistent alignment. The report records each
frame's reason, supported cells, admitted-frame count, experimental sufficiency,
processing latency, and time to sufficient evidence along the capture timeline.

The retired custom minutiae backend and its replay command have been removed.
SourceAFIS and NBIS remain available for offline comparisons.

Replay a complete touch once against every enrolled experimental atlas with
shared feature extraction and identity-margin rejection:

```bash
./egis-lab match-touch-identity \
  .egis-lab/sequences/PROBE \
  --atlas-root .egis-lab/atlases/SESSION_ID
```

Atlas results are offline research evidence and cannot unlock the system.
Production accepts only `EGIS_MATCH_MODE=window` (the default); verification and
enrollment do not load experimental galleries or start Java. Remove old `shadow`
overrides before installing an updated service.

Build presentation galleries and replay every labeled development probe without
writing per-finger shell loops:

```bash
./egis-lab evaluate-feature-gallery \
  --sequence-root .egis-lab/windows-driver/replay-v4 \
  --session windows-v4 \
  --sourceafis-home .egis-lab/tools/sourceafis-3.18.1 \
  --scale 3 \
  --output .egis-lab/results/gallery-windows-v4
```

The command evaluates score, identity-margin, and consecutive-frame policies
and writes full trajectories only below `.egis-lab`. Status 2 means the candidate
remains correctly blocked from promotion.

For unenrolled-finger impostor coverage, capture both pinkies with the guided
private workflow. These recordings are development probes only and are never
added to an atlas:

```bash
sudo ./egis-lab capture-impostor-session
```

Evaluate the fingerprint-specific NBIS candidate on stitched touches:

```bash
sudo ./install-nbis.sh
export EGIS_NBIS_BIN=/opt/nbis-5.0.0/bin
./egis-lab evaluate-nbis \
  --session 20260914T035727Z \
  --include-development-probes \
  --output .egis-lab/results/nbis-evaluation
```

Exit status zero means every replay gate passed. Status 2 prevents integration.
The detailed report contains private biometric evidence and must not be added to
git; the command prints only aggregate results.

Evaluate the pinned SourceAFIS candidate, including full-touch and progressive
prefix replay, with:

```bash
sudo ./install-sourceafis.sh
export EGIS_SOURCEAFIS_HOME=/opt/sourceafis-3.18.1
./egis-lab evaluate-sourceafis \
  --session 20260914T035727Z \
  --include-development-probes \
  --output .egis-lab/results/sourceafis-evaluation
```

This is also non-promotable unless every aggregate gate passes. SourceAFIS runs
in a long-lived Java worker so reported comparison latency does not include JVM
startup. Progressive replay rejects a candidate if any impostor prefix crosses
the derived threshold or fewer than 90 percent of genuine touches settle on the
correct identity through the remainder of the swipe.

Windows driver investigation and the private USB capture procedure are recorded
in `windows-pipeline.md` and `windows-capture.md`. Corrected NBIS or SourceAFIS
replay accepts `--calibration-profile` only after the profile contains a fully
recovered difference conversion and no unresolved bad-pixel operation. An
incomplete profile fails closed instead of silently applying guessed image
enhancement.

Replay those probes against each of the four atlases before recalibrating
production thresholds. A candidate must reject every pinky probe and must also
require the same identity on consecutive confirmation attempts.
Enrollment and replay record the algorithm source digest and OpenCV/NumPy versions
so reports can be traced to the implementation that produced them.
Processing time is also recorded separately: replay evaluates every recorded frame
and does not simulate the live queue's scheduling or drops. Exact enrollment
recordings cannot be used as probes, even if copied to another path. Incomplete
recordings can be inspected but have `valid_trial=false` and no correctness score.

Atlas files and reports stay under `.egis-lab`, use owner-only permissions, and
refuse to overwrite previous artifacts. Atlases store numeric arrays without
pickle plus versioned metadata and checksums. They are biometric data, including
descriptors, transforms, and source paths; none belongs in Git or public reports.
Reports are explicitly experimental and non-promotable. The existing live matcher
and its thresholds are unchanged; real sequence accuracy, cross-touch alignment,
and calibration remain prerequisites for live adoption.

On an installed system, summarize privacy-safe runtime outcomes with:

```bash
egis-doctor --journal-summary --since today
```

The summary counts readiness, enrollment capture, verification capture, and
matching outcomes independently. Metric events contain counts, durations, and
reasons; they do not contain frames, templates, usernames, or finger names.

Verification capture metrics also report `queue_age_ms` (the age of the last
frame in a window when matching starts) and `queue_overflow` events with dropped
frame counts. Use these alongside matcher durations to distinguish consumer lag
from slow sensor acquisition. A recording terminated by repeated USB failures
is marked incomplete even when its capture thread exits normally.

The service uses one sensor owner across verification, enrollment, and recovery.
After installing controller changes, exercise ordinary verification, cancel and
immediate retry, enrollment, and repeated suspend/resume with verification armed.
Expected behavior is no result from the canceled operation, no reads after sleep
release, and recovery before new acquisition. Automated tests simulate blocked
reads and matching, but physical USB release and desktop behavior still require
this live check. A log reporting pending USB release means the owner is still
waiting for an already-dispatched backend call.

## Live latency reports

The service emits one privacy-safe `[LATENCY]` record for every completed live
touch. It separates request-to-touch time, first-frame delay, capture, queueing,
authoritative matching, confirmation attempts, and
touch-to-decision time. D-Bus dispatch queueing is recorded separately.

After several genuine and impostor lock-screen attempts, aggregate the journal:

```bash
sudo journalctl -u egis-bridge --since '-30 min' -o cat | \
  ./egis-lab latency-report --input - \
  --output .egis-lab/results/live-latency.json
```

The report contains only counts and latency distributions; it excludes images,
templates, identities, and matcher scores. It separates outcomes and records
confirmation progress, accepted-attempt timing, inter-frame timing, and deadline
expiry. The live contact budget remains three seconds until an untouched holdout
supports a replacement. Derive a candidate by rounding
`p95(time to third consecutive accept) + p95(inter-frame interval)` up to 100 ms;
accept it only if the same holdout has zero impostor accepts and no increase over
the three-second baseline.

When progress depends on a new enrollment format, stage it fail-closed with one
privileged command:

```bash
sudo ./stage-development
```

This backs up the installed payload, templates, and thresholds; installs only
the driver payload; enrolls index and thumb through the fixed path; and creates
a development snapshot. Any failure restores all three backups. Fingerprint
authentication stays disabled until a later holdout passes and is promoted.

## Holdout

After a candidate passes development, freeze its source, configuration, and
development evidence before touching the holdout:

```bash
./egis-lab freeze-candidate \
  --config lab-configs/production-parity.json \
  --development-report DEVELOPMENT-CANDIDATE.json \
  --output .egis-lab/candidates/CANDIDATE.freeze.json
```

Then collect the new physical dataset:

```bash
sudo ./egis-lab holdout \
  --candidate-freeze .egis-lab/candidates/CANDIDATE.freeze.json \
  --session-index 1 --session-id SESSION-A
sudo ./egis-lab holdout \
  --candidate-freeze .egis-lab/candidates/CANDIDATE.freeze.json \
  --session-index 2 --session-id SESSION-B
```

The first command archives previous samples; each session collects 10 presentations
for every target/actual pair, with three verification windows per touch, and always
restarts the bridge. The second session requires a distinct label, verifies at
least 20 combined presentations per pair, and creates the immutable `holdout`
snapshot. The default
selection is right and left index plus right and left thumb. At least three
selected fingers are required so every target faces two non-target fingers.
Development snapshots cannot be packaged for promotion, and holdout snapshots
without a valid pre-collection freeze cannot be evaluated for promotion.

This is a manual critical path: the four-finger all-pairs default is 320 labeled
presentations and commonly takes 8–16 hours across several sessions once retries,
full lifts, and USB recovery are included. Wrong-finger, partial, or interrupted
captures must be recollected, not relabeled. Until a broader multi-person and
multi-device study exists, the result supports only a single-user, single-EH575
best-effort claim; it is not a population FAR estimate.

Evaluate the baseline and candidate against the holdout snapshot. A candidate
artifact can be built only when its holdout report passes and its latency is no
more than 20% slower than a passing baseline. When the baseline fails its
authentication gates, it is not a valid performance reference and the 250 ms
absolute p95 limit remains mandatory:

```bash
./egis-lab build \
  --baseline HOLDOUT-BASELINE.json \
  --candidate HOLDOUT-CANDIDATE.json
```

## Promotion

Validate an artifact without privileges or service changes:

```bash
./promote-candidate --dry-run dist/egis-candidate-*.tar.gz
```

Promotion is the final deliberate privilege boundary:

```bash
sudo ./promote-candidate dist/egis-candidate-*.tar.gz
```

The promoter verifies every checksum, backs up `/opt/egis-driver` and the live
thresholds, installs atomically, reruns calibration, and checks service state,
hardware readiness, and D-Bus registration. Any failure restores the previous
code and thresholds and restarts the previous services.

## Security Rules

- Never add `.egis-lab` or raw sample files to Git.
- Never tune acceptance thresholds to make impostor samples pass or disappear.
- Existing development samples cannot serve as final holdout evidence.
- Do not deploy intermediate experiments to `/opt`.
- Keep password unlock enabled through live lock/idle/suspend acceptance tests.
