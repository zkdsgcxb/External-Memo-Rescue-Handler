# Changelog

## v0.0.1-beta

First public pre-release with explicit USB/ext4 data-device enrollment, mapping creation, activation, persistent disable, reboot handling and guarded removal. Includes the existing C++17 recovery controller, RAM runtime, read-only discovery and diagnostics.

Installation is inert. Candidate-only acceptance cannot activate protection. Initial queue enablement occurs inside the native ownership fence after transaction evidence is established. Active or incomplete protection blocks package removal; upgrades do not restart it.

See the release evidence and [support matrix](docs/SUPPORT.md) for actual qualified combinations. Root/LVM protection and rescue terminals remain advanced experimental features. Physical-device acceptance is not claimed for this release.
