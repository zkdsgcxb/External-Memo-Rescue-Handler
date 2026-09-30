# 阶段 D：启动资格与同盘回滚原型

> 当前状态：2026-09-30 已通过「首次安装、正常关机、再次启动已有盘、再次正常关机」的正常路径验收，证据见下文。完整故障与回滚矩阵仍未通过验收，已暂停扩展；不能将下文合同与原型设计解读为全矩阵通过。首次失败报告保留于 `lab/work/20260925-161459-boot-evolution-50275/report.json`。

初稿日期：2026-09-25；正常启动路径收敛：2026-09-30。实验只使用当前 Linux `7.0.0-34-generic`，不增加旧内核兼容分支，不修改宿主引导、真实 USB 根盘或宿主服务。配套验证器为 [lab/boot_recovery_probe.py](../../lab/boot_recovery_probe.py)。

当前优先目标已收敛为真实 USB 根盘的短断重接。验证器默认 `--normal-only`，只初始化一次新盘，再从同一已有 Ubuntu PV 启动；不再默认运行下文七项故障矩阵。矩阵代码与先前记录保留，必须显式指定 `--fault-matrix` 才会运行，本次没有重跑。

## 本次修正与正常路径

最初 `20260925-161459-boot-evolution-50275/seed` 的 `console.log` 为空；`qemu.log` 明确报 AF_UNIX 串口 socket 路径超过 108 字节。QEMU 尚未启动内核，不能归因为 Ubuntu/Guard 启动失败。验证器改用短的实验/场景目录，并在启动前校验最长 socket 路径。之后发现临时交互 shell 忽略 SIGTERM，使 PID 1 等待它退出；改为仅对这个实验 shell 发送 SIGKILL 后完成串口交接。

正常模式的流程是：

```text
新建 raw → 安装一次 Ubuntu / 写入并 fsync sentinel → 正常关机
    └─ 新 qcow2 overlay / 新 boot → 核验已有 PV → 稳定 DM → 激活原 LV → 原 Ubuntu
```

可复用逻辑位于验证器嵌入 guest 的 `activate_existing_root()`。它不含安装或格式化操作。`existing-boot` 还跳过 `ubuntu.prepare()`，因此不会在正常重启时重写既有 service、fstab 和 machine-id。新的 boot ID、原 sentinel、PV/ext4 UUID、machine-id、fstab 哈希共同区分“启动原安装”与“重装一个能启动的替代品”；随后做文件写入、文件/目录 fsync 和回读，要求根挂载为可写且 Guard 为当前活 owner。

正常模式要求两次 systemd 正常关机完成。旧原型等待 30 秒便终止 QEMU，可能碰到 Ubuntu snapd 与交互 shell 的正常停止超时，记录为 `graceful_shutdown=false`；这不能冒充正常关机。新正常模式给予 150 秒收尾，并把两次关机完成加入汇总验收。

本次新增三个独立单测：默认/显式正常模式不选择故障矩阵、登记缺失时在导入控制代码之前拒绝、嵌入 guest 语法检查。三个通过；已有盘实际启动以 QEMU 实测为准。

2026-09-30 的正常路径实际使用 `lab/work/async-guard-v2`，两台顺序 VM，结果 **passed=true**：

| 核验 | 实际结果 |
|---|---|
| 首次安装与已有盘再次启动 | 均成功；第二次没有调用安装器 |
| 当前 Guard | 两次均同 boot/epoch、systemd MainPID 与活进程匹配；ready |
| 原持久 sentinel | token `f233cd7bc44340cba8995423c6b96cf3` 保留 |
| PV UUID | `dE4Dc2-FYER-usxT-NVTk-z7Cp-CSbm-Tqh7g8` 不变 |
| 根 ext4 UUID | `709688b5-38f4-4589-b67b-1544d0084d2f` 不变 |
| 原安装配置 | machine-id 与 fstab SHA256 不变 |
| 写入能力 | 根挂载 rw；文件写入、文件/目录 fsync、回读均通过 |
| 启动与关机 | 两个不同 boot ID；两次均到达 `reboot: Power down`，QEMU 正常退出 |
| 原 raw | 再次启动前后完整 SHA256 不变；后续写入只在新 overlay |

原始报告：`lab/work/bootevo-0930-173036-39819/report.json`，SHA256：`8af4fc7e34eae5fa26297d127b83a5a5e9167feee5d41c9e3798e0acb138a7ad`。内核 SHA256 为 `73d9c6e40b210deb638070d6af591a68bb6d7ff0280d0ec34c9ccf705a4f47ac`，原 initramfs 为 `b77bf6b035cbc9c5c6b14bea50007db24a577e5f3a080354c611f7dfaf148947`；overlay、runner 和命令哈希/内容也保存在报告中。

直接适用于后续最小部署的结论是：**现有 PV 可以先经稳定 DM map 激活，再启动原 Ubuntu；不需要重新分区或格式化。** 真正移入实机 initramfs 时还需要参数化登记的 VG/LV、排除裸路径抢先激活，并接入已有内核/引导构建流程。当前函数仍有 QEMU/DMI 与 `labrescue` 限定，不能作为实机安装命令直接执行。这里尚未测试这一正常启动路径与本次真实 USB 热拔插的组合，不把正常开机通过当作掉盘恢复验收。

## 需要解决的启动合同

现有正常启动通过，不能证明升级失败后仍有安全入口。源码审计确认了三个具体缺口：

1. `lab/guest/init.sh` 的 RAM 诊断副本和串口 shell 在 `agent.py --setup` 成功后才建立；setup 异常会退出 PID 1。准备失败时不能依赖尚未建立的救援入口。
2. `ubuntu.py` 的 Guard 单元采用 `Type=simple`，`lab-ready.service` 只有 `After=` 排序。进程刚创建、Guard 已报 ready、根系统已启动，是三个不同事实。`LAB_ROOT_READY` 不能作为受保护启动证明。
3. `agent.setup()` 是全新实验盘安装器，每次执行都会分区、创建 PV/VG/LV 并格式化。拿旧 initramfs 在同一 raw 上重跑 setup，可能得到一个能启动的全新文件系统，不能据此宣称回滚保持数据。

本原型把启动资格划为两层：

| 层次 | 必须满足的事实 | 失败行为 |
|---|---|---|
| 切换 root 之前 | 清单内核版本匹配、模块已加载、multipath target ≥ 1.15.0、RAM 依赖及登记配置存在、已登记 PV/LV 身份和布局通过核验、LV 只经过稳定 DM map、无 inactive/suspended 遗留 | 留在 initramfs RAM shell；不启动 Guard，不切 root，不称 protected |
| 切换 root 之后 | 本 boot 的 gate 成功；Guard state=ready；state/journal 为同 epoch；journal boot ID 为当前 boot；journal owner PID 是当前 systemd MainPID；服务 active/running 且该 PID 仍为 Guard 活进程 | 根系统可能已起来，但 protected=false；保留 RAM 救援入口 |

这个 protected 是**当次采样的控制就绪谓词**，会随 owner 退出、epoch 变化失效。它不是持久授权令牌，不证明任意上下游请求的正确性，也不把应用或文件系统错误历史的 `incomplete` 改成健康。

## 本轮原型实现

验证器校验原构建的 kernel/initramfs SHA256 及展开 `/init` 的源码哈希，在镜像末尾追加实验 cpio overlay。主 guest 源码、主 build 脚本和原镜像均不修改。

overlay 建立准备之前即可访问的 ttyS2 RAM shell，把准备异常转换为 RAM 停留。成功切 root 前结束该临时 shell，之后由已有 systemd RAM shell 接续串口，避免两个 shell 争用。同一份辅助代码输出结构化 gate 事实与独立 readiness 观察，实验记录不混用 `preflight_passed`、`root_switched`、`guard_ready` 和 `protected`。

只有 seed 启动调用一次既有 setup，且只能格式化验证器新建的 8 GiB 普通 raw 文件。后续启动使用只读登记资料，以现有 Admission 先核验唯一 USB 实例、分区/PV/VG/LV 布局，再创建稳定 map、通过 `--devices /dev/mapper/lab-path` 激活已有 LV。后续分支没有分区、pvcreate、vgcreate 或 mkfs。每个新 boot 更新实例专属的 diskseq/sysfs 路径，不沿用上一 boot 的易变身份。

回滚链为：

```text
新建 raw，安装一次 Ubuntu，写入并 fsync 随机 sentinel
  └─ 失败候选 qcow2：同一 PV/文件系统，写入升级标记，故意破坏 Guard ExecStart
       └─ 回滚 qcow2：选定已验证 v4 initramfs，恢复该构建的服务，读取两个持久标记
```

原 raw、失败候选 qcow2 都作为显式只读 backing。回滚前后核对两者完整 SHA256。回滚必须保留 seed sentinel、候选升级标记、原 PV UUID 和根 ext4 UUID，并由新的 boot 中真正存活的 Guard 取得 ready。若只从原 raw 新建旁支，丢弃失败升级的磁盘修改，本验证器不会把它称为这一项回滚。

## 早期故障矩阵（保留，默认不运行）

| 样本 | 注入位置 | 验收 |
|---|---|---|
| seed | 无故障，初始化新盘 | 根系统运行、同 boot/epoch 活 owner ready，持久标记存在 |
| preparation-failure | 准备入口，激活 root 前显式失败 | 保留 RAM shell、未切 root、无 Guard/journal、protected=false |
| wrong-version | 清单要求一个故意不匹配的 release，实际仍运行相同 7.0 内核 | 版本校验失败，未切 root，无旧内核适配 |
| missing-config | 不提供 `path-guard.json` | 配置校验失败，未切 root，不能把未保护启动当成功 |
| existing-transaction | 正常 ready 后由 systemd stop，再 start 同一服务 | 停止时接管；已有 journal 拒绝新 owner；没有额外 ready，服务 failed，RAM shell 存活 |
| guard-start-failure | gate 成功后，单元 ExecStart 改为 `/bin/false` | 根系统可能运行，但服务 failed、零 ready、protected=false；候选标记已 fsync |
| rollback | 使用原 v4 initramfs，disk 接在失败候选 overlay 之上 | 同 PV/ext4/sentinel、候选标记保留、重新就绪；backing 哈希不变 |

## 集成所需的最小 hook

主实现可按职责吸收经过验证的部分，而不维护第二套 Guard：

- **initramfs 准备器**：启动最早期 RAM 诊断入口；加载当前内核模块、检查能力与登记；从已有 PV 激活 root；任何关键准备步骤失败均留 RAM。新盘安装器和已有盘启动器必须分开，启动器不能含格式化回退。
- **构建/升级器**：记录内核、模块、RAM 依赖、配置 schema 和内容清单，先生成候选镜像并验 hash，再选择启动项。保留一份已验可启动镜像及独立可用的配置。rEFInd/ESP 更新属于之后的真实引导集成，不由本验证器修改。
- **Guard/systemd**：继续作为唯一运行控制者；停止后已有 journal 不能因 Restart 被清空。本轮不在 initramfs 启动另一个 Guard。ready 资格由当前 owner 状态和服务身份共同决定。
- **状态呈现**：root-ready 只表示 root/systemd 已启动。需要受保护启动的后续业务必须等待合格的 Guard ready，失败应进入可诊断状态；不能只依赖 `After=`。

若要求“Guard 已经保护就绪才允许 switch_root”，必须另行设计唯一 owner 从 initramfs 移交到 systemd 的协议。先启动 Guard、杀掉它再由 systemd 启动会留下 journal 并触发现有拒绝重启合同；并行两个 owner 则违反控制职责。本轮没有把这项移交伪装成一个启动排序参数。

## 适用边界

- 这是附加 overlay 的可运行原型，尚未安装为生产 initramfs 逻辑。准备失败样本覆盖显式准备错误、版本不符和缺配置；并未覆盖复制 RAM 工具时 ENOSPC、每一个 mount/move 错误、所有模块/动态库破损等点。
- 版本负例是错误清单与当前内核不匹配，不是测试另一 Linux 版本能否启动。回滚只回退本实验 userspace/initramfs，内核始终保持同一个已验 7.0 二进制。
- 初始 map create 与其间硬件变动/owner 死亡尚无完整事务化合同；Admission fd 凭证不等于 DM 最终取得内核对象的全绑定证明。
- 这里证明所选实验构建可恢复服务与保留指定文件/PV，不能证明任意发行版包升级、数据库格式迁移、LVM 元数据修改或根文件系统损坏可由旧 initramfs 撤销。
- 串口救援不依赖 root 盘普通程序；不意味着物理 USB 电气故障下所有总线、RAM 或桌面进程必然存活。全局 OOM、开机断盘、真实 UEFI/rEFInd 启动项切换需要后续专项实验。

当前正常路径复现需要已准备的 Ubuntu seed 与固定构建：

```bash
python3 lab/boot_recovery_probe.py --normal-only --baseline-build lab/work/async-guard-v2
# 新构建使用同一当前内核时，可以比较原安装的再次启动：
python3 lab/boot_recovery_probe.py --baseline-build lab/work/route-refactor-v4 \
  --candidate-build lab/work/<new-build> --normal-only
```

所有串口、命令、overlay、单例及汇总 JSON 保存在本次 `lab/work/bootevo-*` 目录。公开报告只记录实际通过的合同及原始报告哈希，不把原型通过等同于阶段 D 全部退出条件满足。
