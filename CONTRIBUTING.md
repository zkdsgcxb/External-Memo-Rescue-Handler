# Contributing

Discuss changes to device ownership, activation, boot integration or support policy in an issue before implementation. Keep fixes narrow and identify the behavior, supported topology and failure boundary.

Use Python 3.12 and C++17 on Linux. Build dependencies and commands are in [README](README.md). Run both Python test suites and the native builder. Add behavioral tests for privilege boundaries, refusal paths and lifecycle transitions; do not use real host block devices in tests.

`lab/work/` holds disposable artifacts and is ignored. Preserve failed VM evidence locally. Public reports must distinguish fixtures, actual packages, VM runs and physical-device results. Do not publish private disk identities, credentials, raw host reports or disk images.

A pull request should describe the trigger and resulting behavior, relevant tests, supported combinations and known limitations. Do not claim hardware acceptance from a VM result. Source-only documentation fixes need a link/content check, not new unit tests.

Original contributions are under [0BSD](LICENSE). Preserve third-party copyright notices and licenses. Never introduce vendored binaries without provenance and a distribution plan.
