# Security

This project runs privileged storage operations. The public beta is experimental and accepts only qualified combinations. RAM/chroot shares the host kernel and is not a security sandbox. USB identifiers prevent accidental misidentification; they are not authentication against a malicious device.

Do not post credentials, disk serials, filesystem UUIDs, raw host logs or exploit details in a public issue. Use the repository's private vulnerability reporting channel when available. If private reporting is unavailable, open a minimal issue requesting a private contact without disclosing the vulnerability.

Reports should identify the release/commit, relevant component, affected privilege boundary and a disposable-VM reproduction when possible. Use the built-in redacted export for public diagnostics and inspect it before sharing.

No response-time guarantee or production-support SLA is offered. Security fixes must include the affected version and a reproducer or regression check. Supported combinations are listed in [SUPPORT](docs/SUPPORT.md); old research snapshots are not maintained releases.

The package installs without activation. Removing it is refused while protected mappings, configurations, incomplete state or a RAM runtime remain. Upgrades must not start or stop protection automatically.
