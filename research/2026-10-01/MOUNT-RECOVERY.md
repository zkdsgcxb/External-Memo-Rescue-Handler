# 挂载与子挂载恢复验收

## 结论

完整 Ubuntu QEMU 中，保护根盘与两块额外数据盘由原有共用 Guard 核心维护。额外的 ext4 主挂载、其下的 FAT32 独立文件系统、三个 bind 挂载在 USB 断开/接回过程中保留原来的挂载 ID 和选项；四个数据工作进程保留原始文件描述符，恢复后继续 `fsync` 写入与 `O_DIRECT` 读回，数据摘要一致。

生产新增部分只是 `guard/mounts.py` 生成原生 systemd 单元。没有增加常驻 Python 进程、周期扫描、在线 fsck、自动 remount-rw 或第二个块设备恢复者。本次不部署到实机，也没有改实机 fstab、EFI 或正在运行的 Guard。

## 验证材料

- 命令：`python3 lab/mount_guard_probe.py`
- 最终源码完整结果：`lab/work/mounts-1001-054201-1011629/report.json`；可提交摘要：[2026-10-01-mounts.json](../../lab/results/2026-10-01-mounts.json)。
- 早期通过结果仍保留于 `lab/work/mounts-1001-044047-609585/report.json`。最终复验包含异常诊断内存优化，`owned_operation.py` SHA-256 为 `1c3febc34cd036d5e921ab7f49bf11eab9b85ee1085c1a7c21764d7270a4daf1`。
- 客户机：Ubuntu 24.04.5；Linux `7.0.0-34-generic`，x86-64 KVM；根盘 UAS，额外数据盘 USB storage。
- 数据盘：独立 64 MiB ext4、64 MiB FAT32 文件系统；均经预建稳定 DM multipath 映射访问。
- 当前全部 Guard runtime 在 init-bottom、PID 1 启动前复制到 RAM；根盘和两个数据盘的活动控制器运行本次源码。最初根盘映射创建仍使用已有验证过的启动集成。
- 28 项客户机验收全部通过；11 项挂载计划单元测试通过。
- 源码指纹与只读根盘种子前后不变；测试镜像正常卸载、正常关机；离线 `e2fsck -fn` 和 `fsck.fat -n` 都返回 0。
- 本次客户机内核日志没有 `WARNING:`、`I/O error` 或 `EXT4-fs error` 行。此结果不表示已经解决实机此前另一次启动观察到的内核 WARNING。

原始 `report.json` 包含具体单元、实际 `systemd-analyze verify` 输出、每次 mountinfo、进程 PID/start_ticks、控制器状态、日志和离线检查结果。稀疏 VM 种子未包含 autofs 模块；实验 initrd 单独加入与客户机内核匹配的 `autofs4` 模块，在 PID 1 启动前加载。正常系统也需要内核 autofs 支持才能使用 `.automount`。

## 验收覆盖

| 情形 | 本次证据 |
| --- | --- |
| 初始挂载缺失 | 只启动 `.automount` 时实际文件系统尚未挂载；首次访问后自动建立主挂载 |
| 嵌套文件系统 | FAT32 挂载于 ext4 目录树之下，依赖和停止顺序由 systemd 管理 |
| 子路径 bind | ext4 `/work` 与 FAT32 `/work` 分别绑定到独立访问路径，并持续持有文件描述符 |
| ext4 USB 断开/接回 | 原挂载与两个原进程继续工作，FAT32 工作负载同时推进 |
| FAT32 接回错误身份 | 相同 USB serial/PARTUUID、不同文件系统 UUID 被拒绝，原挂载仍存在 |
| 换回正确 FAT32 | 排队请求完成，主路径和 bind 路径读回数据与已确认写入一致 |
| 根盘断开/接回 | 根盘原 Guard 进程和用户空间继续存活，原根盘工作负载恢复、最终可写 |
| 同时维护范围 | 根盘和两个数据盘始终由统一管理入口查看，合计恰好三个既有控制器 |
| 文件系统只读 | 受控将 FAT32 改为只读，重复 start 及再次断开/接回都不会把它改为读写 |
| 超过恢复期限 | 关闭工作负载后触发无路径超时；正常卸载后新 mount 请求因 Guard 终态启动失败而被拒绝 |
| 完成与一致性 | 原始挂载 ID/选项、原进程身份、连续 ACK 和最终文件内容均检查；再正常卸载及离线只读检查 |

只读测试是明确的 `remount,ro` 场景，不是 ext4 日志中止或真实介质损坏实验。根盘的额外 LVM LV 单元具有专门的保护启动断言与单元测试；本次挂载树实验的新增消费者是额外 ext4/FAT32 数据盘，没有另建 root/shared 的重复挂载。

## 连续性与超时的实际含义

本次四个数据工作负载的单次 `fsync` 写入加直接读回最大用时：

| 路径 | 最大等待 |
| --- | ---: |
| ext4 主挂载 | 1.971 秒 |
| ext4 bind 子路径 | 2.034 秒 |
| FAT32 主访问路径，包含接错盘再换回 | 3.675 秒 |
| FAT32 bind 子路径，同一故障过程 | 3.675 秒 |

根盘工作负载最大写入/同步时间 1.399 秒。这些是最终功能验收中的观测值，不是延迟上界，也不是受控性能比较；本次没有据此声称 CPU 或内存收益。新增单元无常驻用户进程，但 systemd/autofs 对象仍会使用少量资源，未单独测量这部分。

已经存在的挂载不等于每次重连重新挂载。临时下层 USB 消失时，稳定 DM 设备仍在，所有上层挂载继续引用同一个内核对象，因而可以保留原进程的打开文件。真正卸载之后再 mount 只能重建访问路径，不能复活旧文件描述符。

实际 systemd 行为也要求区分两件事：显式停止所依赖的控制器会传播正常停止；控制器因终态自行退出，并不会仅凭 `Requires=` 就驱逐现有挂载。本次终态测试先记录保留的只读挂载，再正常卸载已关闭的消费者，最后确认新 mount 请求不能清空 Guard 事务、绕过终态。代码不使用 force/lazy 卸载。

## 接口与边界

`guard/mounts.py` 输入明确计划和持久化登记，输出新的审阅目录。实际安装继续使用 systemd 工具，配置生成器不写活动系统。完整使用说明见 [guard/MOUNTS.md](../../guard/MOUNTS.md)。

- 源设备只来自登记的 DM UUID；底层 `/dev/sdX` 不出现在生成的设备依赖里。
- 原有根挂载保持启动流程所有权，不接管 `/`、`/usr`、`/boot`、`/run` 等系统路径。
- 根盘额外 LV 消费者使用 `AssertKernelCommandLine=ram_rescue_guard=1`。这避免普通启动时根 Guard 因 Condition 被跳过，却被误认为 Requires 已经保证保护存在。
- bind 源必须在声明的受保护文件系统路径之内，但这种检查是词法检查。计划、挂载点和源目录须可信，不能通过符号链接逃出登记文件系统。此工具不声称隔离恶意介质内容。
- 不接受任意设备重定向、remount、自动修复选项或嵌套 automount。当前路径格式限定 ASCII，避免模糊的单元转义与路径解析。
- 初始挂载可以由原生自动挂载补齐；文件系统错误、永久下层阻塞、终态和被卸载的旧文件描述符仍保留既有能力边界。

设计依据为 systemd 官方的 [mount](https://www.freedesktop.org/software/systemd/man/latest/systemd.mount.html)、[automount](https://www.freedesktop.org/software/systemd/man/latest/systemd.automount.html)、[unit](https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html) 语义，并用实际客户机版本核验。
