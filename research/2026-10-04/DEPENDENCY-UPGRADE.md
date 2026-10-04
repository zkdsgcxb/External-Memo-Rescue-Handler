# P1/P3：真实依赖更新、冷部署与回退

## 方法和信任边界

本实验保持 C++ Guard 可执行文件、管理代码和 systemd 生产策略一致，只更换真实发行版的 `libcrypto.so.3`。A 来自 Canonical 签名的 Ubuntu 24.04 cloud rootfs，B 来自当前构建机已安装的更新包。准备阶段重新验证签名校验列表和整个归档，读取归档内真实库和 dpkg 状态记录，不通过修改文件字节或版本字符串伪造更新。

| 输入 | A | B |
| --- | --- | --- |
| Debian 包 | `libssl3t64:amd64` | `libssl3t64:amd64` |
| 版本 | `3.0.13-0ubuntu3.15` | `3.0.13-0ubuntu3.16` |
| 库 SHA-256 | `6a66c3ba6b3749aacc9497973fff00f6ba61c703ba7015d0d7ab3fd9510974b6` | `6866ed711dab8927e1bf82bf1cd30e828bdfc34f3cc1e83b1c9eb01ce6c37d9a` |
| 文件大小 | 5,309,400 bytes | 5,309,400 bytes |

Canonical 归档 SHA-256 为 `2ea580b9dd2d0e97ba9047798becc63a94427679876dec193221e345a6e0cdc9`；系统 cloud-image keyring 验证通过。B 的 dpkg 元数据只是当前系统来源说明，不独立等同于对该已安装文件的发行版签名认证。

ELF class、byte order、machine 和 SONAME 均一致；当前 Guard 在 `LD_LIBRARY_PATH` 指向真实 A 库且 `LD_BIND_NOW=1` 下执行 `--version` 成功。此检查只说明动态链接可完成；恢复行为仍须由 VM 验收。两个包内基础工具与 native 闭包的散列、包来源均指向实际库字节；prepare 还比较两个包的管理代码逐文件散列，若构建期间源码变化则拒绝生成可执行 fixture。

## 三次启动的验收流程

1. 普通 Ubuntu 启动，确认没有根盘 Guard。安装 A 并登记两个实验数据映射；观察实际控制器启动，执行一次断联恢复。
2. 有序停止两个控制器，正常移除两个没有挂载或应用工作负载的数据映射，满足生产 `manager upgrade` 的静止条件。安装 B 并运行 `manager upgrade`；要求登记未变、当前 RAM inode/内容未变、没有启动新服务、结果明确要求下次启动生效。
3. 第二次启动核对实际 B 进程及库，执行断联恢复。用同样静止条件重新安装 A 并运行 `manager upgrade`，复用经过完整核对的既有版本目录；拒绝损坏或缺失的旧版本。
4. 第三次启动核对 A 已回退，执行断联恢复，再正常停止并关机。

观察器读取 `/proc/<PID>/exe` 和 `/proc/<PID>/map_files/<range>` 的实际 inode 散列，避免仅检查磁盘候选路径就声称当前进程用了新库。三次 boot ID 必须不同，登记内容必须一致。VM 无网卡、无宿主块设备、无共享目录；所有写入只发生在可丢弃 overlay 和测试盘。

入口为 `lab/dependency_upgrade_probe.py prepare` / `run`。准备不启动 VM；命令细节见 [实验室 README](../../lab/README.md#真实依赖更新与冷回退)。

## 当前证据状态

准备和离线审核已通过；针对有界归档读取、来源状态、离线 resolver 恢复及实际映射散列判定的 6 项测试通过。namespace/fstab 冷绑定、最终 `ReadWritePaths=+/run +/dev` 策略、部署锁、root 私有登记输出和普通用户 bundle 边界修正后的 fixture 为 `lab/work/dependency-ab6-1004/fixture.json`，其 A/B 管理包版本分别为 `010fa96351442907b8dd71c1` / `ca84b65b5441bca23af24aa9`，共用 C++ SHA-256 `479c8ba7e2445ae3fbb99d58e1b79d836170c6fe463953ca636cde9857ee8718`。A/B6 包含冻结后的管理说明文档，与打包后的工作树 74 个源码成员逐文件核对没有漂移；此前 A/B1–5 输入均保留，不用于最终验收。

普通用户全量回归为 lab **387/387**、RAM **27/27**，日志分别为 `lab/work/repro-agent-final5-lab-tests-1004.log` 与 `lab/work/repro-agent-enrollment-final-ram-tests-1004.log`。最后收紧的 bundle 普通用户边界随后单独通过 16 项 stage-inputs 测试（`lab/work/repro-agent-final6-stage-tests-1004.log`）；依赖实验的 6 项测试再次通过。这些测试没有启动 QEMU 或访问宿主块设备。

新增 `test_fstab_pin.py` 的 9 项测试使用真实普通用户可信路径检查，仅模拟 mount 系统调用：已有旧 inode 或 rw 挂载拒绝重用，原位修改仍沿用同一 inode，bind/只读 remount 失败按顺序回滚，bind 期间的原子替换被拒，非可信或超大 fstab 在挂载前拒绝。没有为测试放宽生产 UID、权限、链接和大小边界。

## 三次真实启动结果

最终 A/B6 在 `lab/work/dep-vm6-1004/report.json` 完成 **14/14 检查**，`passed=true`，全部输入散列保持不变。公开摘录见 [依赖生命周期验收 JSON](../../lab/results/2026-10-04-dependency-upgrade.json)，包含原始报告散列、74 个管理源码成员散列、包/内核/运行库散列、真实进程映射和每次恢复状态。运行前再次核对 74 个生产源码成员没有漂移。准备 fixture 仍保留 `passed=false`；只有执行后的报告才表示验收结果。

| 启动 | 独立 boot ID | 两个控制器实际加载的 libcrypto | ext4 USB 断联恢复 |
| --- | --- | --- | --- |
| 第一次 A | `47231c57-3a88-4fc5-a848-3b3a0e399db8` | `.15`，SHA `6a66c3ba…` | `ready`，`recoveries=1`，内核探测完成 |
| 第二次 B | `f7fd8c74-ff81-4477-9400-bd215300dd2c` | `.16`，SHA `6866ed71…` | `ready`，`recoveries=1`，内核探测完成 |
| 第三次 A | `2326074e-95ba-41fd-9032-9d27b020c012` | `.15`，SHA `6a66c3ba…` | `ready`，`recoveries=1`，内核探测完成 |

三次均没有根盘 Guard；两个数据控制器均运行同一 `479c8b…` C++ ELF。上表来自实际 `/proc/<PID>/map_files` 映射 inode 的散列，而非只读包内候选路径。vfat 映射作为第二个登记对象持续保持 `ready`，本实验没有拔插它。每次 ext4 拔插设定最少 0.2 秒间隔，切换后的 DM 表指向重新枚举的 `/dev/sdd1`，并完成内核探测；没有把设定间隔当作整个恢复用时。

A → B 和 B → A 均在正常停止控制器、移除未挂载的映射后执行。两次 `manager upgrade` 都返回 `requires_reboot=true`、`runtime_prepared=false`、`services_started=[]`，当前 RAM 的库 inode/内容和 native manifest 散列保持原值，两个登记文件散列始终一致。下一次启动才加载目标版本，第三次真实加载 A 证明冷回退完成。三次均正常关机；Ubuntu 的 snapd 在停止时走默认约 90 秒时限，这部分等待不是恢复性能。

该实验使用同 ABI 的真实共享库更新，验证依赖重建、静止部署、重启生效与回退链，不覆盖跨 ABI 升级，也没有挂载文件系统或启动应用读写工作负载，因此不能从中推导应用数据无损或物理 USB 可靠性。种子既有的可选 `binfmt_misc` 模块挂载失败仍保留记录。没有更改生产服务策略，也没有在宿主安装包或操作物理块设备。
