# P2：从干净仓库复现 Ubuntu 实验

2026-10-04 开始，2026-10-05 完成最终独立验收。本轮把通用工具构建、设备登记、虚拟机初始化和恢复验收分开。干净浅克隆已完成全新种子创建、39 项完整集成检查和 7 种启动失败情形；随后补全历史的普通用户测试为 400/400 与 27/27。结果与边界见本文末尾及公开 JSON。

## 已解除的依赖

| 原限制 | 当前行为 |
| --- | --- |
| 救援构建读取固定 `/dev/sda`、作者序列号与 `vgportable` LV | `ram-rescue-demo/build.py` 只复制工具/依赖；`build(output_dir, identity=None)` 可不带任何设备身份。可选 `--identity` 只读取显式 JSON |
| 根盘登记默认读取已安装救援包 | `guard/enroll.py` 必须显式选择 `--partition` + `--usb-serial`、`--identity` 或 `--base-rescue-dir`。登记复用现有只读准入与切换前复核 |
| 保护镜像默认要求 `/usr/local/lib/ram-rescue-demo` | `guard/build.py` 默认从当前 checkout 构建通用包；`--base-rescue-dir` 可显式复用同一基础包，便于 VM 与宿主产物比较 |
| 完整 C++ 集成调用旧 Git Python checkout 供观察 | 默认使用 `lab/guest/dm_observer.py` 包装当前只读 `guard/admin/dm.py`。没有 Python 恢复循环或故障接管实现 |
| 必须复制作者 `lab/work` 中的 Ubuntu 种子与登记 | `lab/reproduce.py --run` 从签名公开源生成全新的 Ubuntu USB/LVM 种子、登记、内核与普通/保护 initrd，再依次运行现有完整集成与 P0 失败矩阵 |

历史语言 A/B runner 和 `--historical-comparison` 仍保留，只有显式选择历史研究时才需要其完整 Git 提交与旧环境；当前验收不执行历史源码。当前要求 Ubuntu/x86_64、Python 3.12、Linux 7.0 匹配模块和 KVM，未添加旧内核兼容层。

默认当前版本的 `lab/reproduce.py --run` 可以使用浅克隆，不需要旧提交。全量 `unittest discover -s lab/tests` 还包含 5 个历史 fixture 用例（`test_historical` 的 4 项和 `test_cpp_guard_probe` 的历史事务 overlay），因此全量单元测试需完整公开 Git 历史；浅克隆应先 `git fetch --unshallow`。本轮浅克隆的 5 项缺提交错误单独记录，没有跳过用例来掩盖，也不将其认定为当前恢复执行链依赖旧代码。

## 使用方式

依赖安装见 [实验室 README](../../lab/README.md#从干净仓库运行当前版本)。在普通用户、项目根目录运行：

```bash
python3 lab/reproduce.py --run
```

默认无参数只显示说明。`--run` 会下载默认固定日期 `20260911` 的 [Canonical Noble rootfs](https://cloud-images.ubuntu.com/noble/20260911/)，先用系统 `ubuntu-cloudimage-keyring` 验证签名校验列表，再核对归档 SHA-256。缓存也要重新验证，随后逐字节核对 QEMU 使用的填零对齐副本。若旧构建镜像撤下，可显式选择 `--ubuntu-build YYYYMMDD`，记录实际输入；本项目不把可用 URL 当作永远不变的制品库。

内核默认使用可读的当前 `/boot/vmlinuz-*`；不可读时由 APT 下载当前内核包并解包，不要求管理员读取 `/boot`，也不安装包。可显式提供 `--kernel FILE`。模块来自当前 `/lib/modules/<release>`，最终准入仍检查内核与 multipath 接口。

```bash
# 分阶段生成，无作者私有资料依赖；既有目录需要另取名字。
python3 lab/reproduce.py --run --prepare-only --work-dir lab/work/example
# 可选离线复用此前已下载的签名归档（仍复验）。
python3 lab/reproduce.py --run --prepare-only --work-dir lab/work/example-offline \
  --ubuntu-dir lab/work/example/ubuntu --kernel lab/work/example/vmlinuz
```

生成目录包含 `base-rescue/`、`seed/s0/usb.raw`、`seed/report.json`、`enrollment.json`、`original-initrd.img`、`protected/` 以及 `reproduction.json`。后者记录两个完整验收命令；`--prepare-only` 只设 `prepared=true`，不设 `passed=true`。完整默认运行依次调用当前 C++ 集成（含认证、挂载/子挂载、身份拒绝、根盘与数据盘恢复）和 P0 启动失败矩阵。原始日志保留在输出目录，失败不擦除。

干净浅克隆不需要额外私有输入：默认下载的 Ubuntu 归档为 229,233,708 字节，内核可从当前 Ubuntu APT 源下载（本轮核对 `7.0.0-34.34~24.04.1` 仍可用），模块由运行中的同版本内核包提供。还需普通用户可读写 `/dev/kvm`。建议为一次完整运行预留至少 6 GiB 磁盘空间和 4 GiB 空闲 RAM；源码目录使用较短路径，避免继承 runner 的 UNIX socket 路径超过 108 字节。缓存输入下估计完整链约 4–8 分钟，首次下载和宿主争用另计，这不是时限保证。可复用官方签名归档缓存，但必须复制到新 checkout 的 `lab/work` 内并重新验签，不能复用旧登记、seed 或恢复执行体来声称干净初始化。

真实根盘登记仍需本地管理员授权，且是单独操作，例如：

```bash
sudo /usr/bin/rescue-guard-admin enroll-root --name host-enrollment \
  --partition /dev/明确的PV分区 --usb-serial '明确序列号'
mkdir -m 0700 lab/work/host-enrollment
sudo /usr/bin/tar -C /var/lib/ram-rescue-enrollments/host-enrollment \
  -cf - enrollment.json vmlinuz original-initrd.img | \
  tar --no-same-owner -xf - -C lab/work/host-enrollment
python3 guard/build.py --enrollment lab/work/host-enrollment/enrollment.json \
  --kernel lab/work/host-enrollment/vmlinuz --work-dir lab/work/host-build
```

上述命令不会由实验入口执行。固定管理入口将三份导出文件留在 root 私有的 `/var/lib/ram-rescue-enrollments/<name>`，不在用户可写父链创建、覆盖或 chown 文件；管道左侧只读已核验的 root 目录，右侧由普通用户写入 workspace。改用 `--identity` 时先封存成 root 可信文件，直接输入工作区 JSON 会被拒绝。填写的节点不是可信身份凭据；程序还要核验 USB、PV、VG/LV、布局、根挂载与持有的设备实例，现有保护根盘也必须满足稳定映射所有权约束。序列号和 UUID 可克隆，本项目没有硬件真实性认证。

## 隔离及证据范围

- 宿主脚本拒绝 UID 0；只创建 `lab/work` 的常规文件。默认不打开、格式化或绑定宿主块设备，不共享宿主目录，不给 VM 网卡。
- 初始 guest 只有指定 8 GiB 测试 USB、空白占位盘和只读 Ubuntu 归档。格式化程序先核对 cmdline 两个实验标记、QEMU DMI、USB 序列号、容量和归档设备只读属性；只在这些条件成立后创建分区和 LVM。
- 种子解包完整 Ubuntu、干净卸载并关闭 VG 后关机。其 `passed` 仅表示初始化成功，后续恢复验收使用只读 backing 和独立 overlay，核对源种子未变。
- 通用基础包不读宿主密码；内置账户保持锁定。所有归档条目统一 root 所有，目录及文件写权限规范化，不继承宿主 `umask=0002` 的组可写权限。
- 普通 initrd 是为无根盘 Guard 的数据盘验收提供的独立输入，不加入保护启动 hook，不修改真实引导器。
- SHA-256 是一致性和追溯工具；Ubuntu 来源由发行版密钥核验，项目源码及当前系统工具仍须来自使用者信任的来源。没有声称跨时间/跨机器位级可重复构建。

## 当前已运行检查

- 新增 11 项普通用户测试：默认无 VM、UID 0 拒绝、路径越界、缓存重验签、签名失败、归档/对齐副本篡改、可写模式规范化，seed 在非实验机拒绝、独立有界结果通道，以及 ELF 依赖散列/包来源和 256 文件上限。
- 原有 11 项根盘登记回归及 9 项 root 私有输出/可信路径回归通过。
- 实际构建无身份通用工具包成功；当前内核 seed initrd 和普通 Ubuntu initrd 均由普通用户成功打包。
- 本地缓存重新验证 Canonical 签名成功，归档 SHA-256 为 `2ea580b9dd2d0e97ba9047798becc63a94427679876dec193221e345a6e0cdc9`。
- 完整 VM 验收见下文；整轮资源对照另列在总报告，保持各实验执行体的独立 SHA 身份。


### 本轮新种子实测

`lab/work/rp-1004c/seed/report.json` 记录新建 8 GiB USB/LVM Ubuntu 种子成功，guest 控制台约 12.9 秒完成创建、登记、干净卸载和关机；其普通 initrd 与保护镜像可作为后续联合验收输入。这仅是初始化时长，不是断联恢复性能。

两次前置失败证据保留：`rp-1004` 的 GNU tar 调用返回 2，原包装器未保留 stderr；调整解包 PATH 确保选择 GNU xz，并补充错误输出。`rp-1004b` 解包超时，宿主运行时段约 25 分钟、guest 日志时间跳至约 1546 秒，尚未证明是宿主睡眠还是其他暂停影响；没有放宽时限或把它计入成功。相同代码在 `rp-1004c` 完成。

通用归档实查：没有身份文件，manifest 的 identity 为 null；全部非符号链接归档条目 owner/group 为 0，除 `/tmp` 外没有组/其他用户写位。

普通 Ubuntu 数据盘试验还观察到 `proc-sys-fs-binfmt_misc.mount` 启动失败：种子未复制整套当前 `/lib/modules`，普通 initrd 也未预载该可选模块。已测试的原生 x86_64 ELF 与数据盘恢复不依赖它；此种子不能直接视作支持任意额外内核模块的完整工作站镜像。当前保留该现象，没有在并行验收中改写种子。


### 通用工具依赖追溯

构建器在归档内 `/etc/rescue/base-runtime.json` 记录冻结 ELF 可执行文件和共享库（ET_EXEC/ET_DYN，包括 Python 扩展）的 SHA-256 与 dpkg 包/版本/架构，排除不能在运行期装载的 ET_REL 编译对象。复用现有批量 `dependency_packages` 查询，没有新增健康期线程或进程。单次清单最多 256 项；未知包来源明确记为空值，不假冒发行版认证。

本轮修正后的新包 `lab/work/base-prov2-1004/` 实测包含 100 个运行期 ELF 文件、46 个包，清单 30,895 字节。Python 标准库源码不逐文件做 doctor 散列比较；其 minimal/stdlib 发行版包版本由对应 ELF 扩展覆盖，因此这里只保证版本差异可见，不能把它等同于逐字节完整性审计。签名与来源信任边界不变。doctor 的消费与差异输出由诊断模块验收记录说明。

## 最终独立浅克隆验收（2026-10-05）

独立目录为 `/workspace/Project/rr-check-1004`，使用本地 Git 文件传输取得提交 `6b8289c4d03a63cfa677b079108561adfbcdc73f`。这是只含已提交文件的干净浅克隆；本次没有从 GitHub 重新下载项目源码，因此不把本地传输描述成远端下载验收。Ubuntu 缓存 `lab/work/public-ubuntu` 是本轮从官方重新下载的签名归档，程序再次验签并检查填零副本。没有复制原仓库的 seed、登记、工具包或内核。

```bash
python3 lab/reproduce.py --run --work-dir lab/work/r1 \
  --ubuntu-dir lab/work/public-ubuntu
```

默认内核获取流程实际通过 APT 下载 `linux-image-7.0.0-34-generic=7.0.0-34.34~24.04.1`（16.7 MB），不是显式传入旧实验副本。程序从头生成 Ubuntu/LVM 种子、登记、通用工具包、普通与保护 initrd，并编译当前 C++。整个流程约 294 秒；这包含构建和全部 VM，不是恢复延迟指标。

| 检查 | 结果 |
| --- | --- |
| 开始与完成时的 Git 状态 | 均为浅克隆，tracked tree 干净 |
| 全新种子与严格独立串口结果 | 通过 |
| 当前 C++ 完整集成 | **39/39**；种子/源码未变，两个数据文件系统离线检查返回 0 |
| P0 启动失败情形 | **7/7**；包含两阶段与 panic 参数组合及发行版原生失败 |
| 整体 `reproduction.json` | `prepared=true`、`passed=true` |
| 补全历史后的普通用户回归 | lab **400/400**、RAM **27/27** |

只有 VM 全部完成后才执行 `git fetch --unshallow origin main`，并切到 `1f4ff8c21fc771853db963256e08048ccc717441` 运行全量单元测试；后者相对 VM 提交只变更两个普通数据验收观察器/测试文件，没有生产代码差异。这区分了当前实验执行链与历史对照用例的依赖。

全新 C++ 可执行文件 SHA-256 为 `3d100689412815884af4330fda7995ebe81fb4cf3eeae45261fce3afa6e64b42`，与原工作区用于性能和依赖实验的 `479c8b…` 不同。两份 native 构建记录的源码逐文件散列和编译器一致，编译参数中的 checkout 绝对路径不同，运行依赖清单一致。这里证明构建与验收流程可复现，没有声称位级可重复构建，也不把新 ELF 的 39 项结果混记为旧 ELF 的同字节测试。

前一次 `25b76b7` 的 `r0` 失败保留：关机 SCSI 内核消息插入 ttyS0 的 JSON 字符串，使宿主解析失败。修复让结构化结果独占 ttyS1，发送端 raw/flush/drain，接收端最多读取 64 KiB 且只接受单个成功 JSON；新增测试拒绝重复、截断、内核插入、空/超大结果及非零退出。没有从损坏控制台拼接出“成功”，而是新建 `r1` 重跑整条流程。

公开证据：[干净仓库验收 JSON](../../lab/results/2026-10-04-clean-reproduction.json)。其中记录提交、Git 状态、真实输入来源、内核包与各构建产物 SHA、全部 **11 份 VM/汇总报告** SHA、39 项集成与 7 种失败的细项、两套单元测试日志 SHA，以及 `r0` 和此前浅克隆历史用例失败的追溯信息。原始日志留在独立 checkout 的 `lab/work`，没有访问宿主块设备或修改实机引导。
