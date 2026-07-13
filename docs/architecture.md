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
| `egis_driver/image_features.py` | Frame-to-feature conversion using injected frame geometry | USB discovery, D-Bus |
| `egis_driver/fingerprint_matcher.py` | Template indexing, matching metrics, calibrated decision | Sensor commands, service lifecycle |
| `egis_driver/services.py` | Capture state machine and suspend/resume behavior | PolicyKit and manager ownership |
| `openfprintd/` | Standard fprint D-Bus surface, client ownership, authorization | EH575 protocol and image processing |
| `egis_driver/evaluation.py` | Offline replay and acceptance gates | Live installation |
| `egis_driver/compatibility.py` | Sanitized environment and compatibility reports | Raw biometric export |
| `packaging/`, `install-stable.sh` | Exact deployment layout and rollback | Runtime behavior |

`SensorBackend` is the boundary consumed by the service layer. `DeviceProfile`
and `FrameSpec` are immutable hardware inputs. `RuntimePaths` isolates persistent
state paths so tests and offline evaluation never need the live `/var/lib` tree.

## Security Boundaries

- The manager authorizes registration before accepting a backend.
- A claimed device is bound to its D-Bus sender; another client cannot release it.
- Matcher lookup is restricted to the claimed username before nearest-neighbor
  selection.
- Verification fails closed without thresholds validated for the current matcher.
- Suspend and idle transitions do not emit terminal authentication failures.
- Raw frames, templates, calibration samples, usernames, USB serials, and local
  paths are private data and cannot enter a public report or tracked file.

## Compatibility Policy

The only currently supported hardware identity is USB `1c7a:0575` with vendor
interface `ff/ff/00`, bulk OUT `01`, bulk IN `82`, and at least 512-byte endpoint
packets. Revision `1072` is known. A new revision with matching descriptors is
allowed with a warning so it can be tested; descriptor or endpoint mismatches
fail closed.

Template and matcher schemas are separate compatibility contracts. A matcher
change does not require a template migration unless `TEMPLATE_SCHEMA_VERSION`
changes. Thresholds are tied to `MATCHER_VERSION` and must be regenerated when
that version changes.
