# 旧 Python 自动恢复运行时清退

日期：2026-10-02。本次将自动恢复收敛为 `guard/native/runtime/` 中唯一的 C++17 实现，删除当前工作树里的 `guard/runtime/`，并整理管理工具与历史实验的依赖。本次只修改源码和可丢弃的 QEMU 实验环境，没有更新实机保护启动包、服务或磁盘映射。

## 保留与移除的职责

| 内容 | 清退后的归属 |
| --- | --- |
| 健康监视、故障准入、DM 换表、路径探测、截止时间、事务记录、死亡接管 | 只由 C++ 自动恢复运行时负责 |
| 根盘保护启动、数据盘运行时接入 | C++ `activate` / `maintain` 入口 |
| 登记、布局与配置校验、只读设备查询 | Python `guard/admin/`，由管理命令按需调用 |
| 构建、安装、状态查看、挂载计划、EFI 集成 | 现有 Python 管理工具 |
| 手动 RAM 救援、日志、实验编排与报告 | 保留已有工具，不因自动恢复清退而删除 |
| Python 自动恢复与旧实验对照 | 固定 Git 历史快照，只用于实验复现 |

`guard/admin/` 保留的是管理命令需要的冷态功能：`identity.py` 提供只读 USB/LVM/文件系统身份查询，`admission.py` 负责持有 FD 的身份与布局复核，`data.py` 检查登记映射及挂载/swap/fstab 隔离，`registry.py` 校验登记结构，`dm.py` 与 `linux_abi.py` 提供只读 DM/块设备接口。这里不包含后台监视线程、恢复状态机、运行时 journal、路径 probe 或修改 DM 的自动恢复流程。

生产构建不再提供 Python 运行时选择。原生 ELF、exec 入口、动态库和清单必须完整并通过散列校验；不能因文件缺失而回退到另一份控制器。仍运行旧 Python 保护包的会话不能使用新入口新增数据盘维护实例，应先独立安排原生镜像安装与启动验收。本次没有进行这一实机升级，也不在已有 owner 下替换运行包。

## 历史实验仍可复现

[统一历史入口](../../lab/historical.py) 从本地 Git 的固定提交提取完整实验快照，默认 Python 基线为 `e745e5e4b9cde4ffd21d03f6e45a491ca8400083`。快照位于 `lab/work/history/<提交>/`，使用源码校验约束复用；不切换当前工作树、不自动联网获取提交、不把旧恢复模块重新放回生产目录。

```bash
python3 lab/historical.py lab/build.py --help
python3 lab/historical.py lab/auto_run.py --help
python3 lab/historical.py lab/architecture_probe.py --help
```

迁移后旧 Python 包体积对照另使用固定提交 `1f887fe3eca3a6089f3fec28cd30ceeb9ce20048`；历史入口只接受这两个完整提交标识。快照的 `lab/work` 链接到当前项目产物目录，避免复制已有大镜像。

本地必须具有指定提交；缺失时明确报错。旧 runner 的直接命令保留转交入口，原报告中的实验参数可以继续使用，但新结果要明确标注历史提交。当前 `cpp_guard_probe.py` 的 Python 选项同样只用于固定基线对照；生产构建只有 C++。

当前原生挂载、连续恢复、事务与打包集成继续由 `cpp_guard_probe.py`、`cpp_transaction_probe.py`、`cpp_integration_probe.py` 调用。实验使用历史夹具时，区分夹具与被测运行时：原生组必须核验虚拟机实际 `/proc/PID/exe` 的 ELF 散列，不能把旧 Python 的成功结果算作 C++ 恢复成功。

已有 `research/` 历史报告和 `lab/results/` 证据保留原貌。2026-10-02 的 [完整迁移与性能对照](CPP-MIGRATION.md) 仍描述当时实际运行的两个版本；本次代码清退本身不是一次新的性能测量，不能直接沿用旧数字宣称本次额外降低了 CPU 或内存。

## 本轮验证

旧目录的 11 个源码文件已移除，必要的冷态功能抽到 `guard/admin/` 的 7 个模块。8 个仅测试旧 Python 自动恢复实现的测试文件随之删除；身份、设备实例、几何与布局、DM 表规范化、挂载/swap/fstab 隔离和管理包完整性检查保留并适配。测试数量减少源于删除旧实现的专用检查，不作为性能或覆盖率提升的证据。

| 验证 | 本次结果 |
| --- | --- |
| 当前 Python 管理、实验与回归检查 | 214 项通过，包含历史快照创建/复用/篡改拒绝及实际管理包独立导入 |
| 手动 RAM 救援回归 | 15 项通过 |
| 原生检查 | 核心 60、准入 37、控制器 13，共 110 项重新执行通过 |
| 当前 C++ 生产包的完整 Ubuntu 集成 | 32/32；实际启动模板、管理包、三盘恢复、子挂载、原 FD、错误身份、只读策略和超时终态 |
| 从固定 Git 提交取得的 Python 挂载对照 | 28/28；证明历史实现清出工作树后仍可实际复现 |
| C++ `after_commit` 死亡接管 | 完整 Ubuntu 中实际 systemd `ExecStopPost`，11/11；无第二 owner、残留 inactive 表、永久 suspend 或迟到 ready |

本次 C++ 源码和 ELF 均未改变；重新核对构建清单中的原生源码散列后运行单元检查。QEMU 核验实际控制器 ELF，SHA-256 仍为 `72d9d550079b4f73e1954e13f99fa614826758c623eec1d3026aa22bc891b21f`。生产集成与 Python 挂载对照均正常关机，ext4/FAT 离线只读检查返回 0，基础镜像和被测源码保持不变。

实际执行入口：

```bash
python3 -m unittest discover -s lab/tests -v
python3 -m unittest discover -s ram-rescue-demo/tests -v
lab/work/cpp-runtime/core_test
lab/work/cpp-runtime/admission_test
lab/work/cpp-runtime/controller_test
python3 lab/cpp_integration_probe.py
python3 lab/cpp_guard_probe.py --implementation python --scenario mounts
python3 lab/cpp_transaction_probe.py kill --stage after_commit --guest ubuntu
```

前置构建与种子镜像沿用 [C++ 验收方法](CPP-VM-VALIDATION.md)，均为可丢弃的实验环境。原始报告分别位于：

- 生产集成：`lab/work/cpp-int-1002-203411-313505/report.json`。
- 历史 Python 对照：`lab/work/cpp-pm-1002-203720-332619/report.json`。
- 原生死亡接管：`lab/work/20261002-203717-tx-kill-3-332641/report.json`。

可发布的检查、源码/ELF 散列和原始报告 SHA-256 见 [验收证据](../../lab/results/2026-10-02-python-runtime-retirement.json)。单元日志及原生检查摘要保存在 `lab/work/python-retirement/`。

调试过程中发现旧 `.gitignore` 的目录规则不匹配历史快照新增的 `lab/work` 符号链接，导致第二次复用快照被误判为未跟踪源码。修复为先验证链接唯一目标，再只排除这个输出链接；其他未跟踪源码仍被拒绝。第一轮生产集成的 32 项功能检查虽通过，但运行期间仍有源码修改，`sources_unchanged=false`，因此不计最终验收；冻结源码后已完整重跑通过。该轮失败报告保留于 `lab/work/cpp-int-1002-202648-292513/report.json`，并列入证据的排除记录。

实机只读检查仍为原 Python 服务 `active`、PID 683。本轮未安装原生包；所有 QEMU 实验均已结束。

## 未改变的运行边界

I/O 排队仍由内核 DM multipath 承担。C++ 控制器继续要求预先登记和稳定映射，保留唯一 owner、完整身份链、最终复核、原期限与死亡接管合同。永久下层阻塞、已返回应用的 EIO、已经损坏的文件系统和掉电丢失的缓存不会因删除 Python 代码获得额外恢复保证。

源码清退不等于部署清退：实机继续运行此前已安装的包。原生包的实机安装、正常启动及短断恢复验收需要单独完成。
