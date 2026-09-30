# 多指令集验证记录

本轮将 Guard 的 ABI 假设集中到 `guard/runtime/linux_abi.py`，恢复事务仍共用既有控制器。
允许范围是 Linux LP64 小端 x86_64 / aarch64 / riscv64；未知架构和 32 位进程提前拒绝。

## 原生与 QEMU 用户态

最终证据目录：`lab/work/architecture-1001/abi-final87/`；早期 61 项子集报告保留在 `abi-results/`。

`report.json` 记录三个架构的真实 C 头文件编译结果与 Python ctypes 结果，全部一致：

| 接口 / 布局 | 三个架构实测结果 |
|---|---:|
| `BLKGETSIZE64` | `0x80081272` |
| `BLKSSZGET` | `0x1268` |
| `BLKGETDISKSEQ` | `0x80081280` |
| `DM_MPATH_PROBE_PATHS` | `0xfd12` |
| `sizeof(struct dm_info)` / 对齐 | 48 / 4 字节 |
| 12 个字段偏移 | 0、4、8、…、44 字节 |

每个架构实际执行 **87 项** Python 策略测试，全部通过，包含 Admission、普通文件系统身份、owner fence、事件解析、拒绝未验证 ABI，以及最终版本的异步操作、清理和有界异常位置格式化。
ARM64 和 RISC-V64 分别使用该架构的 Ubuntu Python 3.12.3 与动态库，不是在 x86 Python 内伪造架构名。
子进程测试通过实验目录内的 wrapper 显式再次调用 QEMU；没有修改宿主 `binfmt_misc`。
测试前后全部运行时代码和所选测试文件哈希一致，记录在最终报告的 `sources` 与 `sources_unchanged` 中。

另将可选 C++ 观察器的 `health_test.cpp` 分别交叉编译为 ARM64 / RISC-V64 静态可执行文件，在 QEMU 中执行，均返回 0。
这验证解析器编译与执行，不代表这两种架构的完整 C++ 观察器服务已部署。
其源码、二进制和工具包校验值见 `lab/work/architecture-1001/tooling-manifest.json`。

复跑已经准备好的工具：

```bash
python3 lab/architecture_probe.py \
  --tools-root /home/lx320/.local/share/ram-rescue-abi-tools \
  --work-dir lab/work/architecture-repeat
```

## ARM64 完整内核实验

`lab/arm64_probe.py` 使用 **Ubuntu 7.0.0-34.34~24.04.1 ARM64 内核**，与本机内核包版本一致。
工具和库来自 Ubuntu arm64 包，根文件系统驻留 RAM，额外 USB ext4 映像单独经稳定 DM 映射挂载。
QEMU 使用 TCG、2 个虚拟 CPU、1 GiB RAM，不连接宿主块设备、网络或共享目录。
Ubuntu EFI zboot 镜像中的原始 ARM64 `Image` 经解压供 QEMU 直接启动；不修改内核源码或配置。

最终报告：`lab/work/architecture-1001/arm-final/report.json`，**11 项检查全部通过**，包含最终的异常诊断内存优化。
早期同样通过的 `arm-vm6/report.json` 继续保留；两次报告的 SHA-256 见精简证据。
可随仓库保存的精简原始结果、ABI 数值、内核包与源码校验值见 [architecture-results.json](architecture-results.json)。

| 验收项 | 结果 |
|---|---|
| 原生 ARM64 `BLKGETSIZE64` / `BLKSSZGET` / `BLKGETDISKSEQ` | 实际设备登记、持有 fd 核验通过 |
| 原生 ARM64 libdevmapper 查询 / `DM_MPATH_PROBE_PATHS` | 通过，probe errno=0 |
| 底层设备重新枚举 | `/dev/sda1` → `/dev/sdb1`；diskseq 9 → 11 |
| QMP 拔插间隔 | 请求 0.2 秒；删除确认到重新接入返回约 0.252 秒 |
| 应用 I/O 最大等待 | 5.155 秒；TCG 实验值，不是实机性能承诺 |
| 原 Guard、工作负载进程与持续打开的文件 | 保留；25 次 4 KiB 持久写入与 O_DIRECT 回读，0 错误 |
| 挂载身份 | 拔插前后同一挂载 ID 和挂载记录 |
| 恢复后的卸载 / 离线只读 fsck | 正常卸载，`e2fsck -fn` 返回 0 |
| 构建时与验收时源码 | 全部哈希一致 |

采集的串口内核日志中，没有匹配到 `WARNING:`、`BUG:`、内核调用栈、EXT4 错误或块 I/O error。
这是一轮 ARM64 最小内核场景；不是所有故障组合的跨架构证明。

用已有工具与已解包内核，生成新目录后重新执行：

```bash
python3 lab/arm64_probe.py \
  --tools-root /home/lx320/.local/share/ram-rescue-abi-tools \
  --kernel-root lab/work/architecture-1001/arm-kernel/root \
  --work-dir lab/work/arm64-repeat
```

工具目录中 `root/` 存放宿主交叉编译器和 QEMU，`arm64/` / `riscv64/` 存放对应架构的 Ubuntu 软件包解包目录，`linux7/linux/` 存放两个 Linux 7 UAPI 头文件。
内核模块目录需要先以普通用户运行 `depmod -b <解包内核根目录> 7.0.0-34-generic` 生成依赖索引。

## 来源与边界

交叉编译器和 QEMU 来自 Ubuntu 官方软件包，全部在用户目录解包，没有安装到宿主系统。
ARM64 / RISC-V64 软件包来自 `ports.ubuntu.com/ubuntu-ports`：先用系统 Ubuntu archive keyring 校验 `InRelease`，再核对索引和各 `.deb` 的 SHA-256。
内核包、版本与校验值记录在 `lab/work/architecture-1001/arm-kernel/packages.json`；Python 包见同目录上层 `foreign-packages.json`。
Linux 7 的 `fs.h`、`dm-ioctl.h` 来自 Linux 官方源码标签，具体文件校验值记录在 ABI 报告中。

RISC-V64 尚无完整内核 USB 故障验证；ARM64 RAM guest 也不能代表完整 Ubuntu 桌面、保护启动安装器和真实硬件时序已通过。
跨架构 TCG 时间不用于与 x86 KVM 比较 CPU 性能或承诺恢复时延。
