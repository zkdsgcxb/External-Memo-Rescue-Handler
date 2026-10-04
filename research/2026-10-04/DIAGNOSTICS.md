# 按需诊断与本地脱敏导出

2026-10-04；2026-10-05 补齐安装收据生命周期与普通数据管理候选核对。对应 [ROADMAP](../../ROADMAP.md) 的 P2 诊断交付。实现位于 [guard/diagnostics.py](../../guard/diagnostics.py)，既可由统一管理命令调用，也可直接运行：

```bash
python3 guard/diagnostics.py doctor
python3 guard/diagnostics.py export
python3 guard/diagnostics.py export --output /已有或新建私有目录/report.json
```

`doctor()` 返回结构化字典，`export_report(output=None)` 返回导出路径、字节数和权限；管理入口可以直接调用两者，不需要启动恢复进程。诊断没有常驻服务、定时器或网络上传。普通用户可以运行；不能读取 root 私有状态时显示 `unknown`，不会把权限不足当成未安装。完整系统诊断仍需要用户通过本地认证运行可信安装入口。

## 状态含义

| 状态 | 判据与范围 |
| --- | --- |
| `not_installed` | 未见有效安装记录或本次启动中的运行配置；卸载收据不会被当成安装成功 |
| `installed_not_running` | 已安装但尚无运行配置，或相关服务未运行 |
| `ready` | 服务 PID、当前 boot ID、事务与事件 owner epoch、映射身份、PID 启动时间及实际进程 root 均相符；事件为 ready，事务稳定，当前映射有未暂停的 multipath 活跃表且无待提交表 |
| `recovering` | 同一当前 owner 报告等待、验证或探测阶段 |
| `refused` | 当前 owner 拒绝接入候选；不是“现有应用一定已经收到 I/O error”的判断 |
| `failed` | 服务失败，或本次控制流程进入过期、中断、阻止、失败等终止状态 |
| `unknown` | 权限不足、输入不可信、字段不完整、不同启动/owner、采样时归属发生变化，或证据不能支持上述结论 |

根盘 `removed`、首次安装失败回退的 `failed_rolled_back` 和管理器 `uninstalled` 收据均不算已安装。`preparing` / `installing` / `upgrading` 等未完成变更使总体状态为 `unknown`，`upgrade_failed` 为 `failed`；设备条目仍独立报告当前 owner 的运行状态。撤销下次启动入口不会结束已在 RAM 运行的旧 owner，因此有效运行证据仍可使 `installed=true`，同时保留已撤销的收据状态。

旧 `path-state.json` 中单独一条 `ready` 不足以证明当前保护正常。诊断核对 journal 的 boot ID、map name/UUID、owner PID/epoch，并比较 `/proc/PID/root` 的实际设备号/inode 与固定的保护启动或独立数据 RAM 根目录。事件时间必须不早于 systemd 记录的 `ExecMainStartTimestampMonotonic`，与 C++ journal 使用同一 MONOTONIC 时钟；不把当前睡眠时差套到历史 `/proc` 启动时间上。读取进程前后比较其 start ticks，采集结束后再次读服务 PID/ActiveState/启动时间和事务，检测到变化则降为 `unknown`。

这仍是有时间差的快照，不是原子健康证明。`ready` 不验证底层驱动当前是否卡住，不检查文件系统一致性、应用存活或每个 I/O 的最终结果。事件中的 in-flight 数是**该条事件时**的值，不当作当前测量。

## 采集范围与脱敏

只读取固定根盘配置、显式登记或临时接入的数据盘配置、对应服务的选定 `systemctl show` 属性、只读 libdevmapper 映射快照和 Guard 结构化事件。不会枚举并读取任意 USB 磁盘，也不会改变 DM、接入范围、挂载或事务。

导出保留每个选定控制器最近最多 64 条事件、当前事务阶段、拒绝原因分类、实际进程 ELF 哈希、安装候选和当前 RAM 副本的版本信息，以及 systemd 权限/资源配置。拒绝原因按已知文字特征分类，未知原因保留关联 token，不原样抄入 helper 输出。**不导出通用 systemd journal、内核全文日志或任意扩展字段**；因而不能从这份包声称已排查 ext4、应用或其他服务错误。

设备名、UUID、owner、节点路径和自由错误文字使用每份报告独立随机密钥的 HMAC token；同一字符串在同一报告中可关联，跨报告不能靠固定散列直接关联。登记 JSON 中的 identity 部分不投影到报告；不另读 `identity.json`，也不查询 USB 序列、主机名或用户数据库。凭据、shadow、密钥文件从不打开。未知 JSON 字段直接省略，不依赖容易漏掉新字段的全量输出后正则替换。包名/版本/架构、SHA-256、固定状态枚举及有限数值属于明示保留的技术字段。

诊断区分实际运行的 ELF、当前 RAM 副本和安装收据中的下一启动候选。依赖检查覆盖 **C++ Guard 原生库闭包，以及 `base-runtime.json` 登记的基础 ELF 工具/共享库**：逐一核对冻结文件 SHA、当前宿主同一路径 SHA 和清单记录的 dpkg 包版本。宿主包已升级不代表 RAM 副本已经升级。两个版本字段相同也不代替文件散列比较。基础包包含 BusyBox、Bash、Python、LVM、blkid、dmsetup 等 ELF 和相关扩展库；纯 Python 文件等非 ELF 内容不逐文件比较。旧包缺少清单时明确给出 `base_manifest_present=false`。

`versions.installed_candidates` 分别列出 `root` 和 `manager`；只有对应收据为 `installed` 才展示候选，撤销或失败回退收据中的历史镜像/ELF SHA 不再作为已安装候选。数据管理候选只允许从 `/usr/local/lib/ram-rescue-manager/<64 位散列>/guard/manage.py` 所在的可信版本目录读取，核对文件权限并计算该脚本散列，随后读取有界 `runtime/manifest.json`，不执行脚本、不展开归档。每份 RAM 副本分别与两种候选比较完整原生清单和基础包 SHA，因此同 ELF、不同冻结库也会显示不匹配。归档 SHA 来自可信清单，`archive_integrity_checked=false` 明示此按需检查没有重新读取大归档；旧安装缺少清单时对应候选为空，不能据此声称未安装。

独立数据环境通过固定 `/run/ram-rescue-manager/rootfs` 读取；根盘环境通过固定 `/run/ram-rescue-demo` 读取。诊断不跟随管理器的 `tools` alias 去任意目标，也不把运行环境收据当成实际进程 root 的证明。

## 输入与导出边界

- 配置/状态复用 `trusted_paths.py` 的可信读取，逐级用 `openat` 语义固定目录 FD，拒绝符号链接、错误 owner、组/其他用户可写目录、多重硬链接及非普通文件。日志/库使用同样边界。发行版 merged-/usr 的宿主库链接只允许最终落在库目录，随后仍用固定 FD 路径读取；链接到 `/etc/shadow` 等路径会被拒绝。
- 最多选择 64 个控制器；每个 JSON 输入上限 256 KiB，每个结构化事件文件上限 1 MiB，每个 ELF 文件上限 32 MiB，原生库闭包最多 64 项、基础工具/库最多 256 项，导出 JSON 上限 2 MiB。超限不继续扩张；每个外部只读查询有 4 秒和 32 KiB 输出限制。查询按需串行进行，不新增常驻 CPU/内存消耗。
- 默认在当前目录新建随机后缀的 `0700` 私有目录及 `0600` JSON。指定路径时父目录必须由执行用户拥有且为 `0700`；不修改既有目录权限、不覆盖既有文件。
- 导出使用固定目录 FD 和排他创建，拒绝 symlink。写完再次验证目录身份；期间路径被替换则删除本次固定目录内的产物并报错，不将替换后的路径宣称为成功。

SHA 一致性不是发布者认证，诊断不是完整安全审计。序列号/UUID 可被复制；防误接、设备实例一致性与对抗恶意硬件真实性认证仍是不同能力。

## 验证记录

本次新增 28 项无特权测试，覆盖当前/过期 boot、owner/PID 复用、服务启动时间、错误实际 root、单一登记范围、暂停/错误 DM 表、不同故障状态、独立数据盘、异常类型和非有限数字、恶意字符串/未知字段、关联脱敏、基础工具与原生库版本/散列差异、部分包缺失时保留其余版本结果、父目录权限/owner/symlink/FIFO、读写路径替换、大小上限、不覆盖导出及默认/指定私有路径。全部通过。

2026-10-05 定向回归扩展为 **38/38，通过**。新增 10 项覆盖真实安装/卸载/失败/升级收据、仍在 RAM 的 owner、失效候选清空、根盘与数据盘独立候选、同 ELF 不同库/基础包的匹配结果，以及候选路径、父目录、符号链接、清单边界和失效收据不触达候选文件。新增诊断功能已用最终 v9 包单独复验，不能把前次 v8 VM 结果当成已执行这些新字段。

本轮普通 `lab/tests` 回归在该时间点为 **287/287，通过**，日志位于本地 `lab/work/diagnostics-regression-20261004.log`。此数字包括同时进行中的其他改动，最终项目汇总以主验收报告为准。随后诊断小改动、可信读取复用及基础工具清单接入后的 28 项定向回归也已通过。

另以普通用户实际运行了 `doctor` 和 `export` CLI：因不能读取 root 私有状态，正确返回 `unknown`；本地导出 1,556 字节 JSON，文件/父目录为 `0600`/`0700`。未申请提权、未部署、未操作实际块设备。该项不代替特权实机和完整 QEMU 集成验收。

随后在普通 Ubuntu VM 的真实包接入中，控制器启动失败时 doctor 正确返回 `failed`（`lab/work/sd-1004-222058-304607/report.json`）。该次检查已实际读取独立 RAM 环境的 11 项原生依赖和 100 项基础 ELF，冻结 SHA 全部符合清单；同时识别到种子系统与冻结包中 `libssl3t64`、`libexpat1`、`libsqlite3-0` 的实际版本及文件差异。这里的差异不代表运行库已被热替换；服务仍配置为从冻结包启动。完整成功接入、当前 owner 和脱敏导出的集成结果见 [普通 Ubuntu 数据盘验收](STANDALONE-DATA.md)。

修复实际服务启动问题后，`lab/work/sd-1004-232607-652502/report.json` 的完整普通 Ubuntu 验收通过 42/42。doctor 在初次就绪、拔插恢复后、再次冷启动三个时点均核对到两个实际 owner 并返回 `ready`；前后两次真实导出为 99,552 / 103,438 字节，文件/父目录权限符合 0600/0700，测试盘序列、UUID、设备名与 VM 主机名均未泄漏。源码单测的陈旧 boot/owner、权限和异常输入对照仍承担其对应边界，正常 VM 通过不替代这些负向检查。

最终 v9 包的 `lab/work/sd-1005-000949-799661/report.json` 再次通过 **42/42**，三个原有 doctor 判据同时验证新增字段：根盘候选为空，实际安装的数据管理 ELF/base/archive SHA 与包相同，当前 RAM 完整原生清单和基础包 SHA 均匹配。两次真实脱敏导出为 100,385 / 104,661 字节，文件/父目录仍为 0600/0700。最终包 SHA256 为 `dc1829017a88c3812deaeeefef368231f4678d206cfda14d94888b1d79aa02ba`；报告 SHA256 为 `98c3e99154e09f2c7e30537aa07a72375c6139fe160beddf3f72b54f97af9256`。普通 VM 没有刻意破坏已安装收据，卸载、失败回退及异常候选的负向覆盖仍来自上述定向测试。
