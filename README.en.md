# External-Memo-Rescue-Handler

**USB storage reconnection for Linux — v0.0.1-beta**

[中文](README.md) · [Usage](docs/USAGE.md) · [Support](docs/SUPPORT.md) · [Security](SECURITY.md)

A C++17 controller maintains a stable DM multipath device, verifies the original USB medium after re-enumeration, and restores its path so queued I/O can continue. I/O can block during recovery. Already failed I/O, filesystem damage, permanent driver stalls and power-loss cache loss cannot be undone.

The public beta provides explicit enrollment, initial mapping, activation, safe stop, persistent disable, boot activation, removal, diagnostics and Debian packaging. Installing the package and saving configuration are inert. Candidate-only qualification never grants activation.

The supported product scope is Ubuntu 24.04, x86_64, ext4 on a single USB data partition, on exactly qualified kernel/software combinations. Unknown combinations fail closed. Existing root/LVM protection and RAM rescue terminals remain advanced experimental features.

See the Chinese [installation guide](docs/USAGE.md) for the complete command sequence and [support matrix](docs/SUPPORT.md) for tested combinations. The CLI has English command names and structured JSON output. New ordinary users should use `rescue-guard-admin device`, not historical manager installation examples.

Build and test without installing host services:

```sh
mkdir -p lab/work
python3 -B guard/native/build_runtime.py --output lab/work/native
python3 -B -m unittest discover -s lab/tests
python3 -B -m unittest discover -s ram-rescue-demo/tests
```

Dependencies: Python 3.12, a C++17 compiler, binutils, OpenSSL headers. See [native build instructions](guard/native/README.md) and [isolated QEMU reproduction](lab/README.md). VM evidence is not physical-device validation.

Project code: [0BSD](LICENSE). Bundled dependencies retain their own licenses; see [third-party distribution](THIRD_PARTY.md).
