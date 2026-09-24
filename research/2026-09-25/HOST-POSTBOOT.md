# HWE 7.0 首次实机启动检查

后续用户已要求并完成旧 6.8 清退，见 [安装与清退记录](HOST-INSTALL.md)。本文保留首启检查时的状态，其中“旧内核保留”不再表示当前状态。

日期：2026-09-25。**已实际启动 `7.0.0-34-generic`，初步启动与存储检查通过。** 用户反馈桌面显示、声音和常用程序“暂时正常”。旧 6.8 内核继续保留。本文不表示长期稳定、休眠、外接屏或性能收益已验收。

此前安装操作和回退入口见 [安装记录](HOST-INSTALL.md)，本次脱敏结果及原始记录哈希见 [host-postboot-result.json](host-postboot-result.json)。

## 当前检查结果

| 项目 | 结果 |
|---|---|
| 内核与引导入口 | `uname -r` 为 `7.0.0-34-generic`；固件 `BootCurrent` 仍为内置 SSD 的 rEFInd |
| 根卷与工作区 | 原 LVM/ext4 根卷和 shared 卷均为读写挂载；USB 存储仍使用 UAS |
| 实际小文件 I/O | 两个卷各写入 4 KiB 临时文件，文件/目录 `fsync`、重新打开回读、`O_DIRECT` 直接读取比对均通过；临时文件已删除 |
| 存储日志 | 截至取证窗口，未匹配到块 I/O 错误、ext4/JBD2 错误、USB 断联、UAS 错误恢复或内核锁死模式 |
| 系统与包状态 | systemd 为 running；系统和用户管理器当前没有 failed units；`dpkg --audit` 无异常 |
| 桌面与显卡 | Wayland 会话 active；Intel i915 驱动内屏；NVIDIA 仍为 nouveau |
| 网络与音频 | NetworkManager 报 connected/full；PipeWire、pipewire-pulse、WirePlumber 均 active，默认音频输入输出存在；用户暂时未报告异常 |
| 开发工具 | Git、Python、GCC 版本命令可执行；这不是完整项目构建验收 |
| RAM 救援 | prepare、日志、`ram-rescue@tty9`、`ram-rescue@tty10` 服务均 active/success；RAM 挂载含 `noswap` |
| 回退保留 | 旧 6.8 image/modules/modules-extra 仍为手动安装；本轮没有清理旧内核 |

小文件直接回读用于避免只把缓存命中视为存储正常，不等价于断电耐久性或全盘检查。RAM 服务状态也不能替代人工登录及真实故障救援验证。本次没有拔盘、主动休眠或更换驱动。

## 日志中保留的注意项

对比当前 7.0 与上一次 6.8 的可读取启动日志，主要的 ACPI 缺失对象、Bluetooth 插件/SAP 提示，以及 nouveau `0x00731341` 控制命令失败，在旧版本下均已有记录。该 nouveau 消息两次各出现 64 条：旧启动约 750 秒时集中出现，本次约 6.1 秒时集中出现；截至约 311 秒的对比窗口没有继续出现。

本次还在约 13.6 秒出现一条 `nouveau ... gsp: intr 00001000`，前一次保留日志中未见。其实际功能影响尚未确定，不能直接判为无害，也不能仅凭这一条就归因为新内核回归。当前只有 Intel 内屏连接并启用，未检验 NVIDIA 外接显示或 GPU 负载。

Bluetooth 当前为软件禁用、硬件未禁用、`Powered=false`，没有开启或配对测试，因此不能将音频服务正常推广为蓝牙功能已通过。

桌面启动日志另有短暂应用 scope/GDM 提示，但当前没有残留 failed units。系统“当前健康”与“启动全过程没有任何报错”不是同一结论。两次启动观察时长不同，也不能用错误条数推导新版性能或可靠性改善。

## 后续保留的验收边界

初步启动已经通过，继续保留旧内核。在正常使用过程中再验收休眠/唤醒、外接显示和较长开发负载；真正启动旧内核的回退演练也尚未执行。此次升级没有为实际根卷部署项目里的 Guard/multipath 自动保护，不能据此进行工作根盘拔插实验。

原始检查和日志对比保存在 Git 忽略的 `lab/work/host-upgrade-20260925/`。项目公开记录只保留结果、设备类别及证据哈希。
