# 本机 USB 根盘保护启动接入

日期：2026-09-30。范围是当前 `7.0.0-34-generic`、原 USB SSD 上已存在的线性 LVM/ext4 根卷与 shared 卷。本文只记录实际构建、实验和安装状态；镜像通过不代表当前实机已经处于受保护启动。

**当前状态：可选保护启动项和专用 initrd 已安装，原默认入口及原 initrd 保留；本次没有重启，当前实机会话仍是普通启动。真实正常启动和物理拔插尚未验收。**

## 实现

运行核心移至 [`guard/runtime/`](../../guard/runtime/)，实验镜像与实机镜像共用这一份代码。原 `lab/guest/` 下对应实现已删除。健康期仍由内核事件加每秒兜底检查驱动；可能阻塞的核验、换表、路径探测由单个按需 worker 执行，主循环保留期限与状态记录职责。

新增 [`guard/runtime/boot.py`](../../guard/runtime/boot.py) 只负责已有盘启动：身份/布局核验、建立稳定 DM、激活登记卷、确认 LV 依赖、写入本次启动配置。它不包含分区、格式化或新建 PV/VG/LV。切根后由 systemd 启动唯一 Guard，核验本次路径后才发送 `READY=1`。

采用 Ubuntu `initramfs-tools` 的独立配置构建专用 initrd。普通 Ubuntu 的 udev 在 `init-top` 就会发现 PV，因此在首次 udev 扫描前设置 LVM 的过滤与禁止事件自动激活；不能等到 `local-top` 才阻止裸 PV 被抢先激活。启动激活显式通过稳定映射，并启用标准 DM/LVM udev 规则及 cookie 同步，使 systemd 正确识别 LV 与挂载依赖。

运行工具复用原 F9/F10 的 `/run/ram-rescue-demo`，不常驻第二套工具副本。initrd 内认证账号锁定；启动后从原系统本地接入现有救援认证。保护模式中的 `rescue refresh` 拒绝直接刷新 LV，以免绕过预置稳定映射。

## 可回退安装

安装器只新增专用 initrd 和 `Ubuntu USB root protection (7.0.0-34-generic)` GRUB 项。普通 Ubuntu 首项、默认选择、原内核与原 initrd 保留；内置 SSD rEFInd、EFI 内容与固件启动项不变。LVM 限制、udev 规则和 Guard 服务仅在选中保护项的当次启动中存在。

安装前核对当前盘身份与布局、原启动文件、候选镜像哈希，以及通过的 VM 报告中的源码、内核和基础工具包哈希。GRUB 新配置先生成到暂存文件，检查语法及原首项完整性后才替换。异常撤除新增内容；显式回滚仅在文件未被其他升级改变时恢复原菜单。

安装后已独立回读核验：新镜像、GRUB 和 hook 与安装记录哈希一致；原首项、默认选择、内核、原 initrd、宿主 LVM 配置均保持；最终 GRUB 语法检查通过。四个原 RAM 救援服务继续 active。安装备份与记录保存在 `/var/lib/ram-rescue-guard`，专用镜像为 `/boot/initrd.img-7.0.0-34-generic-ram-rescue`。

## 本轮验证状态

- 已有 Ubuntu PV 正常再次启动、持久 sentinel、PV/ext4 UUID、machine-id/fstab 保持及两次正常关机已通过，见 [启动实验记录](../2026-09-25/BOOT-EVOLUTION.md)。
- 172 项 lab 测试与 15 项 RAM 救援测试通过。包括已有盘启动防止重复接入、保护模式禁止手动刷新、GRUB 生成失败及替换后失败回滚。
- 专用 initrd 的标准 Ubuntu 启动、两次快速重接和正常关机通过，见下表。使用与实机候选相同的运行源码、启动集成、内核和基础工具包，仅替换登记的虚拟磁盘身份；串口观察工具只追加在实验镜像中。

| 核验 | 结果 |
|---|---|
| 发行版启动路径 | 使用 Ubuntu 自带 `/init`、udev、fsck 与切根流程；原 lab agent/Guard 被屏蔽 |
| Guard 交接 | 生产 `boot.py` 准备成功，systemd Type=notify 就绪；两次恢复保持同一 owner |
| USB 故障 | QMP 确认删除后等待 0.2 秒再重建设备，重复两次；均自动恢复 |
| 原进程 | PID 与启动时间不变，持续写入/fsync；27 次成功、0 次错误 |
| 实际停顿 | 最长单次写入/fsync 1.6303 秒；这是该次样本观测值 |
| 映射与挂载 | root/shared 的上层 DM 不变、均依赖稳定映射；实际挂载源和 LV 对应，root 保持 rw |
| 用户空间 | 同一 PID 1、D-Bus、journald 进程，multi-user 保持 active；RAM shell 可用 |
| 数据 | 已确认写入的前缀完整回读；再次文件/目录 fsync 成功；原 sentinel 保留 |
| 结束 | 正常关机；作为只读 backing 的原 raw 整体哈希不变 |

原始报告：`lab/work/hb-0930-180705-191300/report.json`，SHA256 `3d1f598d21ce1e3d046c346146a802981031223bf3322d61b1d05525d9b207a9`。VM initrd SHA256 `7490393cda6c9c48b471833329d436d93e87dbac9d611f8523a7756a2de4fdfb`；实机候选 initrd SHA256 `9a9a4d81f0300124873a1707c6d0621bb10fd11dd2f02712ac7411821b3b6b7c`。私有登记、磁盘镜像、原始日志与认证文件不进入 Git。

仅调整 VM 观察 shell 的关机排序后，同一生产镜像补跑也通过全部 23 项检查：31 次快照内成功写、0 错误，最终 33 条 ACK 前缀一致，最长写入 1.9250 秒。补充报告 `lab/work/hb-0930-181107-211908/report.json`，SHA256 `70017b4f3063010581ac57feaa47180c96887036026ddfd7de519a0b009b217c`；安装仍绑定前一份不可变报告。两次关机都观察到 RAM 工具的 `/dev` 绑定挂载及 `/usr/lib/modules` 的一次卸载失败提示，随后正常 Power down；shared 明确成功卸载，未见根卷/shared 的卸载失败或 ext4 journal abort。提示来源尚未完全定位，不将正常关机写成「日志完全无告警」。

已发现并修正两项启动集成问题：早期镜像缺少归档校验命令；RAM 工具原 LVM 配置关闭 udev 规则，导致 LV 已激活却缺少 systemd 等待的 mapper 别名。两者来自标准启动流程的实际 VM 执行，均在安装前修正。

## 首次实机验收

保存工作后，沿内置 rEFInd 的 Ubuntu 入口进入 GRUB，选择新增保护项。首次仅正常启动，核对 Guard 就绪、根卷/shared 卷均经过稳定映射、F9/F10 入口及桌面应用正常；完成后再安排真实短断重接验证。失败时选择普通 Ubuntu 入口返回。

这里尚不覆盖 EFI/exFAT 分区、永久下层驱动阻塞、整机死锁、掉电导致的设备缓存丢失或任意应用的超时策略。QEMU 的移除、等待 0.2 秒再重建设备不等同于物理连接在 0.2 秒内完成枚举；总恢复时间需以实际 I/O 停顿测量为准。
