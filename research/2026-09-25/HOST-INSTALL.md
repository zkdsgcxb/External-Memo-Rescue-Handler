# 实机 HWE 内核安装记录

**后续状态：用户已重启，确认运行 7.0.0-34，初步启动与存储检查通过，使用反馈暂时正常。** 日志对比及尚未验收的情形见 [首次启动记录](HOST-POSTBOOT.md)。下文保留安装完成、尚未重启时的记录。

日期：2026-09-25。**官方 HWE `7.0.0-34-generic` 已安装，实机尚未重启；当前运行的内核仍为 `6.8.0-139-generic`。** 本文记录安装与静态启动检查，不表示新内核的实机启动、显示、休眠或 USB 行为已经验证。

此前的 [升级预检](HOST-UPGRADE.md) 和 [QEMU 版本对照](VERSION-STUDY.md) 是本次依据。机器状态和脱敏校验结果见 [host-install-result.json](host-install-result.json)。

## 实际引导链

用户确认使用内置 SSD 的 rEFInd；本次将对应 ESP 只读挂载，读取配置和 rEFInd 保存的启动选择后卸载。得到的链路证据为：

1. 固件当前入口是内置 SSD 上的 `EFI/refind/refind_x64.efi`。
2. rEFInd 的 `PreviousBoot` 指向标签为 `EFI` 的外置 USB ESP 上 `EFI/ubuntu/grubx64.efi`。
3. 该外置 ESP 的 `grub.cfg` 指向当前 Ubuntu LVM 根文件系统上的 `/boot/grub/grub.cfg`。
4. 内置 SSD 上还存在指向旧根 UUID 的 Ubuntu EFI 副本；它在 rEFInd 的 `HiddenTags` 中，未作为本次安装目标。

因此沿用现有 rEFInd 和外置 Ubuntu GRUB 入口，不需要重新安装引导器。安装前后逐文件比较了内置 ESP 的 rEFInd/Ubuntu 目录和外置 ESP 的 EFI 目录，名称集合与 SHA256 均一致；`efibootmgr -v` 输出一致。没有修改 rEFInd、EFI 可执行文件或固件启动项，也没有执行 `grub-install --removable`。

这些文件与历史选择足以确定本次维护对象；真实重启时的菜单显示和启动行为仍需现场验收。

## 已完成的变更

- 刷新 Ubuntu 官方源的签名索引，重新模拟并严格核对十包计划后，安装 `linux-generic-hwe-24.04=7.0.0-34.34~24.04.1` 及其依赖：十个新增包、零升级、零删除。
- 使用 `--install-recommends --no-remove`；复用此前已校验的内核包缓存，其余下载约 57 MB，总归档量约 242 MB。
- 将旧 `6.8.0-139` 的 image、modules、modules-extra 三个包标记为手动安装，保留对应内核和 initramfs。
- 新增 `/etc/default/grub.d/99-hwe-recovery.cfg`，明确设置 `GRUB_TIMEOUT_STYLE=menu`、`GRUB_TIMEOUT=10`。安装钩子重新生成 GRUB 配置，默认 Ubuntu 项指向 7.0，高级菜单同时保留 7.0 和 6.8。
- 本次 APT 进程设置 `NEEDRESTART_MODE=l`，只报告待重启项。实际输出显示没有服务、容器或用户会话需要重启；本次没有执行重启。

补充预检口径：原 `/etc/default/grub` 虽为 hidden/0，受保护的旧生成配置末尾实际上还有 os-prober 添加的 menu/10 逻辑。因此此前不能据默认文件单独断言菜单必然不可见。此次显式 drop-in 使 10 秒菜单策略不再依赖是否发现另一个系统。

## 已通过的安装检查

| 检查 | 结果 |
|---|---|
| 十个软件包的实际版本与状态 | 全部与计划相符，`dpkg --audit` 无异常 |
| 新内核镜像 | SHA256 与 QEMU 已测试的 Ubuntu 7.0 镜像一致 |
| 旧内核和旧 initramfs | 安装前后 SHA256 相同，旧模块目录存在，保留包为 manual |
| 新 GRUB 配置 | `grub-script-check` 通过；新旧入口同时存在；默认内核为 7.0；显式菜单 10 秒 |
| USB 启动支持 | 7.0 的 xHCI HCD/PCI 为内建；initramfs 含目标 ABI 的 usb-storage、uas 和模块索引 |
| LVM/ext4 启动支持 | SCSI/sd/DM/ext4 为内建；initramfs 含 LVM、dmsetup、vgchange、udev、相关规则和启动脚本 |
| 引导器与固件设置 | 检查范围内的 EFI 文件及启动项完全一致 |

7.0 中 xHCI PCI 已是内建，不能沿用旧内核检查方式，因 initramfs 没有 `xhci-pci.ko` 就判定缺驱动。当前实机仍是原 LVM 根卷布局，此次没有部署实验室 Guard/multipath 自动保护。

完整配置备份、管理员脚本、安装日志、initramfs 清单与安装前后 GRUB 配置保存在项目私有目录 `lab/work/host-upgrade-20260925/`，由 Git 忽略。公开 JSON 保存这些文件的哈希，不发布实际设备 UUID 和原始配置。管理员认证通过本机系统窗口完成。

## 首次重启与回退

保存工作后正常重启。在**内置 SSD 的 rEFInd** 中沿用原来外置盘 `EFI` 卷的 Ubuntu 入口；GRUB 默认 Ubuntu 项将加载 `7.0.0-34-generic`。进入系统后运行 `uname -r` 确认。

如果新内核启动或桌面异常，重新进入同一 GRUB，在 `Advanced options for Ubuntu` 中选择 **`Ubuntu, with Linux 6.8.0-139-generic`**。旧内核文件、模块和入口都已保留；实际菜单交互尚未重启验证。

首次新内核启动后，还需检查根卷与 `/workspace`、网络、音频、显示/外接屏、中文输入、常用开发工具和正常休眠唤醒，再比较性能。本次不以拔掉工作根盘进行升级验收。重启后验证完成前，保留 6.8 回退包，不执行内核清理。
