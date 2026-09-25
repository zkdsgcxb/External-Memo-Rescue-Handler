#!/usr/bin/env python3
"""Test late return after multipathd timeout in disposable Linux 7.0 / 0.15 VM.

This observes stock behavior; it neither implements admission nor runs Guard.
All block-device commands execute inside the VM. The host opens regular files.
"""
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time

from version_probe import ROOT, INIT, Channel, copy_staged_runtime, guest_program


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    if os.geteuid() == 0:
        raise SystemExit("Run unprivileged; only the disposable guest uses root")
    kernel = ROOT / "lab/work/version-study/kernel7/guest/vmlinuz"
    base_initrd = kernel.with_name("initramfs.cpio.gz")
    stage = ROOT / "lab/work/new-multipath/stage"
    folder = ROOT / "lab/work/admission-study" / time.strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True, mode=0o700)
    overlay = folder / "overlay"
    overlay.mkdir()
    spec = importlib.util.spec_from_file_location("payload", ROOT / "ram-rescue-demo/build.py")
    payload = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(payload)
    payload.ROOT = overlay
    for source in ["/usr/bin/udevadm", "/usr/lib/systemd/systemd-udevd",
                   "/usr/lib/udev/scsi_id", "/usr/bin/setpriv",
                   "/lib/x86_64-linux-gnu/libgcc_s.so.1"]:
        payload.with_libs(source)
    copy_staged_runtime(payload, stage)
    guest = guest_program(True)
    anchor = "            elif req['action']=='validate':"
    assert guest.count(anchor) == 1
    guest = guest.replace(anchor, """            elif req['action']=='direct_read':
                import mmap
                block=mmap.mmap(-1,4096)
                readfd=os.open('/dev/mapper/probe',os.O_RDONLY|os.O_DIRECT)
                try:
                    n=os.readv(readfd,[block])
                    ans['direct_read']={'bytes':n,'sha256':hashlib.sha256(block[:n]).hexdigest()}
                finally:
                    os.close(readfd); block.close()
""" + anchor)
    payload.write("/init", INIT, 0o755)
    payload.write("/opt/probe.py", guest)
    payload.write("/etc/udev/rules.d/60-probe.rules",
        'ACTION=="add|change", SUBSYSTEM=="block", ENV{DEVTYPE}=="disk", KERNEL=="sd*", '
        'IMPORT{program}="/usr/lib/udev/scsi_id --export --whitelisted -d $devnode"\n')
    for source in ["/usr/lib/udev/rules.d/55-dm.rules",
                   "/usr/lib/udev/rules.d/60-persistent-storage-dm.rules",
                   "/usr/lib/udev/rules.d/95-dm-notify.rules"]:
        if Path(source).exists():
            payload.copy_file(source)
    payload.write("/etc/multipath.conf", "defaults {\n allow_usb_devices yes\n}\n")
    with subprocess.Popen(["find", ".", "-print0"], cwd=overlay, stdout=subprocess.PIPE) as find:
        archive = subprocess.check_output(
            ["cpio", "--null", "-o", "-H", "newc", "--owner=0:0"], cwd=overlay,
            stdin=find.stdout, stderr=subprocess.DEVNULL)
        find.stdout.close()
        if find.wait():
            raise RuntimeError("find failed")
    initrd = folder / "initramfs.cpio.gz"
    initrd.write_bytes(base_initrd.read_bytes() + gzip.compress(archive, compresslevel=3))
    disk = folder / "disk.raw"
    with disk.open("xb") as stream:
        stream.truncate(1024 ** 3)
    command = ["qemu-system-x86_64", "-machine", "q35", "-accel", "kvm", "-m", "1024",
               "-smp", "2", "-display", "none", "-nodefaults", "-no-reboot", "-nic", "none",
               "-kernel", str(kernel), "-initrd", str(initrd), "-append",
               "console=ttyS0 rdinit=/init panic=-1 probe_deny_rt=1",
               "-serial", "file:" + str(folder / "console.log"),
               "-serial", "unix:" + str(folder / "agent.sock") + ",server=on,wait=off",
               "-qmp", "unix:" + str(folder / "qmp.sock") + ",server=on,wait=off",
               "-device", "qemu-xhci,id=xhci", "-blockdev",
               json.dumps({"driver": "raw", "node-name": "disk",
                           "file": {"driver": "file", "filename": str(disk)}}),
               "-device", "usb-uas,bus=xhci.0,id=stick,serial=UPSTREAM-USB-001,port=1",
               "-device", "scsi-hd,bus=stick.0,id=lun,drive=disk,serial=UPSTREAM-SCSI-001"]
    report = {"scope": "stock 0.15, Linux 7.0, RAM root, whole USB multipath/LVM data disk; no Guard",
              "command": command, "script_sha256": sha256(Path(__file__)),
              "version_probe_sha256": sha256(Path(__file__).with_name("version_probe.py")),
              "baseline_script_sha256": sha256(Path(__file__).with_name("multipathd_probe.py")),
              "kernel_sha256": sha256(kernel), "base_initramfs_sha256": sha256(base_initrd),
              "initramfs_sha256": sha256(initrd), "guest_sha256": hashlib.sha256(guest.encode()).hexdigest(),
              "daemon_sha256": sha256(stage / "usr/sbin/multipathd"), "oracle_passed": False}
    print(folder, flush=True)
    with (folder / "qemu.log").open("w") as log, (folder / "qmp.jsonl").open("w") as qlog, \
            (folder / "agent.jsonl").open("w") as alog:
        vm = subprocess.Popen(command, stdout=log, stderr=log)
        channels = []
        try:
            deadline = time.monotonic() + 60
            while not (folder / "qmp.sock").exists():
                if vm.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("QEMU startup timeout")
                time.sleep(.1)
            qmp = Channel(folder / "qmp.sock", qlog, qmp=True)
            channels.append(qmp)
            while "UPSTREAM_READY" not in (folder / "console.log").read_text(errors="replace"):
                if vm.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("guest startup timeout")
                time.sleep(.1)
            agent = Channel(folder / "agent.sock", alog)
            channels.append(agent)
            report["before"] = agent.call("snapshot")["snapshot"]
            assert report["before"]["kernel"] == "7.0.0-34-generic"
            assert "v0.15.0" in report["before"]["version_output"]
            assert report["before"]["daemon_sha256"] == report["daemon_sha256"]
            report["initial_direct_read"] = agent.call("direct_read")
            report["delete_requested"] = time.monotonic()
            qmp.call("device_del", id="stick")
            deadline = time.monotonic() + 10
            while not any(e["host_time"] >= report["delete_requested"]
                          and e["message"].get("event") == "DEVICE_DELETED"
                          and e["message"].get("data", {}).get("device") == "stick" for e in qmp.events):
                if time.monotonic() > deadline:
                    raise RuntimeError("USB removal timeout")
                time.sleep(.01)
            report["deleted"] = time.monotonic()
            # no_path_retry=8, polling_interval=1; wait beyond the measured timeout.
            time.sleep(15)
            report["absent"] = agent.call("snapshot")["snapshot"]
            qmp.call("device_add", driver="usb-uas", bus="xhci.0", id="stick",
                     serial="UPSTREAM-USB-001", port="1", attached=False)
            qmp.call("device_add", driver="scsi-hd", bus="stick.0", id="lun",
                     drive="disk", serial="UPSTREAM-SCSI-001")
            qmp.call("qom-set", path="/machine/peripheral/stick", property="attached", value=True)
            report["reattached"] = time.monotonic()
            time.sleep(5)
            report["returned"] = agent.call("snapshot")["snapshot"]
            report["returned_direct_read"] = agent.call("direct_read")
            log_text = report["absent"]["multipath_log"]
            before_read = report["initial_direct_read"].get("direct_read", {})
            after_read = report["returned_direct_read"].get("direct_read", {})
            report["oracle_passed"] = bool(
                "Disable queueing" in log_text and report["absent"]["errors"]
                and before_read.get("bytes") == 4096 and after_read == before_read)
            report["oracle_meaning"] = (
                "Observed queue timeout and application errors, then same map successfully served "
                "O_DIRECT read after late return. This passes the counterexample oracle, "
                "not application-continuity or strict terminal-state requirements.")
        except Exception as exc:
            report["error"] = repr(exc)
        finally:
            for channel in channels:
                channel.close()
            vm.terminate()
            try:
                vm.wait(timeout=5)
            except subprocess.TimeoutExpired:
                vm.kill()
                vm.wait()
            (folder / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"folder": str(folder), "oracle_passed": report["oracle_passed"],
                      "error": report.get("error")}), flush=True)
    return 0 if report["oracle_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
