# USB EFI 重接后的挂载

EFI 第一分区仍使用 Ubuntu 原来的普通 FAT 挂载。定向 udev 规则为登记设备生成 `/dev/ram-rescue-efi`；systemd 原生 `ram-rescue-efi.path` 监听该节点，通过已有的 `boot-efi.mount` 恢复挂载。systemd 继续负责设备依赖、文件系统检查和挂载。不增加守护进程、轮询或 Guard 的恢复职责，也不把 EFI 接到根卷的 DM 排队层。

规则只匹配登记的 FAT UUID、GPT PARTUUID 和真正的 USB serial。使用 `ID_USB_SERIAL_SHORT`，不使用本机 USB 桥报告的通用 SCSI 序号。配置留在宿主 `/etc/udev/rules.d/90-ram-rescue-efi.rules`；设备身份及备份不公开提交。

当前已经挂载的 EFI 不重挂；设备消失时仍按 Ubuntu 原有依赖卸载。如果设备在旧卸载尚未结束时就已返回，`.path` 会在 mount 状态变化后重新检查节点，补上挂载。这避免了单独使用 `SYSTEMD_WANTS=boot-efi.mount` 时旧 stop job 与新 start 请求冲突而丢失恢复机会的问题。只保留这一套触发方式。

## 安装与移除

安装入口 [`efi_mount.py`](efi_mount.py) 要求：已验证整分区备份、未挂载时 FAT 只读检查通过、原挂载恢复成功，且测试报告确实通过当前源码的 VM 验收。它再次核对 EFI 是当前根盘保护映射所在 USB 磁盘的第一分区，避免登记资料和实际设备不一致。

```bash
# 根据本机私有检查记录生成规则预览，不改系统。
python3 guard/efi_mount.py --preparation lab/work/efi-recovery-20261001/preparation.json

# 通过本机认证安装；VM 报告路径需使用实际通过的结果。
pkexec python3 guard/efi_mount.py --install \
  --preparation "$PWD/lab/work/efi-recovery-20261001/preparation.json" \
  --vm-report "$PWD/lab/work/<efi-vm-run>/report.json"

# 撤除新增规则和 path unit；普通 EFI 挂载及原 fstab 保留。
pkexec python3 guard/efi_mount.py --remove
```

安装记录位于 `/var/lib/ram-rescue-efi`。除规则和 path unit 外，只为登记 EFI 的 `systemd-fsck@` 实例添加 `RemainAfterExit=no`：避免快速重接时沿用上次“已检查”的状态，每次新挂载事务都请求原生检查。fsck 命令、修复策略和其他文件系统的检查单元不变；其原生 `-M` 会跳过已挂载设备。

安装锁定 dpkg，通过一次正常卸载、停止旧 fsck 缓存状态、原生检查并挂回的流程，使新行为当场生效。它不改 fstab、GRUB、内置 rEFInd、Guard 或内核，无需重启。移除会同时撤除该精确实例的 drop-in。规则重载失败会保留待收尾状态，不把它误报为已完成移除。

## 离线维护与失败处理

为了避免维护期间自动重挂，正常卸载 EFI 前先停监听：

```bash
sudo systemctl stop ram-rescue-efi.path
sudo systemctl stop boot-efi.mount
findmnt -M /boot/efi
# 确认分区未挂载，再执行所需的离线检查。
# 完成后先恢复普通挂载，确认成功，再启动监听。
sudo systemctl start boot-efi.mount
sudo systemctl start ram-rescue-efi.path
```

`.path` 使用原生 inotify 和 mount 状态通知，健康期不创建子进程、不定时扫描或读盘。设置 `TriggerLimitBurst=5`、`TriggerLimitIntervalSec=30s`：30 秒内至多触发 5 次挂载请求，超过后停止监听并报告 failed，避免损坏分区导致持续重试。这也限制同一窗口内快速反复的正常重接。排查实际 mount/fsck 错误、确认文件系统可挂载后，执行 `systemctl reset-failed ram-rescue-efi.path boot-efi.mount`，再按上述顺序先启动普通挂载、再启动 path。`reset-failed` 不会补满当前触发窗口的预算；需要 path 重新发起挂载时应等窗口结束。

path unit 显式关闭默认启动依赖，以免普通 EFI mount 在 `local-fs.target` 之前、path 在 `sysinit.target` 之后产生排序环；关机时仍排在 `umount.target` 之前并随其停止。

## 为什么保留普通挂载

本机 Ubuntu 的 `grub-multi-install` 从 `/proc/mounts` 取 `/boot/efi` 的第一条记录。systemd automount 会增加 autofs 记录，使该版本脚本误认设备；现成的普通挂载兼容当前更新流程。因此没有引入 automount 或额外的包维护 hooks。

## 适用边界

此功能修复的是设备重现后的挂载请求缺失。它不保持旧 EFI 文件句柄，也不保护正在更新 EFI 时的写入、断电或严重 FAT 损坏。原有 fstab 的 fsck 检查策略继续由 Ubuntu 执行；本项目不新增通用自动 fsck 修复。

文件系统检查/挂载失败或 systemd 限流仍可能需要人工处理。不能把设备事件通知当成挂载成功；应检查 `systemctl status boot-efi.mount`、实际挂载源和日志。维护引导器或升级系统前，EFI 必须确实正确挂载。

workspace 中的分区镜像是本次操作的可回退副本；由于与 EFI 位于同一物理 SSD，它不是独立介质备份。
