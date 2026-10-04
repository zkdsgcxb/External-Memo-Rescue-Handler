# 当前服务限制下的事务故障矩阵

[cpp_transaction_probe.py](../../lab/cpp_transaction_probe.py) 默认使用完整 Ubuntu，由当前根盘 unit 启动实际 C++ owner。仅将启动门控换为 lab 标记、RAM RootDirectory 换为 `/run/rescue`、配置路径换为实验路径，unit 以 `lab-guard.service` 命名；`Type=notify`、ExecStopPost 接管、超时、CPU/内存限制、OnFailure 和全部权限限制保留。极简 guest 仍可单独选择，但不计入 systemd 权限验收。

每个 Ubuntu 故障案例开始前，观察器检查实际 unit 散列与无 drop-in、原生进程 ELF 散列、NoNewPrivs/seccomp、精确 capability 上界、ProtectSystem、可写路径和 cgroup `4000 20000` 配额，并保存进程 mountinfo。原有事务阶段死亡、截止时间、无人接管、候选设备替换与 NBD 保持请求的故障注入和终态判据保持原样。这个矩阵有意测试事务失败边界，不代替完整生产 initramfs 启动和失败后用户数据持久性审计。

准备只编译、打包，不启动 VM：

```bash
python3 lab/cpp_transaction_probe.py matrix --guest ubuntu \
  --binary lab/work/roadmap-native-final/guard-runtime \
  --base-build lab/work/route-refactor-v4 --prepare-only
```

2026-10-04 本轮最终源码已成功准备 `lab/work/cpp-txb-1004-233109-673904/`，原生 ELF SHA-256 为 `479c8ba7e2445ae3fbb99d58e1b79d836170c6fe463953ca636cde9857ee8718`，内核为 `7.0.0-34-generic`。生成 runner 和 Ubuntu 观察器已通过语法检查；3 项服务适配测试与原有 5 项原生 fixture 测试通过。定向 `after_load` 死亡测试已通过 12/12（`lab/work/20261004-231201-tx-kill-1-569494/report.json`）；逐案例最终结果见下表；初次整轮并非全绿。

串行 VM 队列轮到此实验后，可复用已准备材料：

```bash
python3 lab/work/cpp-txb-1004-233109-673904/transaction-native.py \
  matrix --build-dir lab/work/cpp-txb-1004-233109-673904 \
  --guest ubuntu --queue-seconds 12
```

该事务 fixture 仍使用已登记的旧实验内核/initramfs 基座和固定历史 Python 观察/设置工具，完整散列写入 `build.json`；Python 不作为恢复 owner。当前干净生产镜像由独立 integration/reproduce 入口验收，不能把两者混称为同一个镜像。生产 unit 或 ELF 变化后必须重新准备材料。

## 实验观察器修正与保留的失败证据

旧基座 `/etc`、`/etc/rescue` 携带组可写权限，新的可信父路径检查正确拒绝了入口。生成实验 init 只在复制后的可丢弃 RAM 工具树中将这些控制父目录和 JSON 设为 root 私有；没有放宽 C++ 信任规则。原拒绝日志保留在 `lab/work/20261004-225726-tx-kill-1-562634/console.log`。

生产 `OnFailure=emergency.target` 会在终态失败后停止常规 Ubuntu 服务。实验原来的 RAM agent/shell 使用默认 systemd 依赖，随 basic/sysinit 停止，导致已有成功接管结果无法继续采集；仅 `IgnoreOnIsolate=yes` 仍无法解除默认依赖。两份失败报告 `20261004-230035-tx-kill-1-564606`、`20261004-230748-tx-kill-1-567614` 保留在 `lab/work/`。最终生成器只给独立的 `lab-agent`、`lab-shell` 加 `DefaultDependencies=no` 与 `IgnoreOnIsolate=yes`；Guard 的 OnFailure、超时、接管和安全策略逐字保留。这个观察器存活不表示普通应用在主动 fail-closed 后还能继续正常工作。

## 本轮结果

结构化结果与每个原始报告、runner、initramfs、unit 和 ELF 散列见 [transaction-policy.json](../../lab/results/2026-10-04-transaction-policy.json)。同一原生 ELF 与生产 unit 策略下，11 个场景的 138 条判据逐案例通过；它们来自初轮 10 个成功案例加最终 deadline 单场复验，不能描述成初次整轮 11/11。

| 场景 | 通过判据 | 证据 |
| --- | ---: | --- |
| 7 个阶段杀死 owner | 各 12 | before_load、after_load、before_commit、after_commit、before_probe、probe_started、before_ready；初轮报告清单在 JSON |
| 健康期 owner 消失 | 12 | `lab/work/20261004-231923-tx-abs-0-570900/report.json` |
| 候选在提交前被另一内核实例替换 | 11 | `lab/work/20261004-232135-tx-inst-2-570900/report.json` |
| 下层请求保持不返回、超过用户态及无路径预算 | 20 | `lab/work/20261004-232230-tx-stall-4-570900/report.json` |
| 已核验候选超过提交截止时间 | 11 | `lab/work/20261004-233135-tx-time-2-674126/report.json` |

初轮 deadline 的 `03-release` 观察动作超时，原报告 `lab/work/20261004-232017-tx-time-2-570900/report.json` 保留且不计成功；RAM heartbeat 当时仍持续，串口上传已完成，日志没有 namespace 启动失败。仅增加串口/RAM 进度标记的复跑 `lab/work/20261004-232833-tx-time-2-672333/report.json` 即通过，五个 DM 查询各约 2–3 ms，因此没有证明首次究竟卡在哪一步，不能断言它是 Guard 缺陷或已被某个生产修复解决。

复核发现历史观察器会先读取所有 Ubuntu 进程的 `/proc/PID/cmdline` 再筛选；该读取可能访问故障进程的用户内存并等待 mm 锁。最终观察器先用内核 `comm` 元数据筛选，再读取相关 owner/native/dmsetup 的 cmdline，保留动作前后进度。已登记 owner PID 不受 comm 筛选影响，死亡 leader 的空 cmdline 与其 D 状态线程仍纳入；原有 argv 判据和 `/proc/locks` 核验保留，blkid/LVM 本来不属于这项进程 oracle。该版本 deadline 再次通过。缩小观测集合避免了不必要的故障进程读取，但不能反向证明首次超时的具体根因。

被保持的下层请求案例在明确释放 NBD gate 前没有 I/O 完成；终态后仍保留 owner lock 和同代 probe，没有重复 owner 或额外线程增长。释放后没有永久 suspend 或残余 inactive 表。这确认本轮受控实验中的有限终态与接管边界，不代表内核能取消任意已发出的 I/O，也不是对所有设备驱动挂死的证明。
