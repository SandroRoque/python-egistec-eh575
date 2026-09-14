# NBIS Evaluation Decision

NBIS 5.0.0 was evaluated as a candidate replacement for generic image-based
identity matching. The experiment used 12 trusted enrollment touches spanning
four fingers and seven development touches from two unenrolled pinkies. All
biometric inputs and detailed results remain private under `.egis-lab`.

The bounded matrix covered three compositor strategies, five image scales, both
polarities, and MINDTCT with and without low-contrast enhancement. The winning
configuration used median compositing, 2x scaling, normal polarity, and no
additional MINDTCT enhancement.

It did not pass the integration gate:

- extraction succeeded for 17 of 19 touches;
- only 7 of 11 extractable genuine probes selected the correct identity;
- the minimum genuine score was 0 and maximum impostor score was 21;
- minimum correct-identity margin was 1;
- p95 extraction was 97.9 ms and p95 BOZORTH3 comparison was 3.8 ms.

The result establishes that subprocess overhead is acceptable but NBIS minutiae
from these small stitched EH575 swipes do not provide reliable identity
separation. NBIS must not become an authentication authority. The reusable
outcomes are the engine-independent `TouchStitcher`, the external engine
contract, and the private replay gate. The next candidate must be an established
matcher designed for small-area or partial fingerprints; SIFT remains motion
estimation only and must not be restored as new identity evidence.
