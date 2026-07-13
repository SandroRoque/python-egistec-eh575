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
once, collects 8 genuine and 8 cross-finger impostor touches for both the right
index and right thumb, with three verification windows per touch, always restarts
the bridge, and creates a snapshot labeled `holdout`. Development snapshots
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
