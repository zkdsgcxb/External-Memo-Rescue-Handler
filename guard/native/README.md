# C++ Guard 运行时

`runtime/` 是唯一的自动恢复运行时，使用 C++17：启动阶段的根卷保护、常驻健康监视、原盘重新准入、换表与内核路径探测、超时后的接管，以及普通数据盘的登记记录接入。根盘与数据盘共用同一份控制器；它仍是用户空间服务，不是内核模块。

原有只读原型见 [OBSERVER.md](OBSERVER.md)。其资源数据不能代替完整恢复运行时的比较。

## 职责

| 文件 | 职责 |
| --- | --- |
| `runtime/core.*` | FD 所有权、单写入者锁、异步工作线程、事件合并、libdevmapper 状态查询、内核 probe ioctl、有界日志和命令 |
| `runtime/admission.*` | 登记策略、USB/分区/LVM 或文件系统身份、活 FD 与 diskseq 核验、布局核验、数据盘隔离检查 |
| `runtime/controller.*` | 唯一恢复状态机、截止时间、事务记录、原子换表、systemd READY、登记设备接入和死亡接管 |
| `runtime/boot.cpp` | 保护启动中先建立稳定 DM，再激活已有 LVM；禁止接管已提前激活的根卷 |
| `runtime/main.cpp` | 严格命令行入口和结构化错误输出 |

块 I/O 排队仍由内核 DM multipath 承担；USB/SCSI、LVM、文件系统、systemd/udev 分别承担已有职责。C++ 不实现替代驱动、文件系统或自动 fsck。Python 仅用于登记/安装工具、人工救援以及实验控制和报告工具。旧 Python 控制器不再保留在当前源码或生产构建选项中；历史对照由 [固定版本实验入口](../../lab/README.md#历史实验复现) 从 Git 提取。

## 构建

```bash
python3 guard/native/build_runtime.py --output lab/work/cpp-runtime
lab/work/cpp-runtime/guard-runtime --version
```

需要 GCC 的 C++17 支持、OpenSSL 开发头文件、binutils、pthread、dl。JSON 解析器固定为仓库内附带 MIT 许可的 nlohmann/json 3.12.0；SHA-256 见 [vendor/README.md](vendor/README.md)。libdevmapper 使用发行版的公共 ABI。只支持经过 ABI 审查的 Linux 64 位小端平台；完整 C++ 内核故障测试为 x86_64；ARM64 和 RISC-V64 的交叉编译与实际用户态执行见 [架构报告](../../research/2026-10-02/CPP-ARCHITECTURES.md)。

构建生成精简 ELF、独立调试符号及源码/二进制散列清单，并执行不接触真实块设备的单元测试。构建动作不会安装或替换本机 Guard。

运行入口：

```text
guard-runtime activate --config ENROLLMENT.json
guard-runtime run --config CONFIG.json
guard-runtime takeover --config CONFIG.json
guard-runtime maintain --record RECORD.json [--takeover]
```

这些入口由保护启动或 systemd 调用，不应对正在由另一个运行时维护的映射手动启动。`maintain` 校验 systemd invocation 收据；任何退出后的接管都要等旧 helper 的 flock 引用释放。截止时间终止新准入，不声称能取消内核里已发出的 I/O。

## 验证与比较

新测试工具 [lab/cpp_guard_probe.py](../../lab/cpp_guard_probe.py) 在同一套完整 Ubuntu、同一 Linux 内核、相同磁盘和配额下，分别运行固定提交 `e745e5e` 的 Python 运行时及新 C++ ELF。核验实际 `/proc/PID/exe` 散列，防止误测仍由 Python 恢复的观察器。

```bash
python3 lab/cpp_guard_probe.py --help
python3 lab/cpp_transaction_probe.py --help
```

比较覆盖正常监视、无关/相关事件风暴、三块磁盘同时断联恢复、十轮连续恢复、原挂载与子挂载/已打开 FD、错误身份和超时，以及控制器在各事务阶段死亡后的接管。CPU 百分比以单个逻辑核为 100%；采样窗口峰值不是任意瞬间的硬上限。PSS、RSS 和 cgroup 内存分开报告，共享库/RAM 包体积也单列。

结论与资源口径见 [迁移报告](../../research/2026-10-02/CPP-MIGRATION.md)，方法和逐项验收见 [CPP-VM-VALIDATION.md](../../research/2026-10-02/CPP-VM-VALIDATION.md)。本轮只在 QEMU 运行新代码，实机继续使用已安装的旧运行包。
