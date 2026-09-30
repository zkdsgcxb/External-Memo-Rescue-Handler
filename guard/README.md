# 本机 USB 根盘保护启动

目标是当前 Ubuntu 7.0、原 USB SSD、线性 LVM/ext4：在启动时先建立稳定的 DM 映射，再激活原有根卷与 shared 卷；运行中由 Guard 核验原盘并接回。这条启动路径不分区、不创建 PV/VG/LV、不格式化，也不在线改接已经挂载的根卷。

2026-09-30 状态：可选入口已安装，标准 Ubuntu VM 的正常启动、两次快速重接和正常关机通过；实机已进入保护项并通过首次根卷/shared 短断恢复，但 EFI 与内核 WARNING 尚待处理，见 [全面检查](../research/2026-09-30/HOST-POST-RECONNECT-AUDIT.md)。构建验收与镜像哈希见 [接入记录](../research/2026-09-30/HOST-GUARD.md)。

## 启动与退回

安装器只添加 `Ubuntu USB root protection (7.0.0-34-generic)` 可选 GRUB 项和专用 initrd。内置 SSD 的 rEFInd、EFI 文件、正常 Ubuntu 默认项、原内核和原 initrd 均保留。沿原 rEFInd 的 Ubuntu 入口进入 GRUB 后，手动选择保护项。

首次只验收正常启动：确认保护服务就绪、两个 LV 经过稳定映射、桌面和日常程序正常，再安排实际拔插验证。此处的镜像构建及 QEMU 通过不表示实机已经启用保护。

若保护项不能正常启动，重新启动并选择原来的普通 Ubuntu 项。保护用 LVM 配置、服务和 udev 规则只存在于本次启动的 RAM 中；普通入口不使用它们。没有自动重启，也不改变默认选择。

## 实现边界

- 发行版 `initramfs-tools` 负责常规驱动、根卷挂载与切根。独立构建目录里的 hook 在 udev 首次扫描前限制裸 PV 自动激活，`local-top` 调用 `runtime/boot.py` 核验并激活已有卷。
- `runtime/` 是虚拟机与实机共用的 Guard；实验构建直接复制这一份源码。`boot.py` 只做启动准备，systemd 在切根后取得运行期间的唯一 owner，核验初始路径并发送 `READY=1`。
- `nompath` 使用 Ubuntu 自带的启动条件排除 stock multipathd。保护映射采用独立 UUID 前缀；定向 udev 规则阻止在无路径时启动不必要的介质扫描。LVM 只接受稳定映射，Guard 的只读核验使用独立 RAM 配置读取登记原盘。
- Guard 复用 `/run/ram-rescue-demo`，F9/F10 沿用现有用户名和救援密码。密码数据库在实际启动后从原系统本地读取，不嵌入 initrd。Guard 与后代共用 20% CPU/20 ms 周期、128 MiB 内存、零 swap 的 cgroup；RAM 工具为 256 MiB 上限的 `tmpfs,noswap`。
- 自动保护对象是已登记的根 LV 与 shared LV。EFI、exFAT 等其他分区仍走原路径。当前仅锁定已验内核版本；保护项禁用休眠恢复，后续内核升级需重新构建和验证。
- 保护模式下 `rescue refresh` 拒绝直接刷新 LV，避免绕过稳定映射；F9/F10 的状态检查、只读核验和救援 shell 仍保留。

## 构建与安装

源码、私有登记、镜像和实验记录都留在项目工作区。下面路径是示例，已有目录不会被构建器覆盖。

```bash
# 读取已安装救援包的登记，再以实际盘内元数据核验；只写私有构建资料。
pkexec python3 guard/enroll.py --output "$PWD/lab/work/host-enrollment" --uid "$(id -u)" --gid "$(id -g)"
python3 guard/build.py --enrollment lab/work/host-enrollment/enrollment.json --work-dir lab/work/host-build

# 安装前必须提供实际通过且内核/源码哈希相同的标准 Ubuntu 启动与重接报告。
python3 guard/install.py --build-dir lab/work/host-build --enrollment lab/work/host-enrollment/enrollment.json --vm-report lab/work/<vm-run>/report.json
# 审阅生成的菜单后，通过本机系统认证增加可选入口。
pkexec python3 guard/install.py --install --build-dir "$PWD/lab/work/host-build" --enrollment "$PWD/lab/work/host-enrollment/enrollment.json" --vm-report "$PWD/lab/work/<vm-run>/report.json"
```

安装器重新核对当前磁盘布局、原内核/initrd/LVM 配置、候选镜像与测试源码。安装记录和原 GRUB 配置备份位于 `/var/lib/ram-rescue-guard`；生成新配置并检查语法后才替换 GRUB 配置。安装不会重启，也不使当前会话立即获得保护。

如果安装后这些文件未被其他升级改动，可运行 `pkexec python3 guard/install.py --rollback` 撤除新增入口并恢复原菜单。若文件已有变化，回滚器拒绝覆盖，需按当前状态重新生成菜单。无论是否撤除文件，原普通启动项都作为现场退路保留。

当前范围优先验收原盘正常启动及短断重接。永久驱动阻塞、整机死锁、硬件掉电缓存丢失和全部应用行为，不作为本轮已解决的能力。
