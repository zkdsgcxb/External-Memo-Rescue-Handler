# 本机 USB 根盘保护启动

> 根盘保护属于 v0.0.1-beta 的高级实验功能。普通数据盘安装请阅读 [用户指南](../docs/USAGE.md)；历史实机记录不等于本次发布的硬件验收。


日常管理使用 [统一后台维护](MANAGER.md)。入口会识别当前根盘 owner，并自动管理登记的数据映射；本页说明内部的根盘启动准备流程。

独立 USB 数据分区见 [DM 恢复配套工具](DATA.md)：复用本目录的恢复事务，只管理明确登记的已有数据映射。普通 Ubuntu 可安装离线包独立维护数据盘，无需先进入本页的根盘保护启动。

目标是当前 Ubuntu 7.0、原 USB SSD、线性 LVM/ext4：在启动时先建立稳定的 DM 映射，再激活原有根卷与 shared 卷；运行中由 Guard 核验原盘并接回。这条启动路径不分区、不创建 PV/VG/LV、不格式化，也不在线改接已经挂载的根卷。

此前实机记录（2026-10-03）：重启后已确认本机实际运行 C++ Guard，根卷/shared 稳定映射、挂载和救援服务通过基础核验，见 [首次 C++ 保护启动](../research/2026-10-03/HOST-CPP-BOOT.md)。新增 P0 启动失败与救援会话策略已完成 QEMU 失败矩阵 7/7、完整集成 36/36；**候选已验证，实机激活待下次启动**，见 [P0 报告](../research/2026-10-03/P0-BOOT-AND-RESCUE.md)。本轮没有实机拔插、睡眠唤醒或人工救援登录验收，块层 WARNING 仍待处理。

本轮新增权限限制、可信安装入口和普通数据盘独立运行的测试与部署边界见 [可信安装说明](../research/2026-10-04/INSTALLATION-AND-TRUST.md) 和 [普通 Ubuntu 数据盘验收](../research/2026-10-04/STANDALONE-DATA.md)。这些新构建不自动替换当前实机 RAM owner。

此前实机根卷/shared 短断恢复与 EFI 处理记录见 [全面检查](../research/2026-09-30/HOST-POST-RECONNECT-AUDIT.md)、[接入记录](../research/2026-09-30/HOST-GUARD.md) 和 [EFI 处理报告](../research/2026-10-01/EFI-RECOVERY.md)；日常 EFI 维护见 [EFI 使用与维护](EFI.md)。

## 启动与退回

安装器只添加 `Ubuntu USB root protection (7.0.0-34-generic)` 可选 GRUB 项和专用 initrd。内置 SSD 的 rEFInd、EFI 文件、正常 Ubuntu 默认项、原内核和原 initrd 均保留。沿原 rEFInd 的 Ubuntu 入口进入 GRUB 后，手动选择保护项。

首次只验收正常启动：确认保护服务就绪、两个 LV 经过稳定映射、桌面和日常程序正常，再安排实际拔插验证。此处的镜像构建及 QEMU 通过不表示实机已经启用保护。

若保护项不能正常启动，重新启动并选择原来的普通 Ubuntu 项。保护用 LVM 配置、服务和 udev 规则只存在于本次启动的 RAM 中；普通入口不使用它们。没有自动重启，也不改变默认选择。

新 P0 候选的专用 initrd 默认设置 `panic=0`：早期失败停止启动，不打开未认证 root shell；项目激活或交接失败也不会继续未保护启动。镜像显式打包停机工具，若停机调用失败，项目脚本仍保持阻塞。此时需要人为重启，再选择普通 Ubuntu 项。该策略依赖可信启动参数与引导链，不防止有权限修改引导参数或离线改盘的人主动改变启动方式。

正常进入系统后，F9/F10 继续使用现有独立救援密码；凭据缺失或账户锁定不放行 shell。新会话默认在等待命令 15 分钟后退出并要求重新登录，不中断前台命令；未登录终端继续阻塞等待，避免周期性重启登录进程。详细参数见 [救援终端说明](../ram-rescue-demo/README.md)。仅升级保护镜像不更新普通回退启动所用的独立救援包，当前 live RAM 也不会被热替换。

## 实现边界

- 发行版 `initramfs-tools` 负责常规驱动、根卷挂载与切根。独立构建目录里的 hook 在 udev 首次扫描前限制裸 PV 自动激活，`local-top` 调用 `guard-runtime activate` 核验并激活已有卷。
- `native/runtime/` 是唯一自动恢复运行时；`admin/` 只保留 Python 管理命令所需的冷态登记与只读校验。`boot.cpp` 只做启动准备，systemd 在切根后取得运行期间的唯一 owner，核验初始路径并发送 `READY=1`。
- `nompath` 使用 Ubuntu 自带的启动条件排除 stock multipathd。保护映射采用独立 UUID 前缀；定向 udev 规则阻止在无路径时启动不必要的介质扫描。LVM 只接受稳定映射，Guard 的只读核验使用独立 RAM 配置读取登记原盘。
- Guard 复用 `/run/ram-rescue-demo`，F9/F10 沿用现有用户名和救援密码。密码数据库在实际启动后从原系统本地读取，不嵌入 initrd。Guard 与后代共用 20% CPU/20 ms 周期、128 MiB 内存、零 swap 的 cgroup；RAM 工具为 256 MiB 上限的 `tmpfs,noswap`。
- 自动保护对象是已登记的根 LV 与 shared LV。EFI、exFAT 等其他分区仍走原路径。当前仅锁定已验内核版本；保护项禁用休眠恢复，后续内核升级需重新构建和验证。
- `rescue` 只提供 `status`、`verify`、`log`、`help`；自动恢复由 C++ Guard 负责。保护启动和普通回退启动均不再提供人工 LV 刷新封装，F9/F10 的完整 root shell 和救援工具仍保留。旧镜像须重新构建、验证和部署后才采用这一变化。

## 构建与安装

构建器只打包 C++ 运行时及完整共享库闭包，不提供 Python 自动恢复选项。源码、用于开发的私有登记副本、镜像和实验记录留在工作区，构建不升级当前会话。原始登记收据暂存在 root 私有的 `/var/lib/ram-rescue-enrollments/<name>`，仅作为受核验的导出源。特权管理代码须先按 [可信安装说明](../research/2026-10-04/INSTALLATION-AND-TRUST.md) 通过审阅后的离线包安装，再从 root 所有的 `/usr/bin/rescue-guard-admin` 执行，不直接提权运行用户可写工作区中的脚本。

```bash
# 明确指定当前根卷的 USB PV 分区及预期序列号；替换占位内容。
# name 只能是一个新目录名，不能提供任意输出路径，也不改变输出所有者。
sudo /usr/bin/rescue-guard-admin enroll-root \
  --name host-enrollment --partition /dev/明确的PV分区 --usb-serial '明确的USB序列号'

# 核对上一步打印的 root 私有目录；root 只读取以下三个固定文件。
# 管道右侧以普通用户写入 workspace，不提权解包，不沿用归档所有者。
mkdir -m 0700 lab/work/host-enrollment
sudo /usr/bin/tar -C /var/lib/ram-rescue-enrollments/host-enrollment \
  -cf - enrollment.json vmlinuz original-initrd.img | \
  tar --no-same-owner -xf - -C lab/work/host-enrollment
python3 guard/build.py --enrollment lab/work/host-enrollment/enrollment.json \
  --kernel lab/work/host-enrollment/vmlinuz --work-dir lab/work/host-build
```

登记只读取显式选定的盘，仍会完整核对 USB、分区、PV/VG/LV、当前根挂载及切换前设备实例。若改用 `--identity`，输入 JSON 必须先经人工核验并封存为 root 所有、父链不可由普通用户修改的可信文件；不能直接把用户可写工作区的 JSON 交给特权入口。`--base-rescue-dir` 同样要求已有可信身份清单，通用无身份工具包不能用于登记。已有名称、目录符号链接、非 0700 登记根目录或复制期间变化的内核输入都会拒绝；失败的私有记录留待检查，重试用新名称。

先取得与候选内核、源码及基础工具相匹配的完整 Ubuntu VM 通过报告，再按可信安装说明生成 bundle、核对 SHA256 并封存到 `/var/lib/ram-rescue-candidates/<SHA256>/`。固定入口仅接受这些 root 私有、受核验的输入。下面是已封存候选的只读预检，路径占位符须替换为实际封存结果：

```bash
sudo /usr/bin/rescue-guard-admin install-image \
  --build-dir /var/lib/ram-rescue-candidates/已审阅SHA256 \
  --enrollment /var/lib/ram-rescue-candidates/已审阅SHA256/enrollment.json \
  --vm-report /var/lib/ram-rescue-candidates/已审阅SHA256/vm_report.json
# 审阅预检后，对同一组参数加 --install 才写入可选启动项。
```

安装器重新核对当前磁盘布局、原内核/initrd/LVM 配置、候选镜像和测试源码。收据及原 GRUB 配置备份位于 `/var/lib/ram-rescue-guard`；生成新配置并检查语法后才替换 GRUB 配置。安装不重启，也不使当前会话立即获得保护。

若安装后相关文件未被其他升级改动，可由固定入口执行 `sudo /usr/bin/rescue-guard-admin install-image --rollback` 撤除新增入口并恢复原菜单。文件已经变化时拒绝覆盖，需根据当前状态重新生成菜单。原普通启动项始终作为现场退路保留。

## 升级已有保护入口

已有安装使用固定入口的 `upgrade-image` 子命令，不重复运行首次安装器。先重新登记当前原盘、构建新镜像，并取得匹配本次源码、内核与基础工具包的通过报告。登记支持当前根卷已位于保留稳定映射之上的情况，只读检查映射与原盘身份，不在线改接根卷。

```bash
sudo /usr/bin/rescue-guard-admin upgrade-image \
  --build-dir /var/lib/ram-rescue-candidates/已审阅SHA256 \
  --enrollment /var/lib/ram-rescue-candidates/已审阅SHA256/enrollment.json \
  --vm-report /var/lib/ram-rescue-candidates/已审阅SHA256/vm_report.json
# 审阅预检后，对同一组参数加 --install；不从工作区直接提供特权输入。
```

升级证据和回退副本位于 `/var/lib/ram-rescue-guard/upgrades/`。升级器只更新保护镜像与安装收据，失败尝试恢复旧文件；未完成事务会阻止下一次升级。GRUB 菜单、普通 initrd、rEFInd 和当前 RAM owner 均保留。不要同时调用旧卸载入口。已经安装统一管理工具时，同时按 [管理包升级](MANAGER.md#升级管理包) 更新下一次启动使用的管理程序。该冷升级可以保留登记，但全部数据映射须已正常退出且控制器 inactive；当前 owner 和 RAM 不热替换。完整旧版本可经逐文件与运行包核验复用，支持 A→B→A 冷回退。

**重启进入原保护项后，新 C++ 镜像才生效。** 当前协议不提供热升级交接：停止旧 owner 会关闭无路径排队并终结事务，新进程会拒绝沿用这个事务。不能通过删除日志、换锁路径或重启服务绕过它。

当前范围优先验收原盘正常启动及短断重接。永久驱动阻塞、整机死锁、硬件掉电缓存丢失和全部应用行为，不作为本轮已解决的能力。
