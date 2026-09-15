# Architecture and Repository Boundaries

This repository is intentionally a monorepo for the EH575 authentication stack.
The hardware protocol, matcher, D-Bus bridge, manager policy, packaging, and
acceptance tests change together and must be released as one tested unit.

Splitting it now would replace local interfaces with cross-repository version
contracts without creating independent consumers. A split becomes justified when
either the matcher has a second hardware backend or the manager has a second
independently released device backend.

## Ownership Map

| Area | Responsibility | Must not own |
|------|----------------|--------------|
| `egis_driver/device_profile.py` | Immutable USB identity, descriptors, endpoints, frame geometry, and EH575 commands | USB I/O, matching, D-Bus |
| `egis_driver/egis_driver.py` | Sensor discovery, descriptor validation, USB lifecycle, capture | Template storage, authentication policy |
| `egis_driver/sensor_controller.py` | Single backend owner, bounded commands, cancelable sessions, suspend/release ordering | Image features, authentication policy, D-Bus |
| `egis_matcher/` | Frame features, template construction, identity metrics, decisions, and confirmation policy | Filesystem paths, USB, D-Bus, service lifecycle |
| `egis_driver/fingerprint_matcher.py` | Persistence-backed matcher adapter and template indexing | Sensor commands, service lifecycle |
| `egis_driver/capture.py` | Touch detection and complete frame-window acquisition with measurable outcomes | Identity decisions and D-Bus status |
| `egis_driver/streaming.py` | Ordered continuous capture stream and isolated matcher worker process | Fingerprint features, enrollment policy, D-Bus |
| `egis_driver/sequence_recording.py` | Private loss-detecting sequence recording and replay | Matching decisions and public artifacts |
| `egis_matcher/sequence.py` | Frame registration, disconnected-component tracking, and experimental mosaics | Sensor access and persistent paths |
| `egis_matcher/atlas.py` | Experimental sequence enrollment and incremental spatial match evidence | USB, files, production authentication decisions |
| `egis_driver/atlas_storage.py`, `sequence_evaluation.py` | Private atlas serialization, sequence provenance, and replay reports | Production template loading and promotion |
| `egis_driver/services.py` | Authentication-session state and suspend/resume behavior | Matching calculations and USB commands |
| `openfprintd/` | Standard fprint D-Bus surface, client ownership, authorization | EH575 protocol and image processing |
| `egis_driver/evaluation.py` | Offline replay and acceptance gates | Live installation |
| `egis_driver/compatibility.py` | Sanitized environment and compatibility reports | Raw biometric export |
| `packaging/`, `install-stable.sh` | Exact deployment layout and rollback | Runtime behavior |

`SensorController` owns the live `SensorBackend`. Service operations receive
`SensorSession` objects with the acquisition interface and a cancellation token.
`DeviceProfile` and `FrameSpec` are immutable hardware inputs. `RuntimePaths`
isolates persistent state paths so tests and offline evaluation never need the
live `/var/lib` tree.

`MatcherCore` is the independent algorithm boundary. Callers supply frame
geometry, frames, templates, indexes, and thresholds; importing it does not load
PyUSB, D-Bus, GLib, systemd integration, or runtime paths. `MatchDecision`
distinguishes accepted, rejected, unscorable, and uncalibrated attempts.

`CaptureCoordinator` turns sensor reads into `captured`, `incomplete`,
`io_error`, `device_unavailable`, or `canceled` outcomes. Capture outcomes and
matcher decisions have separate aggregate counters, available through
`EgisService.diagnostics_snapshot()`.

Live verification no longer alternates sensor reads with matching. A dedicated
`CapturePump` keeps reading for the duration of a contact and publishes immutable,
ordered messages into a bounded queue. If matching is slower than capture, the
oldest queued frames are discarded and the next message carries the exact drop
count; confirmation evidence is then reset. Matching runs in a persistent spawned
process, has a readiness handshake and request timeout, and is restarted on a
crash or timeout. Generation and capture-epoch fields prevent late work from a
canceled session from becoming an authentication result.

Loss is attached to the first surviving frame after a queue gap, before that
frame contributes evidence. USB errors clear the pending window and confirmation;
repeated errors end capture as `device_unavailable`, not as a finger release.
Matcher startup, protocol, and transport failures return unscorable decisions.
Timeout recovery discards the failed worker and starts its replacement on the
next request. Enrollment deletion cancels verification and invalidates the
worker's cached templates before returning.

The live service constructs and accesses its backend exclusively on the
`egis-sensor-owner` thread. Readiness, presence polling, enrollment acquisition,
continuous verification, reconnect, and physical release all pass through this
owner. The capture pump produces frames independently of the matcher while its
individual reads are executed by the owner. Standalone maintenance tools that
stop the bridge still own their backend within their own process.

Each session belongs to one suspend/resume epoch and one cancellation token.
Cancellation abandons queued commands and discards late results from dispatched
calls. Suspend invalidates sessions immediately and queues physical release after
the current USB call; resume commands must follow that release. Stale producers
therefore cannot reconnect the sleeping device. Canceled recovery cannot publish
readiness or restart a paused verification in a newer suspend cycle.

The command queue is bounded (32 pending commands; repeated pending releases are
coalesced), and callers stop waiting after 10 seconds. Neither a caller timeout
nor scan replacement starts another sensor owner. A native USB call that never
returns still blocks subsequent physical work, including release, until the
process is restarted; cancellation keeps callers responsive but cannot forcibly
interrupt USB code. The bridge closes the controller and matcher on normal exit,
SIGINT, and SIGTERM. Shutdown leaves physical cleanup on the owner thread.

Ordered sequences retain every sensor observation and its timing. The independent
`TouchTracker` estimates validated frame relationships, keeps disconnected regions
as separate components, and can render experimental mosaics. A discontinuity
permanently excludes older frames from subsequent registration references.

`FeatureAtlas` remains an offline spatial experiment. Production enrollment no
longer creates one as a candidate identity representation: its largest-component
model discarded most of the validated Windows enrollment, and SIFT/ridge evidence
is not permitted to become authentication authority.

`StreamingAtlasMatcher.observe` extracts features once, tracks the live frame, and
aligns it against atlas keyframes. A frame adds evidence only when it supplies new
supported cells and moves sufficiently from the last admitted view. Frame-to-atlas
poses must agree with the live registration chain. Gaps, weak/unmatched frames,
inconsistent poses, and component changes reset evidence. Repeated views do not
increase the admitted-frame count.

Presentation galleries retain separate enrollment touches and disconnected
motion components. SIFT registration and image similarity select complementary
views and reject duplicates, while identity scores come only from an external
fingerprint engine. Gallery files contain checksummed opaque feature records and
metadata, never raw pixels. Engine configuration and schema mismatches fail
closed; re-enrollment replaces migration.

Production enrollment requires ten independent presentations. After each accepted
stage, progress is reported to the client and capture remains blocked until the
sensor observes four consecutive no-contact frames spanning at least 300 ms.
Continuous contact can therefore contribute only one presentation, and every
presentation remains a separate input to template and gallery construction.

The live gallery worker consumes the ordered stream one frame at a time and
reports per-identity scores, margins, extraction failures, and timing as shadow
telemetry. Queue loss, capture discontinuities, and worker restarts clear its
trajectory. `EGIS_MATCH_MODE` deliberately exposes only `window` and `shadow`;
an uncalibrated gallery cannot emit a D-Bus match.

`TouchStitcher` is the engine-independent swipe-to-image boundary. It uses SIFT
only to estimate relationships between ordered frames, excludes the physical
sensor edge, never joins disconnected coordinate systems, and emits a grayscale
composite plus its validity mask. It contains no identity or acceptance logic.

External fingerprint engines consume immutable masked grayscale images through
an extract/compare contract and return opaque records. A coherent component
stitch is one possible representation, not the enrollment container. The NBIS
candidate executes `cwsq`, `mindtct`, and
`bozorth3` in owner-private temporary directories with fixed arguments,
timeouts, bounded parsing, and fail-closed results. Production authority is
unchanged until a candidate passes private replay and fresh live holdout gates.

The SourceAFIS candidate uses the same boundary through a long-lived, pinned
Java worker. Python sends grayscale images and receives opaque templates
and numeric scores; it implements no minutiae extraction or identity scoring.
Evaluation includes complete-touch leave-one-touch-out comparisons and
progressive swipe prefixes. Candidate engines and their templates remain
strictly outside production until both replay modes pass. Presentation galleries
pin SourceAFIS scale 3 and remain optional; a missing worker disables only shadow
evidence.

## Security Boundaries

- The manager authorizes registration before accepting a backend.
- A claimed device is bound to its D-Bus sender; another client cannot release it.
- Matcher lookup is restricted to the claimed username before nearest-neighbor
  selection.
- Verification fails closed without thresholds validated for the current matcher.
- Suspend and idle transitions do not emit terminal authentication failures.
- Raw frames, templates, calibration samples, usernames, USB serials, and local
  paths are private data and cannot enter a public report or tracked file.
- Sequence directories and analysis images are created below `.egis-lab` with
  owner-only permissions. Queue loss marks a recording incomplete rather than
  silently producing misleading research evidence.

## Compatibility Policy

The only currently supported hardware identity is USB `1c7a:0575` with vendor
interface `ff/ff/00`, bulk OUT `01`, bulk IN `82`, and at least 512-byte endpoint
packets. Revision `1072` is known. A new revision with matching descriptors is
allowed with a warning so it can be tested; descriptor or endpoint mismatches
fail closed.

Template and matcher schemas are separate compatibility contracts, but backward
template migration is explicitly not required. A representation change may bump
`TEMPLATE_SCHEMA_VERSION`, invalidate old enrollments, and require re-enrollment.
Thresholds are tied to `MATCHER_VERSION` and must be regenerated whenever matcher
behavior changes.

Production verification uses three frames per attempt and requires two
consecutive accepted attempts. Calibration, live verification, and promotable
offline reports use the same confirmation state machine. Experimental reports
may use another policy, but cannot be packaged for promotion.
