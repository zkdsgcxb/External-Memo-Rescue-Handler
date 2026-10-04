# 普通 Ubuntu 中独立维护登记数据盘

对应 [ROADMAP](../../ROADMAP.md) 的 P3。实现继续使用同一个 C++ 控制器、稳定 DM 映射、接入核验和有界恢复事务；独立环境没有新增第二套恢复状态机。

## 接入边界

普通 Ubuntu 不必先部署受保护根盘 initrd。用户明确建立受支持的单路径 multipath 映射后，通过安装好的 root 所有管理入口登记；管理器在需要时准备独立 tmpfs,noswap 工具目录 `/run/ram-rescue-manager/rootfs`，数据控制器以这个真实目录作为 `RootDirectory`。匹配的根盘救援环境已存在时通过 bind 挂载复用同一份 RAM 文件；`tools` alias 仅供管理代码定位。不会自动把任意 USB 设备纳入维护范围，也不负责替用户建立生产磁盘映射、分区、格式化或挂载。

已有 DM 环境还必须显式配置有限的内核无路径超时，至少比数据控制器接入预算多 2 秒。本实验为 8 秒预算配 10 秒内核值；默认 0 的普通内核在本轮对照中被正确拒绝。此参数影响系统 DM multipath 的全局策略，管理器不会暗中替用户改写；应先评估同机其他 multipath 使用者，再由管理员配置。本 VM 在专用启动参数里设置 `dm_multipath.queue_if_no_path_timeout_secs=10`，其中只有两条可丢弃数据映射使用 multipath。

安装包没有 maintainer scripts，`dpkg` 安装不会自动启动控制器、登记磁盘或改写引导配置。显式 `manager install`/`register` 才建立管理集成与登记关系。生产说明和权限边界见 [管理说明](../../guard/MANAGER.md) 与本轮主验收报告。

## 可复现实验

实验入口是 [lab/standalone_data_probe.py](../../lab/standalone_data_probe.py)。必须先完成 [lab/reproduce.py](../../lab/reproduce.py) 的干净 Ubuntu 种子创建，以及 [guard/package.py](../../guard/package.py) 的当前源码离线包构建。

```bash
python3 guard/package.py \
  --output lab/work/standalone-package \
  --base-rescue-dir lab/work/REPRO/base-rescue \
  --native-binary lab/work/REPRO/protected/native-runtime/guard-runtime

python3 lab/standalone_data_probe.py \
  --reproduction-dir lab/work/REPRO \
  --package-dir lab/work/standalone-package
```

宿主以普通用户运行，不需要 sudo。入口只接受 `lab/work` 内的普通文件并检查种子/包散列；QEMU 没有网络、共享目录或宿主块设备。根盘种子为只读 backing，写入发生在新 qcow2 overlay。ext4、FAT、错误身份盘和占位盘均为新建的可丢弃文件。

启动使用普通 initrd，不含 `ram_rescue_guard=1` 和 `nompath`；若传入已有根盘保护 hook 的 initrd，会拒绝运行。仅附加带 QEMU DMI 和专用启动参数双重门禁的测试 observer。其串口协议只接受有大小限制的预定义动作 JSON，不提供 `eval` 或交互 shell。被测生产服务使用包内原有启动入口和限制配置。

场景包括：

1. 确认普通 Ubuntu 启动、根盘 Guard 和保护 RAM 环境不存在；安装真实 `.deb`。fixture 仅向可丢弃 guest 的 `/boot` 放入本次真实 kernel 与普通 initrd，通过已安装的 `enroll-root --name vm-root-enrollment` 核验实际 USB/LVM 根盘、0700/0600 权限、三文件 SHA 和标准 tar 流；确认登记没有修改这些 boot 输入或原映射。随后通过同一可信入口独立准备环境和登记两数据盘。
2. 确认两个实际 C++ ELF 和当前包相符，没有服务命令 override；核对实际进程 root、doctor 状态和私有脱敏导出。
3. 通过现有 mount renderer 创建 ext4 automount、嵌套 FAT、主 bind 和两个子 bind。四个进程保持原文件描述符，持续写入、fsync 和 O_DIRECT 读回。
4. 约 0.2 秒 ext4 拔插并插入未登记占位盘，促使设备编号变化；FAT 用错误文件系统 UUID 的盘短暂冒用同一传输序列，然后恢复原盘。
5. 验证原进程、mount ID/选项、子挂载和已确认数据均保持连续；显式只读后再次重连，确认没有擅自改回可写。
6. 正常停止消费者、卸载和关机。关闭 VM 后对数据盘提取副本运行 `e2fsck -fn` / `fsck.fat -n`，核对种子、包、原 initrd 和内核输入未变。
7. 同一 overlay 再次冷启动，由 VM fixture 创建测试映射；已有持久登记自动准备独立 RAM 环境并启动两个新 owner，核验新 boot ID 和 doctor 状态后正常关机。

资源采样单独记录两个数据 Guard 所属 cgroup 的 20 秒常态窗口、100 ms 窗口 CPU 峰值和累计 CPU 均值，并记录进程 PSS 快照。CPU 100% 表示一个核心；cgroup memory 与 PSS 不是同一指标，不相加。恢复阶段资源比较由本轮性能报告覆盖。

## 当前验证记录

新增 8 项普通用户测试已通过，验证普通启动参数、隔离磁盘 wiring、当前源码 observer、双门禁、禁止把保护 initrd 当作普通 initrd、拒绝宿主路径/链接/目录输入以及种子/包散列，并对实际 namespace 的可写根、缺失可写目录和安装候选不匹配做负向检查。

2026-10-05 最终 v9 实际验收 **42/42 通过**，公开摘要见 [2026-10-04-standalone-data.json](../../lab/results/2026-10-04-standalone-data.json)，完整本地记录为 `lab/work/sd-1005-000949-799661/report.json`（SHA256 `98c3e99154e09f2c7e30537aa07a72375c6139fe160beddf3f72b54f97af9256`）。两次启动使用同一普通 Ubuntu overlay，均未启用根盘 Guard；第一次通过真实 `.deb` 和固定管理员入口安装、登记，第二次由持久登记自动建立新 owner，boot ID 已改变。两次正常执行关机，数据盘副本 `e2fsck -fn` / `fsck.fat -n` 均返回 0，种子、原 initrd、内核和包的输入 SHA 全部未变。

被测原生 ELF 为 `479c8ba7e2445ae3fbb99d58e1b79d836170c6fe463953ca636cde9857ee8718`，实际两个控制器的 `/proc/PID/exe` 均匹配，使用包内完整权限限制，没有服务 drop-in。包为 `ram-rescue-handler_2f5bf9a687263df093e11293_amd64.deb`，SHA256 `dc1829017a88c3812deaeeefef368231f4678d206cfda14d94888b1d79aa02ba`，生产源码提交 `6b8289c4d03a63cfa677b079108561adfbcdc73f`；通用基础归档 SHA256 为 `cdc8ffb3060ffd198265bbdeaefc6122866459c2e57adba1372db6a430d87cee`。v9 保留共用部署锁、回退可信读取、root 私有登记输出和拒绝特权 bundle 构建，并补齐诊断收据生命周期与数据管理候选字段。三个原有 doctor 判据已加强为实际安装候选/运行副本一致性检查，因此顶层计数仍为 42；本表只绑定以上实际受测产物。

| 实际场景 | 观察结果 |
| --- | --- |
| ext4 快速断开并用陌生盘占位 | QMP 实测断开 0.25197 秒，原路径 `sdc1` 变为 `sde1`；原进程和 FD 继续工作 |
| FAT 错误盘冒用同一传输身份 | 错误文件系统 UUID 被拒，恢复次数保持 0；换回原盘后恢复为 1，原挂载未撤销 |
| 两主工作进程与两个 bind 子工作进程 | 全部原 PID/start time 保持，未收到 I/O error，所有已确认写入的最终内容校验通过 |
| 已安装的根盘登记入口 | 真实 seed PV 身份与 ubuntu/shared LV 匹配，私有三文件和 tar 流 SHA 相符；登记过程未改 boot 输入或映射 |
| 实际 I/O 等待 | ext4 最长写入+fsync+O_DIRECT 读回为 2.00066 秒；FAT 错盘换回阶段为 2.78687 秒，短于断盘预算但并非零暂停 |
| 挂载与只读 | 5 条挂载路径的 mount ID/选项保持；显式改只读后再重连仍为只读，没有强制改回可写 |
| 实际挂载限制 | 两次启动均从两个实际控制器的 `/proc/PID/mountinfo` 核对 `/` 为 ro、`/run` 和 `/dev` 为 rw，并核对 ELF 与进程 start time；不把 unit 文本当成生效证据，也不声称其它隐式挂载全为只读 |
| 独立维护边界 | 原普通根卷映射未变；未登记占位 USB 没有被接管；两个数据 owner 都处于独立 RAM 环境 |
| doctor / export | 初始、恢复后及第二次启动均为 `ready`，实际 owner 匹配；根盘候选为空，数据管理候选 ELF/base/archive SHA 与包一致，当前 RAM 的完整原生清单及基础包均匹配；两次导出为 100,385 / 104,661 字节，文件 0600、目录 0700，序列/UUID/主机名/设备名泄漏检查通过 |
| 冻结依赖 | 11 项原生库和 100 项基础 ELF 的冻结 SHA 全匹配；准确显示种子中 libssl、libexpat、libsqlite 与冻结包之间的实际版本/文件差异 |

20.092 秒的常态观察包含 201 个样本，两个数据 Guard 合计 CPU 均值 **0.06076%**，100 ms 窗口最大 **0.71299%**，100% 表示一个核心。两者 cgroup 内存均值 **3.531 MiB**、峰值 **3.758 MiB**，进程 PSS 合计快照 **10.227 MiB**，独立工具 tmpfs 实际使用 **88.176 MiB**、上限 256 MiB。这些内存口径有重叠：工具文件可由准备服务计费，而运行进程仍映射相同页，不能相加。短窗口峰值也不是最坏情况保证；本实验未单独采样恢复阶段 CPU，恢复性能以主验收的配套报告为准。

普通种子仍有与本功能无关的 `binfmt_misc` 模块/挂载警告以及关闭模块目录挂载的提示；未用修改种子或服务 override 掩盖这些记录。没有验收 VM 图形桌面或实机 USB 链路。实机普通数据盘独立接入未在本任务中执行；VM 结果不能替代真实 USB 桥、Hub/供电、文件系统和应用组合的验收。

### 保留的中间记录与失败对照

此前 v8（`sd-1004-232607-652502/report.json`，SHA256 `8dcae3246d896a61003769afd561549ca83f1d34d2da1ae0ea3de58a80b4647d`）已通过 42 项，但尚未包含最终诊断收据/候选修正。其包 SHA 为 `4b68d52a30979a66260b54244ecfb71caca78c216628e3abfee2ae0dcdd0cbb4`；v9 的原生清单、RAM 工具归档和 base SHA 与 v8 完全相同，变更位于冷态诊断和文档。**本轮最终管理包采用上述 v9 的 42 项验收**，此前结果保持各自对应的产物范围。

此前 v6（`sd-1004-225008-497727/report.json`）通过 38 项且已验证实际 namespace，但尚未包含随后修复的管理部署/登记入口。v5、v6 均保留为中间证据；v7 只构建未运行 VM。

先前 `sd-1004-223037-365281/report.json` 的 v5 包通过 36 项功能检查，但没有检查实际 namespace。随后发现 systemd `ReadWritePaths` 需要 `+` 前缀才能相对 `RootDirectory` 解释路径；旧声明范围过宽。最终包已改为 `+/run +/dev`，以上 42 项验收重新完成全部功能并增加两次启动的实际挂载选项核验。v5 只保留为中间功能记录，不作为最终隔离配置的验收。


本轮中间失败保留在 `lab/work`，不计入通过结果：默认内核无路径超时为 0 时，登记被既有的有限超时前置检查拒绝（`sd-1004-215532-141431/report.json`）；改为显式 10 秒后，真实包安装和独立 RAM 准备成功，但数据控制器因 systemd 的 `226/NAMESPACE`、`Device or resource busy` 未能启动（`sd-1004-221254-244635/report.json`）。后者的 doctor 正确报告 `failed`，并完成 100 项 base ELF 的冻结/当前文件与包版本比较。测试没有放宽生产服务限制来绕过失败；修复后已重新构建包并完成上述冷启动验收。

进一步使用不读写 DM 的 `/bin/busybox true` 对照，保持所有生产限制相同：`RootDirectory` 指向 alias 时返回 226，指向同一环境的真实目录时返回 0（`sd-1004-221724-284667/report.json`）。systemd debug 日志把失败定位到 alias 路径下根和子挂载的重新挂载。该诊断没有修改生产 unit；它验证了目录路径差异，不能代替控制器恢复功能验收。

真实目录修复后的下一包已越过 namespace 阶段，原生维护入口随后拒绝读取 `/proc/1/root/etc/fstab`（`sd-1004-222058-304607/report.json`）。缩减权限后经 PID 1 的 proc 链接访问宿主配置需要额外权限；这一失败说明普通单测和根盘 owner 启动都不足以覆盖数据盘专有接入检查。最终修复由冷态管理过程只读绑定单个 fstab 文件，原生入口读取固定路径；没有增加 `CAP_SYS_PTRACE`，恢复和退出时不新增宿主根盘配置访问，随后完整数据服务验收通过。
