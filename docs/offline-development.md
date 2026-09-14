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
live policy of three frames per attempt and two consecutive accepted attempts.
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

Record separate development probes for each enrolled finger and for cross-finger
impostors. The probe metadata is checked against the atlas, so a genuine replay
must use the atlas finger and an impostor replay must use a different one.
```

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

After a candidate is frozen, collect the only new physical dataset required:

```bash
sudo ./egis-lab holdout
```

This single privileged command archives previous live samples, stops the bridge
once, collects 8 genuine touches for each selected finger and 8 samples for every
cross-finger target/actual pair, with three verification windows per touch, always
restarts the bridge, and creates a snapshot labeled `holdout`. The default
selection is right and left index plus right and left thumb. Development snapshots
cannot be packaged for promotion.

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
