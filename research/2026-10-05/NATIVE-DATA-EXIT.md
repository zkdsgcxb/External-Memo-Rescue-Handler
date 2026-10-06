# A：既有数据 DM 的安全退出与同次启动重入

2026-10-05。本轮工作区实现与隔离 VM 工程验收已完成，等待协调者审阅。没有宿主部署、根保护修改、提交、推送或发布。首次启用 B、开机挂载 C、离线分发 D 均未实施。

## 范围与结果

支持本轮已经运行的 `host-data` owner：健康、未挂载、所选 DM 无可观察消费者，才允许退出。根 profile 和 lab profile 保留原入口分支。普通数据的旧 native `run/takeover --config` 转到已有 `maintain`，统一 systemd invocation、owner fence 和控制器；没有第二套 Python DM 状态机。

实际工程组合是普通 Ubuntu VM、`7.0.0-34-generic`、现有单路径 DM、ext4。该内核仍只是首个工程基线，未成为公开最低版本。实验启动沿用临时数据服务模板，VM observer 准备夹具和既有 owner；未验证尚未实现的普通用户首次启用 CLI。

- 243 项 native 检查通过：admission 37、controller 13、core 60、data lifecycle 100、storage/input security 33。
- 13 个 Python launcher 测试通过；native ELF 的 PIE、RELRO、NOW、不可执行栈、栈保护和 fortified calls 检查通过。
- 最终 VM 完成六项拒绝检查、两次停用/重入、幂等停用、锁占用与释放后重试、一次原进程断联恢复、SIGKILL 后接管与拒绝非安全终态重入。
- 最终 ELF 构建记录、封存包内管理源码、VM driver 源摘要均与当前工作区匹配。

机器可读汇总：[2026-10-05-native-data-exit.json](../../lab/results/2026-10-05-native-data-exit.json)。原始最终报告：[vm4-report.json](../../lab/results/2026-10-05-native-data-exit/vm4-report.json)。同目录保存四次运行的完整 actions/QMP/QEMU 日志、失败报告、构建/测试记录及包清单。

## 实现职责

| 文件 | 本轮职责 |
| --- | --- |
| `guard/native/runtime/data_lifecycle.{hpp,cpp}` | 冷态停用检查、显式收据、归档/重入写入顺序、root Unix socket、native 停用客户端 |
| `guard/native/runtime/controller.cpp` | 当前 owner 接受并裁决停用；绑定启动时身份；安全终态接管；已有 maintain 的受限 rearm；host-data 旧入口统一 |
| `guard/native/runtime/admission.{hpp,cpp}` | 复用完整准入；仅内部重入调用要求 queue 已关闭，普通启动仍要求 queue 已开启 |
| `guard/native/runtime/core.{hpp,cpp}` | 原事件 poll 增加可选控制 socket；根/lab 默认无该 descriptor |
| `guard/native/runtime/main.cpp` | native `safe-stop --record/--config` 与 `maintain --record --rearm` 参数接缝 |
| `guard/data.py` | 旧 `stop` 改为验证 RAM runtime 后调用 native；原样区分 stopped/blocked/incomplete，不再直接发送 systemctl stop |
| native / launcher 测试与两个 lab driver | 安全边界和真实隔离实验；不增加生产首次启用流程 |

稳态仍使用原事件等待/兜底机制，没有新增每秒命令或进程扫描。新增 cgroup 检查只在冷态操作发生：优先读取 cgroup 成员；现有 RAM 的 `/sys` 绑定未必包含 cgroup2 子挂载，因此有 `/proc/<pid>/cgroup` 后备，限制 8192 个 PID、逐项检查 2 秒预算。该预算不能中断已经卡住的内核读取，也不是原子进程快照。没有增加 ptrace 权限、服务挂载或常驻辅助进程。

本轮未重新测量常态 CPU/RSS 峰值。ELF 实际为 929128 字节；资源验收只涵盖下面的实验上限，不能当作新的常态性能基准。

## 停用与重入的具体边界

停用由正在运行的 owner 在原 fence 下执行：

1. 请求绑定 boot、owner epoch、登记与配置摘要；磁盘上的身份必须与本 owner 启动时的身份摘要一致。
2. 要求 ready、无 deadline/pending operation/candidate/helper；检查设备实例、单路径表、几何、queue 开启、DM open_count、挂载、上层 holder、原始分区 holder、在途请求和路径状态。
3. 记录 `safe_stop_intent` 后再次核对。queue 修改前发现 busy 或检查失败，恢复原 journal 并返回 blocked，保留原 owner。
4. 用原 fence 执行 `fail_if_no_path`，读回 queue 已关闭及同一表，写 `safe_stopped` journal，再发布 `safe-stop.json`。普通 interrupted/expired/failed 不产生这种授权。
5. owner 正常退出；既有 ExecStopPost 验证并保留显式安全终态。客户端只有在同一 fence 可获取、原 cgroup 清空、收据及 queue-off 再核对成功后才返回 stopped。否则报告 incomplete 并保留资源。

未强卸载、未由停用流程杀用户进程、未删除 DM/分区、未释放共享 RAM/排除规则、未删除或更换锁文件。实验中测试进程的结束和夹具 holder 的移除均由 observer 完成并记录。

重入只接受原 `safe-stop.json` 和匹配 journal：同 boot、登记身份、原配置、几何、当前节点/dev_t/sysfs/diskseq 必须一致，并重做完整准入。恢复曾换过底层节点时，采用 owner 安全停用时记录的当前实例；不会把最初节点误当成新身份，也不会自动重新登记。

顺序为：queue-off 检查 → 复制旧 journal/receipt/invocation 至 `safe-stop-archive.json` → 新 owner 的 `rearm_intent` journal → 新 invocation 收据 → 再检查 → 开 queue → 读回 → 原 Guard ready。新 journal/invocation 必须先于 queue-on 成为可供接管的配对证据；锁 inode 保持不变。归档保留最近一次安全停用证据，记录大小沿用已有原子 JSON 上限。

本轮停用只作用于当前启动的 owner，不取消永久登记，不提供持久停用或卸载语义。后续 B 还须协调登记的期望状态和 udev/systemd 自动启动。

失败不是一律自动重试：归档失败或获取 fence 失败且旧 journal 仍为安全终态时，可以排除原因后重试；新 `rearm_intent` 已替换主 journal 后的失败应诊断/接管，不拿旧归档或普通 interrupted 重新授权。停用后设备再次拔插而 diskseq 改变，也不会自动重入，应由后续 B 的新计划/确认准入处理。

## 真实 VM 证据

| 场景 | 实际结果 |
| --- | --- |
| 挂载、打开 DM FD、上层 linear holder | 均 blocked，原 PID、journal、表保持 |
| 未登记的几何变化 | blocked：table or geometry changed；由 observer 恢复原夹具表 |
| 额外测试 helper 留在 owner cgroup | blocked，原 owner 保持 |
| 运行期间 identity 文件改变 | 客户端发现当前 runtime 与选中登记不一致并拒绝；恢复原文件后继续。owner 内存身份钉住另经代码审阅，不能把此案例说成同时改登记与身份后的 server 专项实验 |
| 第一次停用及重复调用 | PID 1053 → 0、inactive、cgroup 空、queue-off；重复调用收据摘要一致 |
| 另一个进程持同一 fence | 停用 incomplete、rearm 拒绝且原 journal/表/RAM 保持；释放 fence 后重复停用成功 |
| 第一次 rearm | 新 PID 1291、新 epoch、queue-on；归档 journal 等于停用 journal |
| 断联期间请求停用 | 实际路径 `8:17 F 1`，明确拒绝 recovering/unfinished helper，PID 1291 保持 |
| 原应用恢复 | QMP 移除/接入命令区间 0.944220181 秒，包含停用观察；worker PID 1311 不变，ACK 4→7；最终 9 条连续 ACK、fsync/O_DIRECT 核对和文件逐块核对通过，正常退出 0；最长该次 I/O 为 3.392206347 秒 |
| 恢复后再次停用/rearm | 停用绑定 `/dev/sdd1`、`8:49`、diskseq 18；第一次停用为 `/dev/sdb1`、`8:17`、diskseq 12；再次重入成功，PID 1488 |
| SIGKILL owner | 既有接管将 queue 关闭并写 interrupted；rearm 返回 1，终态 journal 未变 |

全程同一个锁 inode **1607**。实验 RAM 挂载、fstab 摘要和所选 DM 保留。native 代码只向选中 DM 发送排队策略消息，未操作外部 DM；本次没有逐一对比其他 DM 的完整快照。这是在普通根、单个数据 owner 环境中的保留观察；没有实测另一个活动 owner 共享 RAM，也没有实测根保护共存。

实验使用 2 vCPU / 3 GiB RAM、无网络、项目 regular-file qcow2/seed。一次只运行一台；未挂入宿主块设备。最终实验目录新增 **3140214784 字节（2.925 GiB）**，另有约 1.58 MiB 的原始证据副本，低于 8 GiB 上限。资源监控没有报错；只读 seed 的 inode/大小/mtime/ctime 保持，启动前完整 SHA 核对通过；最终 VM 正常关机，已无实验 QEMU 运行。

## 封存与复核

- 最终 ELF：`lab/work/a-exit-1005/native-r4/guard-runtime`
- ELF SHA256：`53893fefeb21b31a896c03e1ae20dc222d3691164ad431ff618715226df50324`
- 实验包：`lab/work/a-exit-1005/package-r4/ram-rescue-handler_bbaf25c0389f37b23f6971b0_amd64.deb`
- deb SHA256：`17c026bd7c5b3af6b746e00adb0504fe2ec29963298c3df3012fe310876b03a5`

VM 从实际 `/proc/PID/exe` 复核上述新 ELF。未继承旧 ELF 的恢复通过结论。VM1 因 RAM 中缺失 cgroup2 子挂载保守拒绝停用，修复后重建；VM2 正常/幂等停用通过，后续因 observer 对 inactive unit 的 reset-failed 处理失败；修正 observer 后 VM3 全流程通过。最终自审收紧 fixture 异常捕获，防止断言失败被当作注入故障；r4 重建的 ELF 字节与 r3 相同，测试源码更新后重新封包，VM4 用最终包再次全流程通过。旧失败和中间通过记录均保留。

```bash
python3 -B guard/native/build_runtime.py --output lab/work/a-exit-1005/native-r4
python3 -B -m unittest lab.tests.test_data_launcher -v
python3 -B guard/package.py --output lab/work/a-exit-1005/package-r4 --base-rescue-dir lab/work/cq-1005/base-from-v9 --native-binary lab/work/a-exit-1005/native-r4/guard-runtime
python3 -B lab/data_lifecycle_probe.py --reproduction-dir lab/work/rp-1004c --package-dir lab/work/a-exit-1005/package-r4 --output lab/work/a-exit-1005/vm4
```

以上为本轮实际路径；封包和 VM 输出目录拒绝覆盖，复跑应使用新输出目录并继续计入资源上限。

## 未证明的事与 B 接缝

1. raw backing 普通 FD 没有被完整枚举；收据/结果明确标记 `raw_open_descriptors: not_observable`。DM 消费者检查不等于整块物理盘没有任何旁路读者/写者。
2. 没有注入真正永久 D 状态。实际验证的是 held-fence 的未完成/拒绝/保留资源；内核卡住时不宣称用户空间可强制安全退出。
3. fixture 覆盖持久化/queue 回调崩溃前缀；真实 SIGKILL 命中 READY owner，没有逐指令命中 rearm 内部每个时点。SIGKILL 到 systemd 接管之间不承诺零时间窗口；实现保证 queue-on 前具有匹配的接管日志，并保留既有内核排队时限兜底。
4. 未扩展根启动、根恢复、其他文件系统/拓扑、其他内核、ARM 或公开发行验收；没有新的完整性能结论。
5. B 可以复用当前 native `safe-stop`、显式 `--rearm`、blocked/incomplete 结果及原子收据；仍需完成用户确认/激活资格、首次启用文件与服务所有权事务、撤销/停用后的登记状态、包移除规则。`save_candidate_only` 不得被激活消费。本轮未创建或消费激活资格，实验包不是可公开发布的产品验收包。

工作区原有改动全部保留。本轮停在代码与工程证据审阅边界。
