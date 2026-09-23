#!/usr/bin/python3
"""Create a self-contained rescue root from local installed binaries."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile

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


def enroll():
    info = subprocess.check_output(["udevadm", "info", "--query=property", "--name=/dev/sda"], text=True)
    props = dict(line.split("=", 1) for line in info.splitlines() if "=" in line)
    # Refuse silently enrolling a different disk when re-running the demo build.
    if props.get("ID_USB_SERIAL_SHORT") != "ZTE51T0AL262251108":
        raise RuntimeError("Expected disk is not /dev/sda. Review enrollment before building.")
    lvs = {}
    for node in Path("/sys/class/block").glob("dm-*"):
        name = (node / "dm/name").read_text().strip()
        if name in ("vgportable-ubuntu", "vgportable-shared"):
            uuid = (node / "dm/uuid").read_text().strip()
            lvs[name.split("-", 1)[1]] = {"dm_uuid": uuid}
    if set(lvs) != {"ubuntu", "shared"}:
        raise RuntimeError("Expected active LVs not found.")
    partuuid = subprocess.check_output(["lsblk", "-dn", "-o", "PARTUUID", "/dev/sda3"], text=True).strip()
    pvlinks = [p.name.removeprefix("lvm-pv-uuid-") for p in Path("/dev/disk/by-id").glob("lvm-pv-uuid-*")
               if p.resolve() == Path("/dev/sda3")]
    if len(pvlinks) != 1:
        raise RuntimeError("Cannot enroll PV UUID.")
    return {"vid": "21c4", "pid": "00c0", "usb_serial": props["ID_USB_SERIAL_SHORT"],
            "sectors": int(Path("/sys/class/block/sda/size").read_text()),
            "partition_number": 3, "partuuid": partuuid, "pv_uuid": pvlinks[0],
            "vg_name": "vgportable", "vg_uuid": lvs["ubuntu"]["dm_uuid"][4:36], "lvs": lvs}


def main():
    if ROOT.exists():
        shutil.rmtree(ROOT)
    ROOT.mkdir(parents=True)
    with_libs("/usr/bin/busybox", "/bin/busybox")
    applets = subprocess.check_output(["busybox", "--list"], text=True).splitlines()
    for name in applets:
        if name != "busybox":
            (ROOT / "bin" / name).symlink_to("busybox")
    for name in ["lvm", "dmsetup", "e2fsck", "blkid", "blockdev"]:
        with_libs("/usr/sbin/" + name, "/sbin/" + name)
    for name in ["lsblk", "findmnt", "python3.12"]:
        with_libs("/usr/bin/" + name)
    (ROOT / "usr/bin/python3").symlink_to("python3.12")
    stdlib = Path("/usr/lib/python3.12")
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
        path = Path("/lib/x86_64-linux-gnu") / name
        if path.exists():
            with_libs(path)
    copy_file("/usr/share/terminfo/l/linux")
    for path in ["dev", "proc", "sys", "root", "run/lock/lvm", "run/lvm", "tmp", "var/log", "mnt", "etc/lvm/backup"]:
        (ROOT / path).mkdir(parents=True, exist_ok=True)
    (ROOT / "tmp").chmod(0o1777)
    copy_file(BASE / "src/rescue.py", "/sbin/rescue")
    copy_file(BASE / "src/supervisor.sh", "/sbin/rescue-supervisor")
    copy_file(BASE / "src/session.sh", "/bin/rescue-session")
    copy_file(BASE / "src/kernel_log.py", "/sbin/rescue-kernel-log")
    for p in ["sbin/rescue", "sbin/rescue-supervisor", "bin/rescue-session"]:
        (ROOT / p).chmod(0o755)
    copy_file(BASE / "src/lvm.conf", "/etc/lvm/lvm.conf")
    write("/etc/rescue/identity.json", json.dumps(enroll(), indent=2) + "\n")
    write("/etc/passwd", "root:x:0:0:Disabled root:/root:/bin/sh\nrescue:x:0:0:RAM rescue:/root:/bin/rescue-session\n")
    write("/etc/group", "root:x:0:\n")
    write("/etc/shadow", "root:!:20000:0:99999:7:::\nrescue:!:20000:0:99999:7:::\n", 0o600)
    write("/etc/nsswitch.conf", "passwd: files\ngroup: files\nshadow: files\n")
    write("/etc/securetty", "tty9\ntty10\n")
    write("/etc/shells", "/bin/sh\n/bin/rescue-session\n")
    write("/etc/fstab", "# Deliberately empty: no automatic disk mounts.\n")
    write("/etc/issue", "\nRAM RESCUE DEMO | user: rescue | separate rescue password\nVT9 / VT10; disk-independent tools; shared host kernel.\n\n")
    write("/etc/motd", "\nRAM rescue root shell. Start with: rescue status\nThen: rescue verify; rescue refresh ubuntu (MANUAL, not fsck).\nUse rescue help. Exit to lock this VT. Alt+F9/F10 switches rescue terminals.\nNEVER repair a mounted filesystem. Host root remains mounted.\n\n")
    write("/var/log/lastlog", "")
    write("/var/log/wtmp", "")
    write("/run/utmp", "")
    archive = BASE / "rescue-root.tar.gz"
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
    (BASE / "rescue-root.sha256").write_text(digest + "  rescue-root.tar.gz\n")
    size = sum(p.stat().st_size for p in ROOT.rglob("*") if p.is_file() and not p.is_symlink())
    manifest = {"uncompressed_file_bytes": size, "archive_bytes": archive.stat().st_size,
                "sha256": digest, "kernel_built_on": os.uname().release,
                "identity": enroll(), "runtime_tmpfs_limit_mib": 256, "slice_memory_limit_mib": 768}
    (BASE / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({k: v for k, v in manifest.items() if k != "identity"}, indent=2))


if __name__ == "__main__":
    main()
