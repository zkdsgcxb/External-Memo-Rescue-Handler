# Third-party components

Project-authored code uses 0BSD. This does not relicense the operating-system tools and libraries included in a RAM runtime.

- nlohmann/json is vendored under MIT; its notice is in `guard/native/vendor/LICENSE.nlohmann-json`.
- Ubuntu packages supply Python, Bash, BusyBox, LVM/device-mapper, filesystem tools, libc and other libraries. Their upstream/Debian copyright files and exact package/source versions accompany binary distributions.
- OpenSSL and the C++ runtime retain their original licensing terms.

A release binary must be accompanied by a complete inventory, the applicable copyright/license texts and corresponding source distribution materials for the actual bundled versions. A package name, generic upstream link or checksum alone is not a corresponding-source delivery.

The release preparation script records the source package, version, archive URLs and SHA-256 values. Binary publication is gated on completing those materials. Kernel reference metadata is distinct from redistributing a kernel binary.
