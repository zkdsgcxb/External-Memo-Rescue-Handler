# 指令集与 ABI 范围

恢复策略和状态机共用一套 Python 实现；不同指令集不复制控制流程。
`runtime/linux_abi.py` 集中定义 Linux ioctl 请求和 libdevmapper 的 `dm_info` 布局，启动即核对运行进程的 ABI。

目前允许 Linux 上的 **x86_64、aarch64、riscv64，64 位、小端、LP64**。
同时检查指针、`size_t` 和 `int` 长度；仅看到 `uname` 的 64 位机器名还不够。
32 位进程、大端和未验证架构会在设备操作前明确报错，不猜测其 ioctl 编码。
这项约束独立于原有的内核版本、multipath target 版本和设备身份检查；没有增加旧内核兼容分支。

## 已验证的层次

| 验证 | x86_64 | ARM64 | RISC-V64 |
|---|---|---|---|
| 真实 Linux 7 UAPI / libdevmapper C 头文件与 Python ctypes 对照 | 原生执行通过 | 交叉编译、QEMU user-mode 执行通过 | 交叉编译、QEMU user-mode 执行通过 |
| Admission、身份、owner fence、事件解析与 ABI 边界策略 | 原生测试通过 | ARM64 Python 实际执行通过 | RISC-V64 Python 实际执行通过 |
| 可选 C++ 健康观察器解析测试 | 原生测试 | 交叉编译、QEMU 执行通过 | 交叉编译、QEMU 执行通过 |
| 完整内核中的 USB / DM 恢复 | 完整 Ubuntu VM 与既有实机证据 | 同版 Ubuntu 7.0 ARM64 RAM guest，11 项通过 | 尚未验证 |
| 完整 Ubuntu 桌面与启动安装流程 | 既有本机验证 | 尚未验证 | 尚未验证 |

**QEMU user-mode 共用宿主内核，不等于 ARM64 / RISC-V 内核中的 USB 故障实验。**
`lab/architecture_probe.py` 验证前两行；`lab/arm64_probe.py` 单独运行真正的 ARM64 内核。
ARM64 实验使用 Ubuntu 同版本内核和软件包、RAM 中的最小用户空间；它不包含桌面、systemd 启动接管或真实 ARM USB 控制器。
不同架构必须在对应系统上准备其原生 Python、libdevmapper、blkid/LVM 等二进制，不能复用 x86_64 救援归档。
现有早期 `ram-rescue-demo/build.py` 仍有本机磁盘登记与 x86_64 路径约束，不是通用交叉构建安装器。

## 接口来源与约束

- [Linux 7 fs.h](https://github.com/torvalds/linux/blob/v7.0/include/uapi/linux/fs.h)：`BLKGETSIZE64` 的请求号编码 **sizeof(size_t)**，输出仍为 64 位数；`BLKGETDISKSEQ` 使用 64 位输出，`BLKSSZGET` 是历史 `_IO` 接口。
- [Linux 7 ioctl.h](https://github.com/torvalds/linux/blob/v7.0/include/uapi/asm-generic/ioctl.h)：三个验证架构采用同一通用 ioctl 编码。
- [Linux 7 dm-ioctl.h](https://github.com/torvalds/linux/blob/v7.0/include/uapi/linux/dm-ioctl.h)：`DM_MPATH_PROBE_PATHS = _IO(0xfd, 18)`；其完成不代表文件系统和所有上层进程都健康。
- [libdevmapper 公开头文件](https://github.com/lvmteam/lvm2/blob/v2_03_16/libdm/libdevmapper.h)：`dm_info` 含 12 个 32 位字段。本次使用 Ubuntu `libdevmapper-dev 1.02.185` 头文件实际编译比较，结构长 48 字节、对齐 4 字节。

ABI 模块仅启动时执行检查；健康循环没有新增探盘、子进程或跨架构判断。
复现实验、工具来源、报告路径与具体结果见 [架构实验报告](../research/2026-10-01/ARCHITECTURE-VALIDATION.md)。
