#!/usr/bin/python3
"""Create a self-contained rescue root from local installed binaries."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import sysconfig
import tarfile

from session_payload import stage_session

BASE = Path(__file__).resolve().parent
ROOT = BASE / "work/rootfs"


def copy_file(source, dest=None):
    source = Path(source)
    target = ROOT / str(dest or source).lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source.resolve(), target)
    target.chmod(target.stat().st_mode & 0o777)  # no setuid/setgid payloads


def with_libs(path, dest=None):
    copy_file(path, dest)
    result = subprocess.run(["ldd", str(path)], text=True, capture_output=True)
    output = result.stdout + result.stderr
    if "not found" in output:
        raise RuntimeError(output)
    if result.returncode and "not a dynamic executable" not in output and "statically linked" not in output:
        raise RuntimeError(output)
    for dep in re.findall(r"(?:=>\s+|^\s*)(/[^\s]+)", output, re.M):
        copy_file(dep)


def write(path, data, mode=0o644):
    target = ROOT / path.lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(data)
    target.chmod(mode)


def normalize_payload(root):
    """Archive ownership is root; writable modes must not depend on host umask."""
    root = Path(root)
    root.chmod(0o755)
    for path in root.rglob('*'):
        if path.is_symlink():
            continue
        if path.is_dir():
            path.chmod(0o1777 if path == root/'tmp' else 0o755)
        else:
            path.chmod(path.stat().st_mode & 0o755)


def dependency_manifest(root):
    """Account every frozen ELF tool/library; this is a cold build operation."""
    sys.path.insert(0, str(BASE.parent / 'guard'))
    try:
        from native_payload import dependency_packages
    finally:
        sys.path.pop(0)
    frozen = {}
    for path in sorted(Path(root).rglob('*')):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open('rb') as stream:
            header = stream.read(18)
            if len(header) < 18 or header[:4] != b'\x7fELF' or header[5] not in (1, 2):
                continue
            # ET_REL build objects (for example Python's config/python.o) do
            # not execute as tools or load as shared libraries at runtime.
            if int.from_bytes(header[16:18], 'little' if header[5] == 1 else 'big') not in (2, 3):
                continue
        if len(frozen) >= 256:
            raise ValueError('Frozen ELF dependency inventory exceeds 256 files')
        frozen['/' + str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {'schema': 1, 'kind': 'base-rescue-tools', 'file_sha256': frozen,
            'dependency_packages': dependency_packages(frozen),
            'scope': 'All frozen ELF tools and shared libraries, including Python extensions; '
                     'Python stdlib source files are covered by package versions, not individual byte comparison.'}


def build(output_dir, identity=None):
    """Build generic tools from trusted installed binaries; never discover disks.

    Enrollment is a separate, explicit read-only operation. An optional identity
    JSON can be embedded for the manual rescue command; it is not inferred from
    the current host or required by standalone data-map aftercare.
    """
    global ROOT
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    ROOT = output_dir / "rootfs"
    if ROOT.exists() or any((output_dir / name).exists() for name in
                           ("rescue-root.tar.gz", "manifest.json")):
        raise ValueError("Use a new output directory; previous artifacts are preserved")
    if identity is not None and (not isinstance(identity, dict) or not identity):
        raise ValueError("identity must be a nonempty explicit JSON object")
    ROOT.mkdir()
    with_libs("/usr/bin/busybox", "/bin/busybox")
    applets = subprocess.check_output(["busybox", "--list"], text=True).splitlines()
    for name in applets:
        if name != "busybox":
            (ROOT / "bin" / name).symlink_to("busybox")
    for name in ["lvm", "dmsetup", "e2fsck", "blkid", "blockdev"]:
        with_libs("/usr/sbin/" + name, "/sbin/" + name)
    for name in ["lsblk", "findmnt"]:
        with_libs("/usr/bin/" + name)
    interpreter = Path(sys.executable).resolve()
    with_libs(interpreter, "/usr/bin/" + interpreter.name)
    (ROOT / "usr/bin/python3").symlink_to(interpreter.name)
    stdlib = Path(sysconfig.get_path("stdlib"))
    for base, dirs, files in os.walk(stdlib):
        dirs[:] = [d for d in dirs if d not in {"__pycache__", "test", "tests", "idlelib", "tkinter", "ensurepip"}]
        for name in files:
            if name.endswith((".pyc", ".pyo")):
                continue
            path = Path(base) / name
            if path.is_file():
                if ".so" in name:
                    with_libs(path)
                else:
                    copy_file(path)
    for name in ["libnss_files.so.2", "libnss_compat.so.2"]:
        path = Path("/lib") / sysconfig.get_config_var("MULTIARCH") / name
        if path.exists():
            with_libs(path)
    copy_file("/usr/share/terminfo/l/linux")
    for path in ["dev", "proc", "sys", "root", "run/lock/lvm", "run/lvm", "tmp", "var/log", "mnt", "etc/lvm/backup"]:
        (ROOT / path).mkdir(parents=True, exist_ok=True)
    (ROOT / "tmp").chmod(0o1777)
    copy_file(BASE / "src/rescue.py", "/sbin/rescue")
    session = stage_session(ROOT)
    copy_file(BASE / "src/kernel_log.py", "/sbin/rescue-kernel-log")
    for p in ["sbin/rescue", "sbin/rescue-supervisor", "bin/rescue-session"]:
        (ROOT / p).chmod(0o755)
    copy_file(BASE / "src/lvm.conf", "/etc/lvm/lvm.conf")
    if identity is not None:
        write("/etc/rescue/identity.json", json.dumps(identity, indent=2) + "\n", 0o600)
    write("/etc/passwd", "root:x:0:0:Disabled root:/root:/bin/sh\nrescue:x:0:0:RAM rescue:/root:/bin/rescue-session\n")
    write("/etc/group", "root:x:0:\n")
    write("/etc/shadow", "root:!:20000:0:99999:7:::\nrescue:!:20000:0:99999:7:::\n", 0o600)
    write("/etc/nsswitch.conf", "passwd: files\ngroup: files\nshadow: files\n")
    write("/etc/securetty", "tty9\ntty10\n")
    write("/etc/shells", "/bin/sh\n/bin/rescue-session\n")
    write("/etc/fstab", "# Deliberately empty: no automatic disk mounts.\n")
    write("/etc/issue", "\nRAM RESCUE DEMO | user: rescue | separate rescue password\nVT9 / VT10; disk-independent tools; shared host kernel.\n\n")
    write("/var/log/lastlog", "")
    write("/var/log/wtmp", "")
    write("/run/utmp", "")
    dependencies = dependency_manifest(ROOT)
    write("/etc/rescue/base-runtime.json", json.dumps(dependencies, indent=2, sort_keys=True) + "\n")
    normalize_payload(ROOT)
    archive = output_dir / "rescue-root.tar.gz"
    with tarfile.open(archive, "w:gz", compresslevel=3) as tar:
        for path in sorted(ROOT.rglob("*")):
            info = tar.gettarinfo(str(path), arcname=str(path.relative_to(ROOT)))
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            if info.isfile():
                with path.open("rb") as data:
                    tar.addfile(info, data)
            else:
                tar.addfile(info)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output_dir / "rescue-root.sha256").write_text(digest + "  rescue-root.tar.gz\n")
    size = sum(p.stat().st_size for p in ROOT.rglob("*") if p.is_file() and not p.is_symlink())
    manifest = {"uncompressed_file_bytes": size, "archive_bytes": archive.stat().st_size,
                "sha256": digest, "kernel_built_on": os.uname().release,
                "identity": identity, "runtime_tmpfs_limit_mib": 256, "slice_memory_limit_mib": 768,
                "rescue_session": session, "dependencies": dependencies}
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (output_dir / "manifest.json").chmod(0o600 if identity is not None else 0o644)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=BASE,
                        help="New artifact directory; no disk enrollment is performed")
    parser.add_argument("--identity", type=Path, help="Optional explicit identity JSON from read-only enrollment")
    args = parser.parse_args()
    identity = json.loads(args.identity.read_text()) if args.identity else None
    manifest = build(args.output_dir, identity)
    print(json.dumps({k: v for k, v in manifest.items() if k != "identity"}, indent=2))


if __name__ == "__main__":
    main()
