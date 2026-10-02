# 可选 C++ 健康观察器实验

本页描述早期只读观察器，不是当前自动恢复入口。完整控制器见 [C++ 运行时](README.md)；若实验依赖旧 Python Guard，使用 [固定版本实验入口](../../lab/README.md#历史实验复现)。

这是 **只读观察器原型**，当时未替代 Python Guard，也不随生产服务安装；完整恢复迁移现已由独立 C++ 运行时完成。
它用于回答：同一张实际 DM multipath 映射、相同健康检查和检查频率下，
移除 Python 解释器能减少多少 CPU 与常驻内存。

## 职责与限制

- 持久加载 `libdevmapper.so.1.02.1`，每秒查询一次状态；内核 block uevent
  可以提前唤醒，100 ms 内合并重复通知，每次最多读取 64 个事件。
- 检查精确 map UUID、单一 multipath target、原节点 `dev_t`、sysfs 真实路径、
  `diskseq`、当前路径 `A` 状态及任意 `F` 路径。
- 只接受来自内核的 netlink 通知；通知只是提前检查的提示。
- 启动要求 multipath target 至少 1.15.0，以保持项目的环境前提。
  **观察器没有执行 `DM_MPATH_PROBE_PATHS` ioctl。**
- 不获取 Guard owner，不登记新设备，不读身份扇区，不加载/提交 DM 表，
  不挂载，不执行恢复，不修改 journal，不重设故障截止时间。
- 固定观察启动时的已准入设备实例。真正的 Guard 准入重连新实例后，
  观察器仍会报告旧实例失效；它不能自行批准新 `diskseq`。

输出 JSONL：`ready` 或 `path-unavailable`；控制查询失败输出
`control-uncertain`。退出码分别为：全程健康 `0`、发现路径失效 `2`、
参数或控制异常 `1`。它没有 systemd READY 通知协议，不能直接放进现有 Guard 单元。
这里的 `ready` 仅代表上述观测条件通过，不证明请求队列已清空、下层没有阻塞，
也不证明文件系统或应用未受到先前错误影响。

Python 对照 `reference.py` 检查相同条件，包括当前路径必须为 `A`。
这比生产 Guard 健康循环的“当前实例存在且未出现 F”条件更严格。
因此比较的是 **观察器对观察器**，不代表两个完整恢复实现等价。

## 构建和可重复实验

```bash
python3 guard/native/build.py --output lab/work/native-build
python3 guard/native/vm_probe.py
python3 guard/native/vm_probe.py --fault-only
```

构建需要 C++17 编译器，默认 `g++`，没有额外框架或第三方 JSON 库。
运行依赖标准 C++ 动态库、libgcc、libc 和系统 libdevmapper。通过 `dlsym`
使用 libdevmapper 公共 ABI；声明有结构大小断言，不依赖私有符号或内核结构布局。
当前完整存储实验为 x86-64 Ubuntu VM；其他指令集构建/解析器运行证据见架构报告，
不能把用户态解析器通过当作完整热插拔恢复验证。

`vm_probe.py` 复用完整 Ubuntu 的只读基础镜像，创建新的 qcow2 overlay 和
两块一次性数据盘，既不向 QEMU 传入宿主块设备，也不安装到宿主。
VM 的 `/run` 为 noexec，因此实验 ELF 放在 **虚拟机 overlay** 内
`/usr/local/lib/ram-rescue-native-benchmark`。生产 RAM Guard 的部署未变化。

## 计量口径

同一个 ext4 数据映射、同样 1 秒兜底和 100 ms 事件合并，两个观察器顺序运行，
交替次序，各重复 3 次。每次预热 5 秒、健康采样约 20 秒，名义采样窗口 100 ms。
主 Guard 继续承担真实恢复；其资源以及采样进程均在观察器 cgroup 之外。

- CPU 从 cgroup `usage_usec` 差值计算，一个逻辑核占满为 100%。
- 窗口峰值使用实际采样时间差，**不是硬件瞬时峰值**。
- 启动到首样本的 CPU 用量单列，预热和稳态不混为一个平均值。
- 同时保存 `memory.current`、systemd `MemoryPeak`、进程 RSS/PSS、私有页。
  cgroup 内存包含被记账的页缓存，与 PSS 不可相加，也受页缓存归属影响。
- 每次完整原始样本、健康输出、所用二进制及 Python 模块 SHA256 写入报告。
  不以宿主 CPU 百分比或 QEMU 整进程资源作为 Guard 自身占用。

本原型未测恢复执行、超时接管和身份验证成本。因此观察器的节省不能直接标为
整个 Guard 的节省。完整迁移另外保留唯一恢复 owner、事务 fence 和 terminal
状态约束，并单独完成了故障矩阵与资源对照，见 [C++ 迁移报告](../../research/2026-10-02/CPP-MIGRATION.md)。
