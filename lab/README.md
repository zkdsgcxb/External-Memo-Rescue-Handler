# USB 根盘断联实验室

用 QEMU/KVM 反复制造 USB 断开、重接和设备重新枚举，验证 **RAM 救援入口保活 → 身份核验 → LVM 映射恢复 → 文件系统与应用结果**。默认使用 UAS，也支持普通 USB Mass Storage（BOT）。

## 已实现的环境

- 当前 Ubuntu 7.0 内核 + Ubuntu 的 BusyBox、Python、LVM、ext4 工具，组成最小 Linux 虚拟机。
- 每次运行新建 2 GiB 稀疏磁盘，虚拟机内建立分区、PV、`labrescue` VG、`ubuntu`/`shared` LV 和 ext4。
- PID 1 通过 `switch_root` 真正运行在 USB/LVM 根卷上，不只是把测试盘挂到一个健康系统旁边。
- 独立工具副本位于 `tmpfs,noswap`，一个串口运行 JSON 控制程序和心跳，另一个串口提供独立 RAM shell。
- QMP 执行 `device_del`/`device_add`。加入一块空白占位盘促进重新枚举；验收还会检查设备名称确实改变、旧 LV 的直接读取确实失败。
- 身份信息全部在虚拟机内生成。恢复直接调用项目的 `Recovery` 实现；测试框架只为虚拟机内指定的 LV 自动提供确认文本。
- 每次结束都停止 QEMU，保留磁盘和记录供检查；再次运行使用新盘，不复用故障状态。

默认最小 guest 不覆盖完整 Ubuntu/systemd；现已增加 [Ubuntu Server 模式](UBUNTU.md)，运行真正的 systemd 和常规服务，并支持真实 Git 克隆工作负载。两种模式均不覆盖完整桌面、宿主 F9/F10 安装认证或真实 Hub/供电问题。[后台自动恢复实验](AUTOMATIC.md) 验证非预知断联时的 I/O 排队和同一工作进程继续运行，不等于完整桌面无感运行。

## 安装与构建

当前支持 Ubuntu 24.04、x86_64、Python 3.12，内核与 `/lib/modules` 必须匹配。QEMU 等工具安装到 Ubuntu；镜像、源码与记录留在本项目的 `lab/work/`。

```bash
# 仅安装依赖需要管理员权限；实验和构建使用普通用户。
sudo apt-get install --no-install-recommends qemu-system-x86 qemu-utils busybox-static lvm2 e2fsprogs fdisk kmod cpio

# 在项目根目录执行。内核包提供可复制的 guest 内核，不修改宿主启动配置。
mkdir -p lab/work/kernel-package
(
  cd lab/work/kernel-package
  apt-get download "linux-image-$(uname -r)"
  dpkg-deb -x linux-image-*.deb extracted
)
chmod u+r "lab/work/kernel-package/extracted/boot/vmlinuz-$(uname -r)"
python3 lab/build.py --kernel "lab/work/kernel-package/extracted/boot/vmlinuz-$(uname -r)"
```

若精确内核包已从源中移除，可以自行提供可读的匹配内核文件，通过 `--kernel` 指定；`--release` 指定其模块版本。不要把不同版本的内核和模块混用。

恢复事务重构、准入边界、死亡接管与故障矩阵见 [TRANSACTIONS.md](TRANSACTIONS.md)；全内存与 CPU 统计口径见 [RESOURCE-MEASUREMENT.md](RESOURCE-MEASUREMENT.md)。

当前自动 Guard 以本机 `7.0.0-34-generic` 为验收基线，必须具备 `DM_MPATH_PROBE_PATHS`（multipath target ≥ 1.15.0），没有旧内核兼容降级。构建器仍可用于历史研究镜像，但不表示自动 Guard 支持这些内核。

测试另一内核时，可将对应 image/modules 包私有解包，以 `--module-root <解包根>` 读取其 `/lib/modules/<release>`，用 `--work-dir lab/work/<独立目录>` 保存新构建，无需安装宿主内核。解包根若只有 `usr/lib`，需要补私有 `lib → usr/lib` 链接，再执行 `depmod -b <解包根> <release>`。`auto_run.py` 和 `research_probe.py` 接受 `--build-dir <独立目录>`；Ubuntu/Git 种子仍沿用原路径。完整命令、来源与已运行的 7.0 对照见 [版本研究](../research/2026-09-25/VERSION-STUDY.md)。

构建脚本只复用生产构建器的文件/动态库复制函数，不执行其中绑定真实磁盘的登记函数。它不包含宿主密码、主机设备登记清单或实际救援镜像。

## 运行

```bash
python3 lab/run.py --scenario baseline
python3 lab/run.py --scenario idle --gap 0.2
python3 lab/run.py --scenario write --gap 0.2

# 普通 USB 存储协议对照组
python3 lab/run.py --scenario write --transport bot --gap 0.2

# 其他故障时长；无 KVM 权限时可加 --tcg（较慢）
python3 lab/run.py --scenario write --gap 0.05
python3 lab/run.py --scenario write --gap 1

# 理想时序对照：在拔盘前暂停两个 LV 的 I/O，映射恢复后放行
python3 lab/run.py --scenario queued-write --gap 0.2
```

每台虚拟机配置 1.5 GiB RAM、2 vCPU。`baseline` 连续追加文件并 `fsync`，不拔盘；`idle` 不启动应用写负载，但仍可能发生 ext4 后台 I/O；`write` 在写入期间拔盘。

`queued-write` 是**预知故障的实验对照**：RAM 控制程序先对 guest 两个 LV 执行 `dmsetup suspend --noflush --nolockfs`，再拔盘，核验新设备并刷新映射后 resume。检查同一 PID/启动时间的工作进程继续写入、没有 I/O 失败、文件系统仍可写，并记录最长写入延迟。它不是生产自动拦截器，也不能证明在 USB 消失后才暂停仍来得及；不能照搬到正在使用的宿主根卷上。

`--gap` 是确认 QEMU 删除设备后到请求重接的目标间隔，不是保证的物理断联时长。UAS 控制器重建、QMP 调度和 Linux 枚举会增加延迟。报告同时记录实际重接命令完成、身份核验完成的时间。`--settle` 控制恢复后的观察时间（默认 3 秒）。

探针会在虚拟机中丢弃干净页缓存，再尝试从故障根卷执行 `cat`，另外以 `O_DIRECT` 读取 LV 的 ext4 超级块。这样不会把缓存命中当成恢复；探针本身也属于故障期间的额外 I/O，可能影响 ext4 的错误路径。

## 结果与验收

每次结果位于输出中的 `lab/work/<时间>-<场景>-<进程号>/`：

- `report.json`：前后状态、实际时间、恢复响应、映射表、心跳、独立 shell、读写结果及验收。
- `console.log`：内核与启动日志；`agent.jsonl`：串口请求响应及心跳；`qmp.jsonl`：热拔插事件。
- `command.json`：实际 QEMU 参数；`usb.raw`：本次虚拟盘，`decoy.raw`：占位盘。
- `lab/work/build.json`：内核、initramfs 与源文件 SHA-256，构建时 Git 提交作为上下文；工作树源码可能尚未提交，应以哈希为准。

`completed` 只表示实验完成。`recovery_checks_passed` 才表示本轮保活、故障复现、映射恢复和恢复后直接块读取通过；失败会返回非零退出码。**应用读写恢复不与映射恢复混为一个指标**：查看 `probe_after_settle`、`writes_after_refresh` 和内核的 `EXT4/JBD2` 信息。基线要求应用写入成功且没有错误。

暂停对照组不主动读取暂停中的 LV（读取会等待），不执行 `fault_reproduced` 读取失败检查；通过 QMP 删除事件、设备重新枚举和 DM 状态记录故障与暂停。`filesystem_state` 在恢复后同时检查只读标志及实际文件写入/`fsync`；单看 mount 的 `rw` 或成功读取超级块不代表文件系统健康。

实验进程只连接项目内 UNIX socket；运行目录权限 0700，无虚拟网卡、无共享目录、无宿主块设备透传。虚拟机的 RAM shell 无密码，仅用于这个隔离实验；不要把它用于生产部署。guest 初始化另外核对启动参数、QEMU DMI 标记和实验盘序列号后才格式化虚拟盘。

`work/` 全部由 Git 忽略。实验记录在宿主侧持续保存，guest 崩溃后仍可分析；宿主本身的 USB 盘若掉线，这些记录仍可能受影响。需要更可靠的实机取证时，接收端应在独立存储/机器上。

## 下一步顺序

1. 先扩大保活/映射恢复回归矩阵：更多间隔、重复拔插、错误身份、命令阻塞、更多 Ubuntu/systemd 服务与认证集成。
2. 对已经中止 journal 或只读的卷，区分可读数据抢救与离线修复；不在线强行 fsck 或宣称系统恢复正常。
3. “短暂断联无感”单独研究：需要在 I/O 错误到达 ext4/应用前实现有界等待/重试和稳定块设备身份。仅在错误之后 `lvchange --refresh` 无法撤销失败写入。[自动恢复原型](AUTOMATIC.md) 已在虚拟机中实现单 USB 路径的 dm-multipath 排队、身份核验、重接与超时。真实根盘启动集成、故障竞态、应用超时和长期压力仍待验证。

机制参考：[QEMU USB 热插拔文档](https://www.qemu.org/docs/master/system/devices/usb.html)、[Linux ext4 错误行为](https://www.kernel.org/doc/html/latest/admin-guide/ext4.html)。实测结果见 [VALIDATION.md](VALIDATION.md)。
