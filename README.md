# External-Memo-Rescue-Handler

**面向 Linux USB 存储短暂断联的后台恢复工具，为 DM multipath 补充原盘核验与重接管理。**

项目起源于外置 USB SSD 上的 Ubuntu / LVM 根系统：磁盘快速拔插后重新枚举，原映射仍指向失效的设备实例，导致后续 I/O 失败。现在，根盘与预先登记的数据盘共用一套 C++17 恢复控制器，配有 RAM 手动救援环境和可重复的 QEMU 故障实验。

目标是在可恢复的短暂断联中，保留稳定的块设备、原挂载和原进程，让等待中的 I/O 在原盘重接后继续。恢复期间读写可能阻塞；项目不保证任意故障下零中断。

[快速开始](#快速开始) · [性能与验证](#性能与验证) · [支持范围](#支持范围) · [项目结构](#项目结构) · [下一版本目标](ROADMAP.md) · [文档导航](#文档导航) · [开源协议](#开源协议)

## 工作原理

受保护的 I/O 路径为：

```text
应用 / 文件系统 → LVM（可选）→ 稳定 DM multipath 映射 → USB 存储
```

| 部件 | 职责 |
| --- | --- |
| Linux DM multipath | 维持稳定块设备，在无可用路径时按配置排队 I/O |
| C++ Guard | 监视登记映射，核验重连盘身份，更新底层路径并通过内核探测确认就绪 |
| systemd / udev | 启动对应维护实例、限制资源、执行死亡接管及维护挂载依赖 |
| Python 管理工具 | 设备登记、构建安装、生成挂载计划、运行实验与生成报告 |
| RAM 救援环境 | 保存恢复工具和临时日志，提供独立的 F9/F10 手动救援入口 |

自动恢复仅使用 C++ 运行时；Python 负责管理、实验与人工救援。旧 Python 自动恢复实现已从当前源码移除，历史对照从固定 Git 提交复现，见 [清退记录](research/2026-10-02/PYTHON-RUNTIME-RETIREMENT.md)。

Guard 是用户空间服务。内核负责块 I/O、USB/SCSI 和文件系统处理；Guard 不替代这些实现。健康期等待设备/DM 事件，按配置定期兜底检查，不主动读盘；恢复期才执行必要的身份和介质核验。

职责划分、复用的开源实现和可替换边界见 [架构说明](ARCHITECTURE.md)。

## 已有功能

- **统一后台维护**：根盘与登记数据映射使用同一套恢复逻辑；日常恢复静默执行，无需选择设备模式或手动触发。
- **原盘身份核验**：结合 USB、分区、LVM 或文件系统身份及设备实例信息，拒绝把错误设备接入旧映射。
- **有界恢复事务**：每张映射只有一个恢复执行者，保留事务记录、最终身份核验、截止时间及进程死亡后的接管。
- **挂载连续性验证**：覆盖根卷、ext4/FAT 数据卷、嵌套挂载、bind 子挂载与已打开文件；挂载计划交由原生 systemd 管理。
- **资源控制与救援入口**：使用事件合并、退避和 cgroup 配额控制开销，保留 RAM 工具及人工救援通道。
- **独立数据盘与按需诊断**：普通 Ubuntu 启动可准备独立 RAM 工具；`doctor` / `export` 提供状态、事务与冻结依赖差异，默认本地脱敏保存。
- **受控安装与安全检查**：使用固定的 root 管理入口、离线包、可信路径检查及服务权限限制；构建验证实际 ELF 防护，CI 包含 sanitizer 和异常输入测试。

维护对象必须预先登记并建立稳定 DM 映射。项目不会自动接管任意插入的 U 盘，也不会将正在挂载使用的裸分区在线改接。接入方式见 [统一后台维护](guard/MANAGER.md)。

## 快速开始

建议先构建、测试，再在虚拟机中验证。下面的命令在项目目录内生成产物，不安装系统服务或修改启动配置。

### 获取源码与构建

构建需要 Python 3、支持 C++17 的 GCC、binutils 和 OpenSSL 开发头文件；Ubuntu 中对应 `python3`、`g++`、`binutils`、`libssl-dev`。运行时使用发行版的 libdevmapper。完整要求见 [C++ 运行时说明](guard/native/README.md)。

```bash
git clone git@github.com:zkdsgcxb/External-Memo-Rescue-Handler.git
cd External-Memo-Rescue-Handler

python3 guard/native/build_runtime.py --output lab/work/cpp-runtime
lab/work/cpp-runtime/guard-runtime --version
```

构建器默认执行原生单元检查，输出 ELF、独立调试符号和散列清单。nlohmann/json 已随源码提供，无需在构建时下载。

### 运行回归检查

```bash
python3 -m unittest discover -s lab/tests -v
python3 -m unittest discover -s ram-rescue-demo/tests -v
```

### 虚拟机实验与实机接入

当前版本可以从公开签名 Ubuntu 镜像生成一次性 USB/LVM 实验盘，不需要作者的登记文件、已安装救援包或私有 `lab/work/`：

```bash
# 明确启动 QEMU、生成种子并执行完整恢复和启动失败验收。
python3 lab/reproduce.py --run
```

依赖、输入版本及分阶段命令见 [QEMU 实验室](lab/README.md)。不带 `--run` 只显示说明。实验不传入宿主块设备，保留原始日志与输入散列。

实机接入先阅读 [可信安装与冷态升级](research/2026-10-04/INSTALLATION-AND-TRUST.md)，使用已审阅离线包安装固定管理入口。根盘专用镜像、普通数据映射与管理配置分别说明于 [根盘保护](guard/README.md)、[统一后台维护](guard/MANAGER.md) 和 [数据映射边界](guard/DATA.md)。源码、磁盘上的包和 RAM 中实际运行的版本必须分别核对。

## 性能与验证

2026-10-04—05 对改进前后的完整 C++ 包执行 AB/BA 四场 Ubuntu QEMU 实验，每场 **26/26** 验收通过。两边使用相同内核、种子、基础工具与观察器；统计**根盘、ext4 与 FAT 三个 Guard 及其子进程合计**，一个逻辑核占满为 100%。

| 指标 | 改进前 C++ | 本轮 C++ |
| --- | ---: | ---: |
| 常态平均 CPU | 0.070% | 0.077% |
| 常态 CPU 峰值，约 20 ms 窗口 | 2.49% | 2.92% |
| 常态 PSS 采样峰值 | 9.56 MiB | 9.73 MiB |
| 恢复 CPU 峰值，约 20 ms 窗口 | 36.66% | 32.34% |
| 恢复 PSS 采样峰值 | 10.51 MiB | 17.74 MiB |
| 三映射恢复耗时 | 2.855 s | 3.282 s |

CPU 均值和耗时取每版两场的中位数，峰值取两场最大的观察窗口；PSS 每 200 ms 采样。本轮补齐安全与接入能力后，常态开销略增，恢复 CPU 峰值降低，但恢复耗时未改善。新版恢复 PSS 峰值捕获了短命 helper，旧版最大快照仅含三个控制器；这些采样不能证明两者真正的瞬时内存上限。

CPU 不包含业务程序、日志服务、采样器、udev 及其他内核工作线程。根服务与数据盘父 slice 各有单核 20% 配额，合计预算为 40%；短窗口峰值不等于任意瞬间的硬上限。PSS、cgroup 内存和 RAM 工具文件大小不可相加作为整机内存。

常态、两类事件风暴、恢复期、子进程与各次样本的完整结果见 [本轮性能报告](research/2026-10-04/PERFORMANCE-METHOD.md)。测量绑定当时冻结的运行包；后续冷态诊断和实验结果通道修复未改动被测 C++、服务限制或健康/恢复路径。

早期 Python→C++ 三次迁移对照及其原始数据仍保留在 [迁移报告](research/2026-10-02/CPP-MIGRATION.md)，旧 Python 自动恢复实现的移除见 [清退记录](research/2026-10-02/PYTHON-RUNTIME-RETIREMENT.md)。当前功能、安全与实机启用状态见 [逐项验收报告](research/2026-10-04/ROADMAP-IMPLEMENTATION.md)。

## 支持范围

| 项目 | 当前范围 |
| --- | --- |
| 内核 | 当前验收基线为 Ubuntu `7.0.0-34-generic`；要求 `DM_MPATH_PROBE_PATHS` 接口，不提供旧内核兼容降级 |
| 根盘 | 预先接入稳定 DM 映射的 USB / 线性 LVM / ext4 根系统 |
| 数据盘 | 预先登记的受支持单路径 DM 映射；具体身份与文件系统限制见 [DATA.md](guard/DATA.md) |
| 指令集 | C++ 完整故障实验覆盖 x86_64；ARM64、RISC-V64 已交叉构建并执行用户态检查，尚不代表对应内核的完整恢复验收 |
| 实机证据 | 既有 Python 包曾通过本机根卷/shared 短断恢复；C++ 已于 2026-10-03 重启运行，实际 ELF、稳定映射、读写挂载及基础服务核验通过。C++ 实际短断、睡眠唤醒等仍待验收，块层 WARNING 仍待定位，见 [实机启动记录](research/2026-10-03/HOST-CPP-BOOT.md) |

当前仍有明确边界：永久下层 I/O 阻塞不保证能被取消；掉电丢失的磁盘缓存和已返回应用的 I/O 错误无法撤销；映射恢复不代表文件系统及所有应用均无损。内核崩溃、全局死锁和任意硬件供电故障不在保活保证内。实机历史检查中的块层 WARNING 仍待定位。

Guard 不自动执行文件系统修复或强制读写重挂。EFI 有独立的 [udev/systemd 检查与挂载集成](guard/EFI.md)，不属于根卷 DM 排队保护。RAM 救援终端是共享宿主内核的 root shell，临时日志重启即失。

安全边界：设备身份核验用于防误接和实例确认，不是防克隆设备的认证；RAM/chroot 也不是 root 安全沙箱。启动失败控制台、救援认证、服务权限与安装信任边界见 [攻击面评估](research/2026-10-03/ATTACK-SURFACE.md)。保护启动失败停机、救援提示符超时退出和新设密码检查见 [P0 实现与验收](research/2026-10-03/P0-BOOT-AND-RESCUE.md)。本轮新增权限与可信安装策略后，完整根盘联合验收通过 39 项，普通 Ubuntu 独立数据盘两次启动通过 42 项；最终证据见 [本轮报告](research/2026-10-04/ROADMAP-IMPLEMENTATION.md)。

[ROADMAP](ROADMAP.md) 跟踪安全加固、按需诊断、干净环境复现、数据盘独立接入与实机验收。本轮 P0–P3 软件和 QEMU 验收已落实，普通回归 **400 + 27** 项通过；新管理包和保护镜像已完成 [本机冷态部署](research/2026-10-04/HOST-COLD-UPGRADE.md)，当前仍运行旧实例，下一次保护启动及真实使用观察待验收。逐项证据见 [本轮报告](research/2026-10-04/ROADMAP-IMPLEMENTATION.md)。

## 项目结构

主要目录与入口如下：

```text
External-Memo-Rescue-Handler/
├── README.md                  # 项目介绍与使用入口
├── ARCHITECTURE.md            # 组件职责、复用关系与可替换边界
├── ROADMAP.md                 # 已采纳的下一版本目标与验收方向
├── LICENSE                    # 项目原创内容的 0BSD 协议
├── guard/                     # 自动恢复控制器与系统集成
│   ├── native/
│   │   ├── runtime/           # C++17 恢复核心及原生单元测试
│   │   ├── vendor/            # 随附第三方源码及原始许可
│   │   └── build_runtime.py   # 原生运行时构建入口
│   ├── admin/                # Python 冷态登记、只读查询与校验
│   ├── integration/          # initramfs、systemd 与 udev 配置模板
│   ├── manage.py             # 统一登记、状态查看与维护入口
│   ├── diagnostics.py        # 按需 doctor 与本地脱敏导出
│   ├── package.py            # 无自动激活脚本的离线 Debian 包
│   ├── admin_entry.py        # root 固定入口与代码完整性核验
│   ├── stage_inputs.py       # 已审阅镜像输入的封存
│   ├── ram_environment.py    # 根盘复用或独立数据盘 RAM 工具
│   ├── trusted_paths.py      # 管理文件的 FD 路径与权限检查
│   ├── security_policy.py    # 共用 systemd 权限限制
│   ├── enroll.py             # 根盘身份登记
│   ├── build.py              # 保护启动镜像与运行包构建
│   ├── install.py            # 保护启动入口首次安装与回退
│   └── upgrade.py            # 已有保护镜像的校验、备份与原子升级
├── ram-rescue-demo/           # RAM 手动救援环境
│   ├── src/                  # 救援命令、会话与服务配置
│   ├── tests/                # 手动救援回归检查
│   └── work/                 # 本地构建与登记资料（生成，不提交）
├── lab/                      # QEMU 故障注入、验收与性能测量
│   ├── guest/                # 虚拟机内的初始化、探针与工作负载
│   ├── reproduce.py          # 从公开镜像生成种子并完整验收
│   ├── standalone_data_probe.py # 普通 Ubuntu 的独立数据盘验收
│   ├── roadmap_performance_probe.py # 本轮完整生产包资源对照
│   ├── historical.py         # 从固定 Git 提交提取历史实验对照
│   ├── tests/                # Guard、管理工具及实验工具的回归检查
│   ├── results/              # 纳入版本管理的实验摘要、数据与图表
│   └── work/                 # 本地镜像、构建产物与原始日志（生成，不提交）
└── research/                 # 按日期归档的技术路线、验收与问题报告
```

阅读恢复实现从 `guard/native/runtime/` 开始；复现实验从 `lab/README.md` 开始。目录树仅列主要入口，构建生成的 `work/` 目录不随源码克隆提供。

## 文档导航

| 内容 | 入口 |
| --- | --- |
| 组件职责与实现方式 | [ARCHITECTURE.md](ARCHITECTURE.md) |
| 下一版本与安全边界 | [下一版本目标](ROADMAP.md) · [攻击面评估](research/2026-10-03/ATTACK-SURFACE.md) |
| 安装信任链与本轮实现 | [可信安装](research/2026-10-04/INSTALLATION-AND-TRUST.md) · [逐项落实报告](research/2026-10-04/ROADMAP-IMPLEMENTATION.md) · [本机冷态部署](research/2026-10-04/HOST-COLD-UPGRADE.md) |
| 日常登记、状态查看与维护 | [统一后台维护](guard/MANAGER.md) |
| 根盘构建、安装与回退 | [根盘保护启动](guard/README.md) |
| 数据盘与挂载计划 | [数据映射](guard/DATA.md) · [挂载与子挂载](guard/MOUNTS.md) |
| C++ 源码、构建与接口 | [原生运行时](guard/native/README.md) · [Python 运行时清退](research/2026-10-02/PYTHON-RUNTIME-RETIREMENT.md) |
| 手动救援环境 | [RAM 救援终端](ram-rescue-demo/README.md) |
| 可重复故障实验 | [QEMU 实验室](lab/README.md) · [完整 Ubuntu](lab/UBUNTU.md) · [恢复事务](lab/TRANSACTIONS.md) |
| 性能、资源与架构验证 | [C++ 迁移报告](research/2026-10-02/CPP-MIGRATION.md) · [架构范围](guard/ARCHITECTURES.md) |
| 技术路线与历史研究 | [技术路线](research/2026-09-25/TECHNICAL-ROUTE.md) · [版本对照](research/2026-09-25/VERSION-STUDY.md) · [Python 优化](research/2026-10-01/OVERNIGHT-OPTIMIZATION.md) |
| 实机接入与问题记录 | [保护启动接入](research/2026-09-30/HOST-GUARD.md) · [拔插后检查](research/2026-09-30/HOST-POST-RECONNECT-AUDIT.md) · [EFI 处理](research/2026-10-01/EFI-RECOVERY.md) |

仓库跟踪源码、配置模板、测试、说明及可发布的实验摘要。虚拟磁盘、构建产物、设备登记资料和原始故障日志保留在本地，由 Git 忽略；克隆仓库不包含可直接部署的运行镜像。

## 开源协议

本项目原创内容采用 **[0BSD（零条款 BSD）](LICENSE)** 协议，版权署名为 `2026 zkdsgcxb`。允许自由使用、修改、商用及再分发，不要求衍生作品公开源码或保留署名；软件按原样提供，不作担保。标准协议说明见 [Open Source Initiative](https://opensource.org/license/0bsd)。

第三方组件保留各自的许可证和版权声明。随附的 nlohmann/json 使用 MIT 协议，来源与授权见 [第三方依赖说明](guard/native/vendor/README.md)；构建生成的救援镜像包含的 Linux、系统工具和共享库仍遵循各自的许可证。
