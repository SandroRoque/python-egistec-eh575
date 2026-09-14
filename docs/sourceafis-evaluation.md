# SourceAFIS Evaluation Decision

SourceAFIS 3.18.1 was evaluated as a second established replacement for the
temporary image-feature identity matcher. The experiment reused the 12 trusted
enrollment touches for four fingers and seven private development touches from
two unenrolled pinkies. Detailed biometric scores and prefix histories remain
under `.egis-lab` and are not tracked.

The bounded matrix covered three compositor strategies, three image scales,
and both polarities. The winning full-touch configuration used median
compositing, 1.5x scaling, normal polarity, and 500 DPI metadata. It did not
pass the integration gate:

- extraction succeeded for all 19 touches;
- only 8 of 12 leave-one-touch-out enrollment probes selected the right finger;
- the minimum genuine score was 0 while the maximum impostor score was 12.68;
- only 4 of 12 genuine touches achieved a stable correct progressive prefix;
- one right-pinky prefix crossed the full-touch impostor-derived threshold;
- p95 extraction was 124.1 ms and p95 comparison was 4.0 ms.

SourceAFIS therefore must not become authentication authority and was not
deployed. The successful full extraction rate, paired with poor separation,
shows that process integration is no longer the limiting issue. Conventional
full-print minutiae matchers do not obtain reliable identity evidence from the
current EH575 composites. The next investigation should focus on small-area
partial-fingerprint matching or on materially improving the biometric
reconstruction itself, with this evaluator retained as the regression gate.
