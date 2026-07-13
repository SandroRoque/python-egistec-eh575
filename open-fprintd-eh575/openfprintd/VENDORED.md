# Vendored open-fprintd

This directory is derived from [uunicorn/open-fprintd](https://github.com/uunicorn/open-fprintd),
which is licensed under GPL-2.0. It is kept in this repository because the EH575
backend, manager security policy, and suspend lifecycle must currently be
released and tested atomically.

## Provenance

- Upstream project: `https://github.com/uunicorn/open-fprintd`
- Local import lineage: repository commit `e172b6a`
- Exact upstream base: not recorded by the original import
- Local tree identity: recorded in every v0.4 release manifest

Because the original import did not retain an upstream commit identifier, this
file does not claim a guessed base. A future re-vendor must start from an exact
upstream commit and retain that commit in this document.

## Local behavior

The vendored manager adds or hardens:

- D-Bus sender-to-username resolution and cross-user denial
- polkit authorization for enrollment, verification, and backend registration
- bounded asynchronous authorization work
- claim ownership enforcement
- target disappearance handling
- logind suspend/resume forwarding
- preservation of active verification claims across suspend

These behaviors are covered by repository tests and are part of the EH575
runtime contract. The directory should be extracted to a separate fork only
after the changes are accepted upstream or another backend needs to consume the
manager independently.
