# 新版 multipath-tools 的隔离对照实验

核查日期：2026-09-25。这是对原报告“先验证上游新版，再决定自行扩展”的实际补充。**宿主没有安装新 multipath-tools、替换动态库、启动新守护进程或修改块设备。** 所有写入对象为 `lab/work/` 下普通文件创建的 QEMU 私有虚拟磁盘。

## 已证实的结论与限制

1. 未修改上游代码的 **multipath-tools 0.15.0 可以在 Ubuntu 24.04 库环境构建运行**，不需要混装另一发行版的用户空间。
2. Linux 6.8 下，新版在普通调度下完成三次同盘移除／接回，原进程持续追加并 `fsync`，在线逐块前缀回读通过。原来的 0.9.4 也通过了对应基本测试，所以这证明兼容可行，不能据此声称恢复能力获得普遍提升。
3. **升级至 0.15.0 没有消除重复 WWID 的错误接入风险。** 同容量、同 USB serial／SCSI WWID 的空白替代盘被接入，并收到 46 条完整的 4 KiB 业务记录；应用当时没有报错，原前缀回读失败。标准 multipath 的 WWID 信任边界仍然不同于项目要求的 PV／布局准入策略。该结论是这一输入条件下的复现，不是对正常唯一 WWID 设备的通用漏洞指控。
4. 新版可直接依据 `RLIMIT_RTPRIO=0` 保持普通调度。实际额外跑了一组**保留 `CAP_SYS_NICE`** 的试验：guest 继承的限制为 0，daemon 为 `SCHED_OTHER`、priority 0，三次重接通过。这比旧版必须拒绝实时调度系统调用的对照更明确，也适合由 systemd `LimitRTPRIO=0` 管理。实际部署的 unit 限制仍须验证，不能把本试验 PID1 的默认限制当作发行版 service 默认值。

| 实际 guest 内核 | multipath 版本／调度设置 | 注入 | 结果 | 最大单次追加＋fsync |
|---|---|---|---|---|
| 6.8.0-139 | 0.15.0，限制 RT＋去掉 SYS_NICE | 同盘 3 次 | 238 条前缀逐块通过、0 应用错误 | 0.432 秒 |
| 6.8.0-139 | 0.15.0，保留 SYS_NICE，继承 RTPRIO=0 | 同盘 3 次 | 236 条前缀逐块通过、0 应用错误 | 0.429 秒 |
| 7.0.0-34 | 0.15.0，限制 RT＋去掉 SYS_NICE | 同盘 3 次 | 239 条前缀逐块通过、0 应用错误 | 0.590 秒 |
| 6.8.0-139 | 0.15.0，限制 RT＋去掉 SYS_NICE | 同 WWID 空白盘 1 次 | 替代盘收到 46 条业务记录，前缀校验失败 | 0.381 秒，不代表安全恢复 |

7.0 行使用单独下载、验证的 Ubuntu 内核与配套模块，基础用户空间不变。以上所有 daemon 的实际调度均为 `SCHED_OTHER`。该表用于确认版本组合是否运行以及准入边界，不用于版本性能排名。

这些实验的根文件系统在 RAM；USB 盘承载 `整盘 multipath → kpartx 分区 → LVM → ext4 数据卷`。**它们不构成完整 Ubuntu 根盘迁移、冷启动持久性、真实 USB 供电中断或生产整套 multipath 软件包的验证。** 工作负载仍采用原研究的 4 KiB 追加、逐次 `fsync` 和在线前缀回读；没有把拔插改成整机断电。

## 固定版本与构建来源

源码采用 [0.15.0 标签](https://github.com/opensvc/multipath-tools/tree/0.15.0)，GitHub tag API 指向提交 `5a60a67d9f48ddff0d63b6e5d04c3a22764a0670`。分别下载标签归档和该提交归档，对其中 **350 个普通文件逐一计算 SHA256，内容集合完全一致**。这记录了 HTTPS 来源和固定提交，不冒充已经验证上游 GPG 发布签名。

私有开发依赖通过 `apt download` 下载，随后 `dpkg-deb -x` 解包到 `lab/work/new-multipath/sysroot/`。使用 Ubuntu 的现有运行库，不写入 `/usr` 或 `/lib`。具体包版本、归档 SHA256、编译参数、宿主运行库版本以及 stage 文件哈希保存在 [new-version-build.json](new-version-build.json)。

实际 make 选项为：`-j4 ENABLE_LIBDMMP=0 LIB=lib bindir=/usr/sbin EXTRAVERSION= WARN_ONLY=1`。私有 `PKG_CONFIG_PATH`、`PKG_CONFIG_SYSROOT_DIR`、`CPPFLAGS`、`LDFLAGS` 的完整值已记录。`WARN_ONLY=1` 是上游支持的构建选项；它用于处理 Noble 的 liburcu/libaio 头文件与上游 `-Werror` 组合产生的警告，没有修改源代码。没有启用的 libdmmp 管理库不参与本次 daemon 数据路径；因此本构建不是生产软件包配方。

构建输出被安装到私有 `lab/work/new-multipath/stage/`。guest 内部记录的 daemon SHA256 必须和 stage 对应，`multipath -h` 明确返回 `multipath-tools v0.15.0 (07/13, 2026)`。

## 配置与实验控制

使用同一旧版探针的工作负载与校验函数；新 runner 为 [version_probe.py](version_probe.py)，导入 [multipathd_probe.py](multipathd_probe.py) 的 `INIT`、`GUEST`、原始磁盘记录扫描器。它支持分别选择内核、基础 initramfs 和 multipath stage：

```bash
# Ubuntu 6.8 + 上游 0.15.0 + 普通调度
python3 research/2026-09-25/version_probe.py \
  --stage-root lab/work/new-multipath/stage --deny-rt

# 同 WWID 的独立空白替代盘（用于复现准入缺口，预期 continuity oracle 不通过）
python3 research/2026-09-25/version_probe.py \
  --stage-root lab/work/new-multipath/stage --deny-rt --wrong

# 不指定 stage-root 即使用宿主发行版的旧 multipath-tools
# 更换内核时必须同时提供对应模块所在的独立 initramfs
python3 research/2026-09-25/version_probe.py \
  --kernel lab/work/version-study/kernel7/guest/vmlinuz \
  --initramfs lab/work/version-study/kernel7/guest/initramfs.cpio.gz \
  --stage-root lab/work/new-multipath/stage --deny-rt
```

新版使用 `flush_on_last_del never`，对应“最后路径删除时不要立即关闭排队”的实验策略；没有机械沿用旧版 `no` 字面值。其余主要意图保持：允许 USB、严格登记 WWID、TUR checker、1/4 秒检查间隔、`no_path_retry 8`、`recheck_wwid yes`。有效配置从运行中的 daemon 查询并保存。

`--deny-rt` 同时将 `RLIMIT_RTPRIO` 硬／软限制置 0，并去掉 `CAP_SYS_NICE`，保持旧版普通调度对照可运行。不带该选项的新版额外对照保留 capability，验证仅 `RLIMIT_RTPRIO=0` 的行为。

QEMU 使用 KVM、双 vCPU、1 GiB RAM、UAS 设备、1 GiB fresh raw 镜像，无网络、无宿主共享文件系统、没有传入宿主块设备。每次启动创建独立 overlay initramfs，基础 initramfs 不改写。实际 guest kernel release、QEMU 参数、runner／guest／kernel／initramfs 哈希都保存在原始报告。

为隔离 daemon／内核变量，试验保留同一显式 `scsi_id` udev 规则和 Ubuntu DM 规则，没有引入新版完整 udev／systemd 安装策略。这意味着结果不能直接替代 initramfs 早期自动发现、服务打包以及 LVM 自动激活竞争的后续验收。

## 资源解释

本次 0.15.0 在 6.8 中的示例快照约 RSS 19.7 MiB、8 个线程；`VmLck` 约 403 MiB 表示被锁定的虚拟地址范围，不能视为相同大小的额外常驻物理内存。健康期 CPU 只有 10 秒、`/proc/PID/stat` 的 tick 粒度，不能推出峰值或尾延迟改善。部分样本计数差为 0，只表示这次窗口没有积累到可见 tick，不表示 daemon 零 CPU 开销。

本轮存在并行 VM，旧／新探针的时刻、状态查询和调试构建也有差异。表中的最大写入时间是各自样本，**不应做成“新版快 X%”的性能结论**。要评估日常开销，应另做固定负载、固定 CPU 资源、同一服务配置、多轮采样的 PSS/CPU/唤醒次数对照。

运行摘要、每份原始报告的 SHA256 和 oracle 含义见 [new-version-results.json](new-version-results.json)。本轮结果支持继续评估新版，不能作为跳过接回前准入、映射事务恢复、超时后策略或完整根盘验收的理由。
