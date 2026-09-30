# USB 数据分区的 DM 恢复配套工具

这项实验扩展把现有 Guard 用到独立的 USB 数据分区。Linux 的 `dm-multipath` 承担块 I/O 与无路径排队；Guard 负责核验重连设备、更新已登记映射、调用内核路径探测，以及记录失败并结束后续恢复准入。恢复后继续使用原来的映射和挂载，应用可以沿用原有文件描述符。

它管理的是**预先建立并明确交给本项目的单路径 DM 映射**。它不会自动接管任意插入的 U 盘，也不会与 `multipathd` 同时控制映射。已有根盘/LVM 入口保持独立；数据模式不能接管承载根目录、`/workspace` 等系统挂载的整块物理盘。

## 职责与实现

| 部件 | 工作 |
| --- | --- |
| Linux `dm-multipath` | 稳定块设备、无路径排队、换表、内核路径探测 |
| 共用 `path_guard.py` | 单 owner、串行恢复事务、截止时间、故障接管；根盘和数据盘共用这一份实现 |
| `data_recovery.py` / `Admission` | USB 身份、唯一候选、分区位置/容量、文件系统类型与 UUID、PARTUUID、持有 FD 与 diskseq 核验 |
| `data_guard.py` | 只读登记已有数据映射，检查布局、原分区挂载冲突和启动条件 |
| `guard/data.py` | 把控制器和登记放进现有 RAM 环境，配置本次启动有效的 systemd/udev 集成 |
| systemd / udev | 管理服务、合计资源配额、阻止已登记原分区被桌面再次自动挂载 |
| 调用者 | 预先建立映射，并显式通过 `/dev/mapper/rr-data-*` 挂载文件系统 |

健康期仍通过持久化 libdevmapper 和 sysfs 查询状态，事件唤醒并合并，约每秒一次兜底检查；不会周期性调用 blkid 或读取用户数据。当前各实例会收到所有 block 事件，并非内核按登记盘过滤，但唤醒有合并上限。

新增数据实例**合计**限额为单核 20%、20 ms 周期、256 MiB 内存、禁用 swap；每实例另有 128 MiB 内存上限。两个数据盘不会各自再得到 20% CPU。原根盘 Guard 的配额单独计算。配额是上限，不是实测占用。

## 当前接入约束

- 使用当前已验证的 7.0 内核及已有保护启动 RAM 环境；启动器要求根盘 Guard active、`nompath` 和 stock multipathd 未启动。这一轮没有打包独立发行版通用服务。
- 分区具有非空 USB 设备序列号、PARTUUID 和文件系统 UUID。准入策略支持 ext4、VFAT、exFAT；本轮实际 VM 验证的文件系统见实验报告，未测类型不能据此称为已验收。
- 映射名为 `rr-data-<名称>`，UUID 为 `RAMRESCUE-DATA-<标识>`。只接受现有一组、一路径、`queue_mode bio`、`round-robin` 和 `queue_if_no_path` 的约定表，且直接承载文件系统。不接管 stock multipath、多路径 SAN、LUKS、额外 LVM 或整盘无分区文件系统。
- 裸分区不能已经挂载、作为 swap 或同时属于其他映射。已有裸分区挂载必须先按正常文件系统流程退出，再以 DM 路径挂载；工具没有在线插入映射功能。
- 挂载应使用明确的 `/dev/mapper/rr-data-*`。裸分区与稳定映射会呈现相同文件系统 UUID，因此不能依赖二者竞争的 `by-uuid` 链接。登记拒绝指向原分区及其 UUID/标签的常见 fstab 配置；自定义 root 脚本仍需要调用者避免第二次挂载。

内核全局 `queue_if_no_path_timeout_secs` 在这里仅读取，不修改，避免影响根盘。数据恢复准入预算为 8 秒，接入要求当前全局配置至少 10 秒。该预算不是所有 I/O 的可取消时限；已经卡在下层驱动中的请求仍可能超时后不返回。

## 临时登记与启动

以下命令面向**已建立约定映射的专用数据分区**。先查看映射表与挂载关系，明确选定设备后再登记；示例设备名不能直接当作本机目标。命令不创建映射、不格式化、不运行 fsck、不挂载。

```bash
# 在项目目录执行；已有登记文件不会被覆盖。
pkexec python3 guard/data.py enroll \
  --map rr-data-example --partition /dev/已选定分区 \
  --output "$PWD/lab/work/data-enrollment.json"

pkexec python3 guard/data.py start \
  --enrollment "$PWD/lab/work/data-enrollment.json"

systemctl status ram-rescue-data-rr-data-example.service
sudo cat /run/ram-rescue-data/rr-data-example/state/path-state.json
```

启动会再次核对登记与现状，将代码复制到独立内容哈希目录，不替换正在运行的根盘代码；为原分区添加精确 USB 属性匹配的 `UDISKS_IGNORE`，为新 DM 映射抑制无路径期间的自动扫描。验证规则已经生效后，才启动数据 Guard。配置、服务和规则均在 `/run`，不修改 initrd、GRUB、rEFInd 或 fstab。

```bash
pkexec python3 guard/data.py stop --map rr-data-example
```

`stop` 是**终止本次保护**。接管流程停止新设备准入，并在旧操作 fence 释放后关闭无路径排队；它不卸载文件系统，也不删除映射。规则和证据保留至重启，防止裸分区被重新自动挂载。同一映射的本次事务不能通过再次 `start` 清空重开；启动中途失败也会保留已经写入的预防规则与配置，需检查失败原因。原有文件系统应先正常退出，再安排新的测试会话。

## 隔离实验

```bash
python3 lab/data_guard_probe.py \
  --build-dir lab/work/hb-build-v4 \
  --enrollment lab/work/hb-enroll-0930/enrollment.json \
  --seed-report lab/work/bootevo-0930-173036-39819/report.json
```

实验复用完整 Ubuntu 保护启动镜像，在新的 overlay 中增加两块一次性 USB 数据盘。映射创建、分区、格式化只针对新虚拟镜像；不传入本机块设备。实验用实际登记/启动器接入数据 Guard，保留原进程和打开的文件描述符执行写入、fsync 和直接读取，并检查错误身份拒绝、两个数据盘分别/共同重接、原根盘持续工作及卸载后的只读文件系统检查。

可审阅结果见 [2026-10-01 数据盘扩展报告](../research/2026-10-01/DATA-GUARD.md)。USB 硬件缓存掉电损失、全部文件系统/应用行为以及任意设备的故障类型仍不在这些实验的证明范围内。
