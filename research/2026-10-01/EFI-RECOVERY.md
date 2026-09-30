# EFI 检查与重接挂载处理

日期：2026-10-01，Asia/Shanghai。范围是本机外置 USB SSD 第一分区的 EFI 状态和 `/boot/efi` 挂载；根卷/shared 的 Guard 恢复机制保持原样。前次问题见 [实机拔插后检查](../2026-09-30/HOST-POST-RECONNECT-AUDIT.md)。

**结论：EFI 已完成整分区备份及未挂载检查，当前普通读写挂载正常；重接后的原生检查/自动挂载集成已在本机安装并启用，无需重启。VM 中确定复现并解决了“新设备早于旧卸载完成”和“旧 fsck 完成状态被复用”两处竞态。此轮未再次拔插真实 SSD，内核 WARNING 仍是独立待办。**

## EFI 文件系统复查

本次开始时，机器已经重新启动到保护入口。Ubuntu 启动日志显示原生 `systemd-fsck@` 成功，EFI 已普通挂载为 vfat/rw。此前拔插后报告的未挂载、dirty 状态是前一次启动中的观察，不应直接当成本次状态。

维护过程中锁定 dpkg，核对 EFI 与保护根卷来自同一磁盘、分区 UUID/PARTUUID 及当前 diskseq，然后正常停止 `boot-efi.mount`，确认没有其他进程挂载命名空间仍挂着该分区。没有使用强制或 lazy 卸载。

在未挂载状态下独占只读打开 EFI，备份完整 **2 GiB** 分区，用单线程 zstd 压缩，再解压逐字节计算 SHA-256，与原始读取结果比对。执行 `fsck.fat -n -v` 返回 **0**：117 个文件，1715/523260 个数据簇，未报告 dirty、主备启动扇区差异或文件链问题。随后原生挂载恢复成功。

**备份复查阶段只执行只读 fsck，没有手动强制修复 FAT 或改写启动程序。** 当前离线检查已经通过，无需针对旧报告继续修改 FAT 元数据。安装挂载集成时仍会经过发行版原有的挂载前自动检查流程。未把这次 EFI 检查扩大为根卷/shared 的离线一致性检查。

| 证据 | SHA-256 |
|---|---|
| 完整 EFI 分区备份的原始内容 | `58fd9eb988b7942f5b94a44af4bc9e3f74e7f8d19e10096628b563e5b52cb404` |
| `efi-partition.img.zst` | `5446e6e4d4763cddbc32b8e49a15e431fb8afbe95710f371d54cc54e4b1b744a` |

备份和原始记录位于被 Git 忽略的 `lab/work/efi-recovery-20261001/`；目录权限 0700，备份和记录 0600。该副本位于同一物理 SSD 的 shared 卷，是本次操作的回退副本，不是独立介质备份。

## 挂载方式选择

保留普通 FAT 挂载和 fstab 原有文件系统检查依赖，由 systemd 负责启动/停止挂载。定向 udev 生成 `/dev/ram-rescue-efi`，原生 `.path` 监听其存在状态并激活 `boot-efi.mount`。不把这项工作加入 Guard，也不在健康期读取磁盘或轮询。

实验发现单独使用 `SYSTEMD_WANTS=boot-efi.mount` 存在确定的漏恢复窗口：旧 umount 在 9.44–13.43 秒执行，新 EFI 的 udev add 在 10.829530 秒到达，最终 mount 留在 inactive/dead。原生 path 在关联 mount 停止后重新检查当前设备节点，能够处理这种先后关系；因此替换单纯 Wants，而不并行保留两个恢复控制器。失败实验保留于 `lab/work/efi-1001-010332-141235/`。对应机制见 [systemd v255 path.c](https://raw.githubusercontent.com/systemd/systemd/v255/src/core/path.c) 和 [unit.c](https://raw.githubusercontent.com/systemd/systemd/v255/src/core/unit.c)。

path 原生限流为 30 秒最多 5 次激活，超限后停止并报告失败，避免挂载失败时持续重试。没有额外常驻进程；离线维护前必须先停止 path，再卸载 EFI。

第二个实验发现，即使 `.path` 成功挂回，旧 `systemd-fsck@` 的 active/exited 状态也可能跨越快速重枚举而保留，导致未重新检查并遗留 FAT dirty 标志。仅为登记 EFI 的 fsck 实例添加 `RemainAfterExit=no`，使下一次挂载事务重新调用发行版 fsck。原生命令及修复参数不变；fsck 自带 `-M` 跳过已挂载设备，成功完成的 oneshot 变为 inactive 也不会卸载依赖它的 mount。安装时在 dpkg 锁内正常卸载/停止旧 fsck 状态/重新挂载，使这一修改立即生效。原失败证据在 `lab/work/efi-1001-011308-160488/`。

本机 `/usr/lib/grub/grub-multi-install` 直接读取 `/proc/mounts` 中挂载点的首项；systemd automount 的 autofs 记录会被该版本当成设备。这与 [Ubuntu bug 1948571](https://bugs.launchpad.net/bugs/1948571) 描述一致。因此不采用 automount，也不为了它额外维护 GRUB/dpkg hooks。

设备身份规则使用 FAT UUID、GPT PARTUUID 和 `ID_USB_SERIAL_SHORT` 三项精确匹配。本机桥设备的 `ID_BUS` 是 `scsi`，通用 SCSI serial 不能替代真实 USB serial。生成的私有身份配置不公开提交。

具体安装、移除和适用边界见 [EFI 集成说明](../../guard/EFI.md)。

## 虚拟机验收

使用完整 Ubuntu 用户空间、systemd `255.4-1ubuntu8.17` 和当前保护内核。根盘使用原种子的全新 qcow2 overlay，EFI 使用新建 GPT/FAT32 镜像，不透传宿主块设备。最终脚本 [efi_mount_probe.py](../../lab/efi_mount_probe.py) 直接导入生产规则、path 和 fsck drop-in 的生成函数；报告绑定生产源码哈希。

最终 `lab/work/efi-1001-013855-241677/report.json`：**22 项检查全部通过**。

| 用例 | 已观察结果 |
|---|---|
| 普通拔插并用占位设备改变枚举节点 | EFI 从新节点自动挂回，测试文件 SHA-256 相同 |
| QMP 删除后间隔 0.2 秒重接 | 自动挂回；该间隔不等于 Linux 完整恢复时间 |
| 根盘与 EFI 同时消失，EFI 先于根盘返回 | 原 boot/PID1 保持，Guard 恢复根盘；EFI 自动挂回 |
| 新设备到达早于旧卸载完成 | VM 专用延迟使旧卸载处于 9.89–13.78 秒，新 EFI 在 11.264191 秒到达；新检查/挂载仍成功 |
| 每轮重新检查 | 精确 EFI fsck 实例的执行时间均更新，未沿用上次完成状态 |
| 对已挂载项重复 start | 原挂载记录和文件内容保持；没有重复挂载 |
| 人为配置无效文件系统类型 | path 达到限额后进入 `trigger-limit-hit`，额外观察 1 秒不继续触发检查；清除故障并人工恢复普通挂载后可恢复监听 |
| 监听和挂载均启用时关机 | 正常 poweroff；关机后直接检查虚拟 FAT 返回 0 |

最后一轮只增加 VM 内的挂载失败与卸载延迟，不在生产运行包中引入实验 hooks。原始 Ubuntu 种子哈希未改变，无残留 QEMU。单元回归为 lab **203 项**（其中 EFI 安装器 31 项）及 RAM 救援 **15 项**，全部通过。

| 最终验收对象 | SHA-256 |
|---|---|
| 生产 `guard/efi_mount.py` | `cf970651e34539710a2b0d83595cf1d316e667a5ee9bc7fc51aef9e170d71e3a` |
| VM `report.json` | `6a8c058f71d544aff618c6aa5abe58df3b47ce281140f2d4cd2e874861c65098` |
| VM 验证脚本 | `0020413ed5027b2bc2c465187ca74cf95849d6a800c3025ef2b53232f0bd005c` |

## 实机安装与最终复核

2026-10-01 01:45:45，经本机管理员认证安装。新增内容只有定向 udev 规则、原生 path unit/启用链接，以及该 EFI fsck 实例的单项 drop-in；没有安装新后台程序。启动依赖及 udev 语法检查通过。

| 检查 | 结果 |
|---|---|
| `/boot/efi` | 来自登记第一分区，普通 vfat/rw；无 autofs |
| `ram-rescue-efi.path` | enabled、active/running，指向原生 `boot-efi.mount` |
| 专用设备链接 | 指向与登记 UUID 相同的当前块设备 |
| 原生 fsck | 安装中在未挂载窗口实际运行，退出 0；`RemainAfterExit=no`，完成后 inactive/dead + success 为正常状态 |
| EFI 挂回 | 正常卸载完成至再次 mounted 约 52 ms；本次只是维护挂载，不是故障恢复耗时 |
| Guard | 原 PID 681 保持、ready；稳定 DM 后端依赖未变 |
| 引导与系统配置 | fstab 字节哈希、EFI 中 shim/GRUB/MokManager/配置哈希均与安装前相同；未重启 |
| systemd | 无 failed units |

本机验证使用正常卸载/检查/挂载及定向 udev change 生成设备链接，没有断开 USB、暂停根卷或注入 I/O 错误。未来真实拔插的自动挂载仍需在合适时机单独验收，不能把 VM 结果写成已经完成新的物理实验。

安装前后证据保存在 `lab/work/efi-recovery-20261001/installed-host-check.json`，SHA-256：`37618df3586cd80461ec484b5d3e07fc2c6f8a86026d30e9f6908fd58a75cb92`。其中含单调时间日志、原生检查状态、配置/启动文件前后哈希及 Guard 状态；私有原始文件不公开提交。

## 保留边界

- 恢复 `/boot/efi` 挂载不等于保留断联前的 EFI 文件句柄，不保护正在进行的 EFI 更新写入。严重 FAT 损坏仍需离线检查处理。
- 原有系统 fsck 策略没有被本项目扩展为任意设备自动修复；不能单凭规则存在就认为实际挂载成功。
- 内置 SSD 上的 rEFInd、GRUB 默认项、保护 initrd、Guard 代码和内核均不在本次修改范围。
- 之前观察到的 `blk_rq_set_mixed_merge` 内核 WARNING 仍未闭环；EFI 问题收尾不代表它已解决。
