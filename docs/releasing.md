# Release Procedure

Release artifacts are built from a committed Git tree. The same deterministic
source archive feeds the Arch and Fedora recipes.

## Candidate Checks

```bash
./tools/check
./tools/build-release --output dist/release
sha256sum -c dist/release/SHA256SUMS
```

`tools/check` compiles Python, checks shell syntax, runs the nonprivileged source
installer preflight, rejects tracked private/generated data, and runs the test
suite. The release builder refuses a dirty tracked tree by default and records
the source commit and vendored manager tree in `release-manifest.json`.

CI must pass on Python 3.12, 3.13, and 3.14. It also builds the rendered Arch
package and Fedora RPM from the generated source archive. Package success is not
hardware evidence.

## Hardware Release Gate

Before tagging:

1. Re-run the frozen private holdout at least five times.
2. Require deterministic decisions, zero impostor accepts, every target at or
   above its genuine-pass gate, and p95 matching latency at or below 250 ms.
3. Install the exact release candidate through the package/source path.
4. Complete the lifecycle matrix in `docs/compatibility.md`.
5. Confirm password authentication remains available throughout testing.
6. Generate and inspect a sanitized public compatibility report.

For a stable support claim, collect passing lifecycle reports from two
independent machines. Until then, publish a release candidate and label the
hardware support experimental.

## Tagging

The canonical version is `egis_driver/version.py`; `pyproject.toml` reads it
dynamically. Arch and Fedora recipe versions are checked against it in tests.
After all gates pass, tag that exact commit as `vVERSION`, upload every file in
`dist/release`, and verify the hosted archive against `SHA256SUMS`.

Do not build release artifacts from a deployed `/opt/egis-driver` tree or copy
live enrollment/calibration data into a package.
