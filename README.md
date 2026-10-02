# External-Memo-Rescue-Handler

**统一后台入口：** 已有根盘保护和登记的数据映射统一查看与维护，无需选择根盘／普通设备模式。数据映射出现时由 systemd 自动启动恢复实例；没有新增定时扫盘总管。安装、登记及“映射必须预先建立”的边界见 [`guard/MANAGER.md`](guard/MANAGER.md)。

**C++ 迁移与资源比较：** 新版启动准备、后台监视、身份核验、换表、内核路径探测及死亡接管均由 [完整 C++17 运行时](guard/native/README.md) 执行。Python 保留为管理/实验工具和明确选择的行为对照。根盘与两块数据盘共同恢复、子挂载与已打开 FD、十轮恢复、事务死亡矩阵和资源对照见 [迁移报告](research/2026-10-02/CPP-MIGRATION.md)。本轮只在 QEMU 验证，尚未替换本机运行包。

此前的 [Python 优化与配额实验](research/2026-10-01/OVERNIGHT-OPTIMIZATION.md)、[原生 systemd 挂载计划](guard/MOUNTS.md)、[分层架构验证](guard/ARCHITECTURES.md) 和 [只读 C++ 观察器](guard/native/OBSERVER.md) 保留为历史证据。观察器数据不等于完整恢复运行时的资源占用。

针对本机 USB 外置根盘故障的 RAM 救援终端原型，实现在 [`ram-rescue-demo/`](ram-rescue-demo/README.md)。源码与开发数据放在 shared 卷的本项目目录；安装后的系统运行包位于 Ubuntu 的 `/usr/local/lib/ram-rescue-demo`，运行时工具位于 `/run/ram-rescue-demo` 的 RAM 文件系统。

可重复的虚拟 USB/UAS 断联实验见 [`lab/README.md`](lab/README.md)：真实内核、USB 根盘、LVM/ext4、RAM 救援通道与 QMP 故障注入，实验只使用新建的虚拟磁盘。

独立 USB 数据盘的实验扩展见 [`guard/DATA.md`](guard/DATA.md)：定位为 **DM multipath 的热插拔恢复配套工具**，复用现有控制器管理预先登记的单路径数据映射，提供身份核验、路径恢复和有界失败处理。它不负责格式化或自动接管任意 U 盘；本机尚未接入额外实盘。

**交互方式：普通启动保留 F9/F10 手动救援；可选保护启动提供后台自动排队与重接，不依赖弹窗。** 2026-09-30 已安装专用 initrd 和 `Ubuntu USB root protection (7.0.0-34-generic)` GRUB 项，原默认入口保留；**已实际进入保护启动，首次实机拔插中根卷/shared 原进程及数据验收通过**。2026-10-01 EFI 已备份、离线检查通过并恢复正常挂载，基于原生 udev/systemd 的重接检查与自动挂载已安装；VM 快速重接及旧卸载/新枚举交叠验收通过，此轮未再物理拔插。详见 [EFI 处理报告](research/2026-10-01/EFI-RECOVERY.md)。块层 WARNING 仍待定位，整体验收仍有保留；历史现场证据见 [拔插后全面检查](research/2026-09-30/HOST-POST-RECONNECT-AUDIT.md)。使用方式见 [`guard/README.md`](guard/README.md)，启动接入见 [实机接入记录](research/2026-09-30/HOST-GUARD.md)。

**当前优先目标（2026-10-01）：解决本机原 USB SSD 短暂断联、重枚举后 LVM 根系统无法继续工作的实际问题。** 以当前 7.0 内核、已有线性 LVM/ext4 和内置 SSD 的 rEFInd 引导链为范围，优先完成已有磁盘的启动接入、原盘快速重接与原进程继续读写，并保留 RAM 救援及原启动方式。实机已通过预置稳定 DM 承载 LVM，EFI 挂载集成已安装；额外数据分区仅在虚拟机开展隔离扩展。内核警告、永久驱动阻塞和全故障覆盖仍未解决。

各部件的职责、我们新增的工作、复用的开源实现与可替换边界见 [`ARCHITECTURE.md`](ARCHITECTURE.md)。

共享 Guard 已接入新内核的 `DM_MPATH_PROBE_PATHS`，替换换表后直接宣布就绪的分支；当前以本机 7.0 为基线，要求此接口，不提供旧内核兼容降级。健康期不主动读盘。机制和 QEMU 验收见 [路径探测接入](lab/KERNEL-PROBE.md)。

已按技术路线收敛恢复事务：独立准入凭证、唯一 owner、单次内核换表、RAM 有界事务记录与死亡接管；超时停止后续自动准入，保留在途 I/O 的边界。组件职责与复现命令见 [恢复事务](lab/TRANSACTIONS.md)，本轮实测与剩余门槛见 [重构记录](research/2026-09-25/REFACTOR-RESULTS.md)。

完整 Ubuntu Server 用户空间和真实 Git 克隆场景见 [`lab/UBUNTU.md`](lab/UBUNTU.md)，使用 systemd 管理服务并从虚拟 USB/LVM 根卷运行。

2026-09-25 的系统性调研、故障分类、组件选型和新增边界实验见 [技术路线报告](research/2026-09-25/TECHNICAL-ROUTE.md)。报告区分已测能力与待验证情形，不代表实机部署完成。

后续已完成 Linux 6.8/7.0 与 multipath-tools 0.9.4/0.15 的隔离对照，包括新内核下完整 Ubuntu 根盘/Git 拔插测试及仍存在的故障边界，见 [版本对照](research/2026-09-25/VERSION-STUDY.md)。实机官方 HWE 7.0 已启动，**初步启动与存储检查通过**，随后按用户要求清退旧 6.8，后续更新跟随 HWE；引导链和清退结果见 [实机安装记录](research/2026-09-25/HOST-INSTALL.md)，日志提示与未完成的验收见 [首次启动记录](research/2026-09-25/HOST-POSTBOOT.md)。

## 能力边界

- 正常启动并准备服务后，提供 F9/F10 两个独立密码登录入口、RAM 工具环境与内核日志收集。
- 核验预先登记的 USB 设备、分区、PV/VG 身份；人工确认后，只尝试刷新已激活的 `ubuntu` 或 `shared` 线性 LV 映射。
- 早期手动救援包构建器绑定本机设备和 Python 3.12/x86_64 工具布局；新的 C++ 核心另有 ARM64/RISC-V 用户态验证，但不等于这些平台的完整安装与故障验收。更换设备仍需核验登记逻辑。
- 与宿主共用内核，不能覆盖启动早期故障、kernel panic、全局死锁；内核 I/O 阻塞可能让命令无法及时退出。
- 映射恢复不代表文件系统、失败写入或应用恢复；手动救援 helper 不负责自动 fsck、重挂载、USB 重置或网络登录。EFI 的独立原生检查/重挂配置见 [`guard/EFI.md`](guard/EFI.md)。
- 登录后是 root shell，helper 的操作限制不是安全沙箱；人工命令仍能访问宿主设备。
- RAM 日志重启即失；保护启动已通过一次实际短断中的根卷/shared 测试，故障窗口内手动救援终端交互尚未现场验证。

2026-09-23 只读检查：prepare、tty9、tty10、log 四个服务均 active，两个终端及日志服务均 enabled；运行挂载具备 `noswap`，slice 限额 768 MiB、禁止 swap。此状态不代替人工登录和故障现场验证。历史制作记录见 [`VALIDATION.md`](ram-rescue-demo/VALIDATION.md)。

## 版本管理

仓库根目录是 `External-Memo-Rescue-Handler`，主分支为 `main`。跟踪源码、服务配置、测试、说明和故障摘要；忽略镜像、构建目录、生成的设备清单/校验文件、缓存及原始故障日志。忽略只影响 Git，不删除现有文件。

```bash
git status
git diff
python3 -m unittest discover -s ram-rescue-demo/tests -p 'test_*.py' -v
git add <已检查的文件>
git diff --cached
git commit -m "说明本次变更"
```

新检出仓库不含运行镜像，需要先在匹配的健康本机上按子目录 README 构建，再进行隔离烟测与安装。源码提交不会更新已安装的运行包；运行包更新仍需显式构建、测试和重新安装。

公开版本包含源码、测试和说明，不包含运行镜像、密码散列或原始诊断日志。构建器仍绑定原开发设备的序列号；在其他机器上使用前必须审查并调整设备登记逻辑。

本地 Git 历史位于 shared 卷，不能代替独立介质备份；被忽略的构建产物和诊断数据也不会随 Git 推送备份。
