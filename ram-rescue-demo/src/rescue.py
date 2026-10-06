#!/usr/bin/python3
"""Read-only storage diagnostics for the authenticated RAM rescue terminal."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time


class Refuse(RuntimeError):
    pass


def read(path):
    return Path(path).read_text().strip()


def command(args, timeout=12, *, pass_fds=()):
    # A kernel task in uninterruptible sleep can outlive this timeout.
    # The second rescue terminal remains independent of this helper.
    try:
        result = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout, pass_fds=pass_fds,
                                env={"PATH": "/bin:/sbin:/usr/bin:/usr/sbin",
                                     "LC_ALL": "C", "LVM_SYSTEM_DIR": "/etc/lvm"})
    except subprocess.TimeoutExpired as exc:
        raise Refuse("Command timed out. Use the other rescue VT if necessary.") from exc
    if result.returncode:
        raise Refuse("Command failed: " + " ".join(args) + "\n" + result.stderr[-3000:])
    return result.stdout


def rows(output, key):
    return [row for report in json.loads(output)["report"] for row in report.get(key, [])]


class RescueDiagnostics:
    def __init__(self, config, sysroot=Path("/sys"), devroot=Path("/dev"), runner=command):
        self.c = config
        self.sys = Path(sysroot)
        self.dev = Path(devroot)
        self.run = runner

    def candidates(self):
        found = []
        for block in (self.sys / "class/block").iterdir():
            if (block / "partition").exists():
                continue
            for parent in block.resolve().parents:
                if not (parent / "idVendor").exists():
                    continue
                try:
                    match = (read(parent / "idVendor").lower() == self.c["vid"] and
                             read(parent / "idProduct").lower() == self.c["pid"] and
                             read(parent / "serial") == self.c["usb_serial"])
                except OSError:
                    match = False
                if match:
                    found.append(block)
                break
        return found

    def candidate_node(self):
        """Resolve the unique enrolled partition using sysfs only."""
        disks = self.candidates()
        if len(disks) != 1:
            raise Refuse(f"Expected ONE matching USB disk; found {len(disks)}. No changes made.")
        disk = disks[0]
        if int(read(disk / "size")) != self.c["sectors"]:
            raise Refuse("Capacity differs. No changes made.")
        parts = [p for p in disk.iterdir() if (p / "partition").is_file()
                 and read(p / "partition") == str(self.c["partition_number"])]
        if len(parts) != 1:
            raise Refuse("Expected partition not found uniquely.")
        node = str(self.dev / parts[0].name)
        return node

    def verify(self):
        node = self.candidate_node()
        props = dict(line.split("=", 1) for line in
                     self.run(["/sbin/blkid", "-p", "-o", "export", node]).splitlines() if "=" in line)
        expected = {"TYPE": "LVM2_member", "UUID": self.c["pv_uuid"],
                    "PART_ENTRY_UUID": self.c["partuuid"]}
        for key, value in expected.items():
            if props.get(key) != value:
                raise Refuse(f"{key} does not match the enrolled disk. No changes made.")
        pvs = rows(self.run(["/sbin/lvm", "pvs", "--readonly", "--devices", node,
                             "--reportformat", "json", "-o", "pv_uuid,vg_uuid,vg_name"]), "pv")
        if len(pvs) != 1:
            raise Refuse("Unexpected PV count.")
        pv = pvs[0]
        if (pv["pv_uuid"].strip() != self.c["pv_uuid"] or
                pv["vg_uuid"].replace("-", "").strip() != self.c["vg_uuid"] or
                pv["vg_name"].strip() != self.c["vg_name"]):
            raise Refuse("LVM identity differs. No changes made.")
        return node

    def mapping(self, target):
        wanted = self.c["lvs"][target]["dm_uuid"]
        matches = [p for p in (self.sys / "class/block").glob("dm-*")
                   if (p / "dm/uuid").exists() and read(p / "dm/uuid") == wanted]
        if len(matches) != 1:
            raise Refuse("Expected one active LV matching the enrolled DM UUID.")
        return matches[0]

    def status(self):
        print("RAM RESCUE DEMO - read-only status (no disk scan)")
        print("Enrolled USB serial:", self.c["usb_serial"])
        print("Matching disks:", [p.name for p in self.candidates()])
        for name in self.c["lvs"]:
            try:
                m = self.mapping(name)
                print(name, m.name, "depends on", [p.name for p in (m / "slaves").iterdir()])
            except Refuse as exc:
                print(name, str(exc))
        print("\nHost filesystem mounts (kernel mount table):")
        try:
            for line in read("/proc/1/mountinfo").splitlines():
                if " - ext4 " in line or "ram-rescue" in line:
                    print(line)
        except OSError as exc:
            print(exc)
        print("\nCommands: rescue status | rescue verify | rescue log | rescue help")


HELP = """RAM rescue uses a separate root in RAM. This shell is root after authentication.
Alt+F9 and Alt+F10 are independent rescue terminals (Ctrl+Alt+Fn from desktop).
status: inspect sysfs only; verify: probe ONLY the enrolled disk and PV.
log: show kernel messages; help: show this guide. Neither requires enrollment.
These commands only inspect storage; automatic recovery belongs to the Guard.
No mapping changes, fsck, remount, USB reset, poweroff or reboot occurs.
Do NOT run e2fsck repair on a mounted filesystem, including the host root.
The host root can still be mounted even though this shell has a different root.
Use /proc/1/mountinfo to check host mounts; do not mount its LV a second time.
Logs: /var/log/rescue-actions.log and /var/log/kernel-live.log (RAM, lost at reboot).
Commands blocked in kernel D-state may outlive a timeout. Try the other VT.
Exit the shell to lock this terminal. Both VTs use the separate rescue password.
This cannot recover a deadlocked/panicked kernel or undo failed filesystem writes.
"""


def main(argv=None):
    parser = argparse.ArgumentParser(description=HELP)
    parser.add_argument("action", choices=["status", "verify", "log", "help"], nargs="?", default="status")
    args = parser.parse_args(argv)
    try:
        if args.action == "help":
            print(HELP)
        elif args.action == "log":
            os.execv("/bin/busybox", ["busybox", "dmesg"])
        else:
            diagnostics = RescueDiagnostics(json.loads(read("/etc/rescue/identity.json")))
            if args.action == "status":
                diagnostics.status()
            else:
                print("Verified:", diagnostics.verify())
    except (Refuse, OSError, ValueError, KeyError) as exc:
        print("STOP:", exc, file=sys.stderr)
        return 1
    finally:
        try:
            with open("/var/log/rescue-actions.log", "a") as log:
                log.write(f"{time.time():.0f} action={args.action}\n")
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
