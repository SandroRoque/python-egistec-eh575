# Compatibility Validation

Compatibility has three evidence levels. Do not describe a machine as supported
based only on package installation or device enumeration.

| Level | Evidence | Meaning |
|-------|----------|---------|
| Build | CI unit, D-Bus integration, source installer checks, Arch package, Fedora RPM | The software and packages are internally consistent |
| Hardware | `egis-doctor` passes and enrollment/evaluation gates pass | The sensor revision and matcher work on one machine |
| Lifecycle | Hardware evidence plus restart, suspend/resume, locked-idle, password fallback, and standard fprint client checks | The complete desktop authentication path is validated |

The initial public release remains **EH575 experimental** until at least two
independent machines submit passing lifecycle reports. The local known-good
record in `compatibility/known-good.json` is baseline evidence, not a claim about
all laptops containing this USB ID.

## Diagnostic Report

Run the installed diagnostic without exposing biometric data:

```bash
egis-doctor --json --output doctor.json
```

The report includes versions, USB descriptors, endpoint layout, service state,
directory modes, and calibration validity. It excludes usernames, hostnames,
serial numbers, bus paths, templates, scores tied to files, and raw captures.
Review the JSON before sharing it.

## Public Compatibility Report

1. Create a private holdout snapshot and evaluate it as described in
   `docs/offline-development.md`.
2. Copy `compatibility/lifecycle.example.json` outside the repository and record
   the manual lifecycle outcomes. It deliberately starts in a failing state; do
   not change a field to `pass` until that check has actually completed.
3. Generate the sanitized aggregate:

```bash
./egis-lab compatibility-report \
  --evaluation .egis-lab/results/RESULT.json \
  --lifecycle /path/to/lifecycle.json \
  --output compatibility-report.json
```

The generator requires three successful service restarts, five successful
suspend/resume cycles, at least 30 locked-idle minutes with zero false failures,
working password fallback, and working standard fprint clients. It anonymizes
finger and user target keys and only exports aggregate acceptance results.

The public formats are versioned by:

- `compatibility/doctor.schema.json`
- `compatibility/lifecycle.schema.json`
- `compatibility/report.schema.json`

Schema version changes are reviewed as public API changes.

## Reporting Results

Attach only `compatibility-report.json` to a compatibility issue. Never attach
`.egis-lab`, `.npz`, `.npy`, raw frames, enrollment templates, calibration sample
directories, or journal output containing usernames. A failed result is useful;
keep the password authentication path enabled while testing.
