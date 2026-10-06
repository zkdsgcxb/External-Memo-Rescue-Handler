# USB 根盘断联实验室

当前自动恢复运行时只有 C++。完整 Ubuntu 的挂载、连续恢复、性能对照、事务与打包验收使用 `cpp_guard_probe.py`、`cpp_transaction_probe.py`、`cpp_integration_probe.py`；它们核对虚拟机实际运行的原生 ELF。`boot_failure_probe.py` 验证候选保护镜像失败即停止，完整集成同时验证真实救援认证与会话退出。较早的最小 guest、Python 控制器和研究 runner 已归入[历史实验复现](#历史实验复现)，不再从生产代码打包 Python 运行时。

用 QEMU/KVM 反复制造 USB 断开、重接和设备重新枚举，验证 **RAM 救援入口保活 → 身份核验 → LVM 映射恢复 → 文件系统与应用结果**。默认使用 UAS，也支持普通 USB Mass Storage（BOT）。

## 从干净仓库运行当前版本

新入口不依赖已安装的救援包、作者设备登记、既有 `lab/work/` 或旧 Git 提交。运行前需要当前 Linux 7.0 与匹配模块；以下工具安装到 Ubuntu，源码与可丢弃镜像留在 workspace。

```bash
sudo apt-get install --no-install-recommends qemu-system-x86 qemu-utils busybox-static \
  lvm2 e2fsprogs dosfstools fdisk kmod cpio initramfs-tools zstd xz-utils \
  python3 g++ libssl-dev binutils libdevmapper-dev git curl gnupg ubuntu-cloudimage-keyring

# 普通用户回归：不启动 VM、不读写真实块设备。
python3 -m unittest discover -s lab/tests
python3 -m unittest discover -s ram-rescue-demo/tests

# 默认只显示说明；--run 才明确执行完整实验。
python3 lab/reproduce.py
python3 lab/reproduce.py --run
```

`--run` 验证 Ubuntu 官方签名及镜像 SHA-256，构建通用救援工具，创建 8 GiB 稀疏 USB/LVM Ubuntu 种子并在 guest 中登记，然后从当前代码生成 C++ 保护镜像，依次运行完整集成和 P0 启动失败矩阵。宿主没有块设备透传、共享目录或 guest 网络；只在一次性 VM 内执行分区、LVM 和文件系统格式化。源归档与种子 backing 后续只读，测试使用新 overlay。

内核 `/boot/vmlinuz-*` 不可读时，通过已配置的 Ubuntu APT 源下载当前 `linux-image-$(uname -r)` 包并私有解包，不修改宿主内核；也可显式 `--kernel <可读的当前内核>`。离线复用 `--ubuntu-dir lab/work/<签名源目录>` 时重新验证签名、归档和扇区对齐副本，不信任 `source.json` 中的布尔标志。首次下载需要网络，VM 本身没有网络。只有 x86_64/KVM 完整流程在本轮范围内。

```bash
# 分阶段验收：先创建全新种子、登记和镜像，命令写入 reproduction.json。
python3 lab/reproduce.py --run --prepare-only --work-dir lab/work/my-reproduction
# 也生成 original-initrd.img，供普通 Ubuntu 启动/独立数据盘实验使用。
```

每次选择新的 work 目录；程序保留失败日志，不覆盖前次产物。`reproduction.json` 的 `prepared` 只表示输入已生成，只有两个后续验收均返回成功才写 `passed=true`。通用工具、冻结依赖、当前源码和实验产物均记录散列；这些散列用于追溯与一致性，不取代源码来源审查。详见 [可复现交付报告](../research/2026-10-04/REPRODUCIBILITY.md)。

## 历史实验环境与已有场景

- 当前 Ubuntu 7.0 内核 + Ubuntu 的 BusyBox、Python、LVM、ext4 工具，组成最小 Linux 虚拟机。
- 每次运行新建 2 GiB 稀疏磁盘，虚拟机内建立分区、PV、`labrescue` VG、`ubuntu`/`shared` LV 和 ext4。
- PID 1 通过 `switch_root` 真正运行在 USB/LVM 根卷上，不只是把测试盘挂到一个健康系统旁边。
- 独立工具副本位于 `tmpfs,noswap`，一个串口运行 JSON 控制程序和心跳，另一个串口提供独立 RAM shell。
- QMP 执行 `device_del`/`device_add`。加入一块空白占位盘促进重新枚举；验收还会检查设备名称确实改变、旧 LV 的直接读取确实失败。
- 身份信息全部在虚拟机内生成。此处的 `Recovery` 人工刷新实现仅来自固定历史 Git 快照，测试框架只为虚拟机内指定的 LV 自动提供确认文本；当前 `lab/run.py` CLI 已委托该历史入口，不调用当前只读 `RescueDiagnostics`。
- 每次结束都停止 QEMU，保留磁盘和记录供检查；再次运行使用新盘，不复用故障状态。

默认最小 guest 不覆盖完整 Ubuntu/systemd；现已增加 [Ubuntu Server 模式](UBUNTU.md)，运行真正的 systemd 和常规服务，并支持真实 Git 克隆工作负载。两种模式均不覆盖完整桌面、宿主 F9/F10 安装认证或真实 Hub/供电问题。[后台自动恢复实验](AUTOMATIC.md) 验证非预知断联时的 I/O 排队和同一工作进程继续运行，不等于完整桌面无感运行。

## 工具准备与历史 guest 构建

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
python3 lab/historical.py lab/build.py --kernel "lab/work/kernel-package/extracted/boot/vmlinuz-$(uname -r)"
```

若精确内核包已从源中移除，可以自行提供可读的匹配内核文件，通过 `--kernel` 指定；`--release` 指定其模块版本。不要把不同版本的内核和模块混用。

恢复事务重构、准入边界、死亡接管与故障矩阵见 [TRANSACTIONS.md](TRANSACTIONS.md)；全内存与 CPU 统计口径见 [RESOURCE-MEASUREMENT.md](RESOURCE-MEASUREMENT.md)。

下面的较早对照入口保留原默认路径，需显式指向自行生成的登记/种子/构建；当前完整集成优先使用上面的可复现入口。先按 [C++ 构建说明](../guard/native/README.md) 构建 `lab/work/cpp-runtime/guard-runtime`，各脚本的 `--help` 列出可覆盖路径。性能实验应串行运行，避免 VM 之间争用资源。

```bash
python3 lab/cpp_guard_probe.py --implementation cpp --scenario mounts
python3 lab/cpp_guard_probe.py --implementation cpp --scenario soak --cycles 10
python3 lab/cpp_guard_probe.py --implementation cpp --scenario performance --quota-percent 20
python3 lab/cpp_integration_probe.py
```

原生验收与性能对照见 [完整 C++ 报告](../research/2026-10-02/CPP-MIGRATION.md)，清退后的重新检查见 [Python 运行时清退](../research/2026-10-02/PYTHON-RUNTIME-RETIREMENT.md)。较早的 Python 优化数据保留在 [历史优化验收](../research/2026-10-01/OVERNIGHT-OPTIMIZATION.md)；跨指令集验证见 [架构说明](../guard/ARCHITECTURES.md)。

## P0 启动失败与救援认证验收

2026-10-03 候选通过启动失败矩阵 **7/7**、完整 Ubuntu 集成 **36/36**；故障前后分别完成 6 项真实认证检查，离线 ext4/FAT 检查返回 0。见 [P0 报告](../research/2026-10-03/P0-BOOT-AND-RESCUE.md)、[公开验收摘要](results/2026-10-03-p0-security.json) 和 [失败矩阵摘要](results/2026-10-03-boot-failure-policy.json)。本机首次 C++ 保护启动另已[完成基础核验](../research/2026-10-03/HOST-CPP-BOOT.md)，但这不表示新 P0 策略已在实机激活；其实际运行验收仍待下一次保护启动。

下面使用已经验证的实验登记和 Ubuntu 种子，从当前生产 `guard/build.py` 构建新的专用 initrd，再运行两个独立入口。所有产物均在 `lab/work/`，不使用宿主设备或凭据；新机器使用上方 `reproduce.py --run` 自动生成这些输入；`guard/build.py` 默认构建当前通用工具包，也接受 `--base-rescue-dir` 指定已有构建包。

```bash
python3 guard/build.py --enrollment lab/work/<vm-enrollment>/enrollment.json \
  --work-dir lab/work/<candidate-build>
python3 lab/boot_failure_probe.py --build-dir lab/work/<candidate-build> \
  --enrollment lab/work/<vm-enrollment>/enrollment.json \
  --seed-report lab/work/<seed-run>/report.json
python3 lab/cpp_integration_probe.py --build-dir lab/work/<candidate-build> \
  --binary lab/work/<candidate-build>/native-runtime/guard-runtime \
  --enrollment lab/work/<vm-enrollment>/enrollment.json \
  --seed-report lab/work/<seed-run>/report.json
```

失败矩阵保留生产脚本原样，仅在实验 `ORDER` 文件加入故障触发和继续启动标记。`local-top` 缺少 `nompath`、`init-bottom` 丢失交接令牌两种失败各检查默认、负值和正值 `panic` 参数；第七项在 `/init` 上下文调用发行版 `panic`，检查专用镜像的默认策略。后者是合成调用，不代表实际 fsck 或根挂载故障均已覆盖。验收要求观察到内核停机、没有交互提示、没有继续交接或启动 systemd；串口只被动读取，不发送命令，也不运行旧版未认证入口的利用复现。

完整集成继续检查根盘、登记数据盘、挂载与子挂载、错误身份拒绝、原进程和打开文件的连续性，以及正常关机。认证检查在 guest 内单独的临时 RAM 根目录和挂载命名空间中运行，使用新生成的一次性密码；覆盖锁定账户、错误密码、正确登录、主动退出后重认证、空闲退出后重认证和缺失密码库拒绝。测试前后核对真实打包文件，清理临时凭据和挂载，不读取生产密码库；这些 PTY 检查不能代替实机 F9/F10 键盘登录。

当前自动 Guard 以本机 `7.0.0-34-generic` 为验收基线，必须具备 `DM_MPATH_PROBE_PATHS`（multipath target ≥ 1.15.0），没有旧内核兼容降级。构建器仍可用于历史研究镜像，但不表示自动 Guard 支持这些内核。

历史 EFI 重接挂载验证使用 `python3 lab/historical.py lab/efi_mount_probe.py`，依赖先前通过的完整 Ubuntu 保护启动种子、匹配的登记和构建；用 `--build-dir`、`--enrollment`、`--seed-report` 指向各自私有产物，不能仅从源码检出后直接运行。脚本创建根盘 overlay 和独立 FAT 镜像，复用生产配置，验证快速重接、根盘与 EFI 联合消失、旧卸载/新枚举交叠、原生 fsck 生命周期、失败限流以及关机。最终实测和配置选择见 [EFI 处理报告](../research/2026-10-01/EFI-RECOVERY.md)。额外需要宿主已有的 `dosfstools`，Ubuntu 种子内也必须提供 `fsck.vfat`；脚本不自动安装包、不接受宿主块设备。

测试另一内核时，可将对应 image/modules 包私有解包，以 `--module-root <解包根>` 读取其 `/lib/modules/<release>`，用 `--work-dir lab/work/<独立目录>` 保存新构建，无需安装宿主内核。解包根若只有 `usr/lib`，需要补私有 `lib → usr/lib` 链接，再执行 `depmod -b <解包根> <release>`。`auto_run.py` 和 `research_probe.py` 接受 `--build-dir <独立目录>`；Ubuntu/Git 种子仍沿用原路径。完整命令、来源与已运行的 7.0 对照见 [版本研究](../research/2026-09-25/VERSION-STUDY.md)。

当前通用工具包构建不包含宿主密码或自动登记。真实根盘登记是独立的显式只读操作；历史 guest 入口仍在固定历史快照中复现。

## 历史实验复现

旧 Python 实现不再保存在 `guard/runtime/`。统一入口 `lab/historical.py` 从本地 Git 的固定提交 `e745e5e4b9cde4ffd21d03f6e45a491ca8400083` 提取实验快照，写入 `lab/work/history/<提交>/`，不切换当前工作树、不拉取网络，也不安装实机服务。需要该提交已存在于本地 Git；浅克隆缺少历史时应先取得对应提交。快照有独立源码校验，运行产物仍留在忽略的 `lab/work/`。

```bash
python3 lab/historical.py lab/build.py --help
python3 lab/historical.py lab/auto_run.py --help
python3 lab/historical.py lab/architecture_probe.py --help
```

历史打包对照另允许固定提交 `1f887fe3eca3a6089f3fec28cd30ceeb9ce20048`，通过 `--revision` 指定；不接受任意移动分支作为实验基线。产物目录链接到当前项目的 `lab/work/`，避免在多个快照中重复保存大镜像。

旧 runner 的直接命令仍会转交历史入口，便于读取原报告里的复现命令；其结果必须标注为固定版本。当前 `cpp_guard_probe.py --implementation python` 也只用于历史性能对照，不是生产构建选项。清退范围与依赖边界见 [说明](../research/2026-10-02/PYTHON-RUNTIME-RETIREMENT.md)。

## 真实依赖更新与冷回退

`dependency_upgrade_probe.py` 验证普通 Ubuntu 中完整管理包的 `A → B → A` 冷部署。A 的 `libcrypto.so.3` 从重新验证签名的 Ubuntu 归档中提取，B 使用当前系统实际安装的更新版本；两者必须包版本和文件字节都不同，ELF 架构与 SONAME 相同。当前 C++ 可执行文件、生产管理代码和 systemd 策略保持一致。该实验只替换离线 fixture 的依赖来源，生产构建没有增加任意库注入参数。

```bash
# 准备真实 A/B 包，不启动 VM、不安装宿主包。
python3 lab/dependency_upgrade_probe.py prepare \
  --ubuntu-dir lab/work/example/ubuntu \
  --base-rescue-dir lab/work/example/base-rescue \
  --native-binary lab/work/example/protected/native-runtime/guard-runtime \
  --output lab/work/dependency-inputs

# 独占运行实验 VM；需要前述新建普通 Ubuntu 种子。
python3 lab/dependency_upgrade_probe.py run \
  --fixture-dir lab/work/dependency-inputs \
  --reproduction-dir lab/work/example \
  --output lab/work/dependency-run
```

输入库必须是真实发行版差异；当前库与公开种子版本相同则准备阶段拒绝，不能通过改一个字节或版本字符串伪造更新。准备记录包括归档签名、归档/包状态散列、真实库散列、包版本、同 ABI 核验及旧库的动态链接 smoke check。准备成功不是 VM 验收成功。

执行阶段只操作两个未挂载的实验数据映射：A 安装、登记并启动；有序停止控制器及移除映射后部署 B，保留登记但不热替换 RAM；再次启动核对 B；用同一 `manager upgrade` 入口冷回退已核验完整版本 A，再次启动核对 A。每次启动执行一次断联恢复，并通过 `/proc/<PID>/exe` 和 `/proc/<PID>/map_files` 读取实际执行/映射 inode 的散列。任一步失败保留证据并返回非零；不会把路径上的候选库当成进程已经使用的新库。

## 历史手动恢复场景

```bash
python3 lab/historical.py lab/run.py --scenario baseline
python3 lab/historical.py lab/run.py --scenario idle --gap 0.2
python3 lab/historical.py lab/run.py --scenario write --gap 0.2

# 普通 USB 存储协议对照组
python3 lab/historical.py lab/run.py --scenario write --transport bot --gap 0.2

# 其他故障时长；无 KVM 权限时可加 --tcg（较慢）
python3 lab/historical.py lab/run.py --scenario write --gap 0.05
python3 lab/historical.py lab/run.py --scenario write --gap 1

# 理想时序对照：在拔盘前暂停两个 LV 的 I/O，映射恢复后放行
python3 lab/historical.py lab/run.py --scenario queued-write --gap 0.2
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

实验进程只连接项目内 UNIX socket；运行目录权限 0700，无虚拟网卡、无共享目录、无宿主块设备透传。历史 guest 及实验控制串口使用的 RAM shell 无密码，仅用于隔离实验；不要用于生产部署。新增的救援认证检查另用真实登录程序和一次性密码。guest 初始化另外核对启动参数、QEMU DMI 标记和实验盘序列号后才格式化虚拟盘。

`work/` 全部由 Git 忽略。实验记录在宿主侧持续保存，guest 崩溃后仍可分析；宿主本身的 USB 盘若掉线，这些记录仍可能受影响。需要更可靠的实机取证时，接收端应在独立存储/机器上。

## 下一步顺序

1. 先扩大保活/映射恢复回归矩阵：更多间隔、重复拔插、错误身份、命令阻塞、更多 Ubuntu/systemd 服务与认证集成。
2. 对已经中止 journal 或只读的卷，区分可读数据抢救与离线修复；不在线强行 fsck 或宣称系统恢复正常。
3. 继续扩大“短暂断联时原工作负载存活”的覆盖：需要在 I/O 错误到达 ext4/应用前实现有界等待/重试和稳定块设备身份。仅在错误之后 `lvchange --refresh` 无法撤销失败写入。[自动恢复原型](AUTOMATIC.md) 已实现单 USB 路径的 dm-multipath 排队、身份核验、重接与超时，后续真实根盘启动集成及首次实盘拔插已经验证；任意故障竞态、应用自身超时和长期压力仍未完全覆盖。

机制参考：[QEMU USB 热插拔文档](https://www.qemu.org/docs/master/system/devices/usb.html)、[Linux ext4 错误行为](https://www.kernel.org/doc/html/latest/admin-guide/ext4.html)。实测结果见 [VALIDATION.md](VALIDATION.md)。

## v0.0.1-beta 四次启动验收

`release_probe.py` 使用普通 Ubuntu seed、真实程序包、认证内核参考和一次性数据盘文件，执行首次接入、短断恢复、忙设备拒绝、停用重启、启用重启及卸载重装。它不安装宿主服务。

```sh
python3 lab/release_probe.py --reproduction-dir lab/work/<reproduction> \
  --package-dir lab/work/<sealed-package> --reference-dir lab/work/<kernel-reference> \
  --output lab/work/<new-run>
```

第一次工程验收显式标注 `qualification_fixture=true`，只在隔离 guest 写入测试资格；其报告不能被描述成正式资格验收。工程验收通过并封存结果后，使用引用该结果的外置正式资格再次运行 `--acceptance <release-acceptance.json>`，要求 `qualification_fixture=false`，且包和 subject 完全匹配。

实验 cloud seed 的 `copymods` 使用临时模块目录，观察器每次启动前重新提供已认证的模块参考文件。此实验环境行为必须与真实安装内核的机器区分。
