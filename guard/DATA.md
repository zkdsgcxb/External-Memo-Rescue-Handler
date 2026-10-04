# USB 数据分区的 DM 恢复配套工具

现在优先使用 [统一后台入口](MANAGER.md)：登记一次，映射出现后自动维护，不需要选择根盘或数据盘模式。本页说明数据映射约束和隔离实验；生产接入使用安装后的可信入口。

这项实验扩展把现有 Guard 用到独立的 USB 数据分区。Linux 的 `dm-multipath` 承担块 I/O 与无路径排队；Guard 负责核验重连设备、更新已登记映射、调用内核路径探测，以及记录失败并结束后续恢复准入。恢复后继续使用原来的映射和挂载，应用可以沿用原有文件描述符。

它管理的是**预先建立并明确交给本项目的单路径 DM 映射**。它不会自动接管任意插入的 U 盘，也不会与 `multipathd` 同时控制映射。已有根盘/LVM 入口保持独立；数据模式不能接管承载根目录、`/workspace` 等系统挂载的整块物理盘。

## 职责与实现

| 部件 | 工作 |
| --- | --- |
| Linux `dm-multipath` | 稳定块设备、无路径排队、换表、内核路径探测 |
| 共用 C++ `controller.cpp` | 单 owner、串行恢复事务、截止时间、故障接管；根盘和数据盘共用这一份实现 |
| C++ `admission.cpp` / `Admission` | USB 身份、唯一候选、分区位置/容量、文件系统类型与 UUID、PARTUUID、持有 FD 与 diskseq 核验 |
| Python `admin/data.py` | 只读登记已有数据映射，检查布局、原分区挂载冲突和启动条件 |
| `guard/data.py` / `ram_environment.py` | 准备匹配的独立或复用 RAM 环境，配置 systemd/udev 集成 |
| systemd / udev | 管理服务、合计资源配额、阻止已登记原分区被桌面再次自动挂载 |
| 调用者 | 预先建立映射，并显式通过 `/dev/mapper/rr-data-*` 挂载文件系统 |

健康期仍通过持久化 libdevmapper 和 sysfs 查询状态，事件唤醒并合并，约每秒一次兜底检查；不会周期性调用 blkid 或读取用户数据。当前各实例会收到所有 block 事件，并非内核按登记盘过滤，但唤醒有合并上限。

新增数据实例**合计**限额为单核 20%、20 ms 周期、256 MiB 内存、禁用 swap；每实例另有 128 MiB 内存上限。两个数据盘不会各自再得到 20% CPU。原根盘 Guard 的配额单独计算。配额是上限，不是实测占用。

## 当前接入约束

- 使用当前已验证的 7.0 内核和匹配该内核的离线 C++ 管理包。普通 Ubuntu 启动无需根盘 Guard 或 `nompath`，但 stock `multipathd.service/socket` 不得同时控制设备；当前 RAM 的不同版本不能隐式热替换。独立 `.deb` 的可信安装与固定管理入口见 [安装说明](../research/2026-10-04/INSTALLATION-AND-TRUST.md)。
- 分区具有非空 USB 设备序列号、PARTUUID 和文件系统 UUID。准入策略支持 ext4、VFAT、exFAT；本轮实际 VM 验证的文件系统见实验报告，未测类型不能据此称为已验收。
- 映射名为 `rr-data-<名称>`，UUID 为 `RAMRESCUE-DATA-<标识>`。只接受现有一组、一路径、`queue_mode bio`、`round-robin` 和 `queue_if_no_path` 的约定表，且直接承载文件系统。不接管 stock multipath、多路径 SAN、LUKS、额外 LVM 或整盘无分区文件系统。
- 裸分区不能已经挂载、作为 swap 或同时属于其他映射。已有裸分区挂载必须先按正常文件系统流程退出，再以 DM 路径挂载；工具没有在线插入映射功能。
- 挂载应使用明确的 `/dev/mapper/rr-data-*`。裸分区与稳定映射会呈现相同文件系统 UUID，因此不能依赖二者竞争的 `by-uuid` 链接。登记拒绝指向原分区及其 UUID/标签的常见 fstab 配置；自定义 root 脚本仍需要调用者避免第二次挂载。

内核全局 `queue_if_no_path_timeout_secs` 在这里仅读取，不修改，避免影响根盘。数据恢复准入预算为 8 秒，接入要求当前全局配置至少 10 秒。该预算不是所有 I/O 的可取消时限；已经卡在下层驱动中的请求仍可能超时后不返回。

## 登记与启动

以下命令面向已经建立约定映射的专用数据分区。先按 [可信安装说明](../research/2026-10-04/INSTALLATION-AND-TRUST.md) 安装经审阅的离线包，再核对映射与挂载关系，明确选定设备后登记。示例名字不是本机设备推荐；命令不创建映射、不格式化、不运行 fsck、不挂载。

```bash
sudo /usr/bin/rescue-guard-admin manager install
sudo /usr/bin/rescue-guard-admin manager register --device /dev/mapper/rr-data-example
sudo /usr/bin/rescue-guard-admin manager doctor
```

根盘环境版本匹配时，通过 bind 视图复用同一工具 RAM；否则准备独立 `/run/ram-rescue-manager/rootfs`。启动再次核对身份、映射独占性及实际挂载关系，采用完整 C++ 控制器和共用权限限制。原分区的精确 `UDISKS_IGNORE` 规则防止桌面二次挂载，无路径期间的 DM 规则减少自动扫描。持久登记在重启后生效，但映射仍需由调用者自己的启动配置建立。

fstab 检查使用冷态准备时只读绑定的单个文件。原 inode 上编辑仍可见，原子替换后再次管理准入会拒绝并要求正常结束数据使用后重启；udev 自动启动沿用该冷态基线。修改登记设备的 fstab 规则后应重启核验。实际 mountinfo、swap、设备实例与身份仍实时检查；恢复及死亡接管不新增宿主根盘配置访问，也不需要 `CAP_SYS_PTRACE`。

停止维护会终结本次恢复准入，并在旧操作 fence 释放后关闭无路径排队；它不卸载文件系统，也不删除映射。原有消费者和文件系统应先正常退出，不能通过重启服务或删除事务记录开启无限重试。撤除管理集成、保留登记的冷升级和 A→B→A 回退见 [统一后台管理](MANAGER.md)。

## 隔离实验

```bash
python3 lab/standalone_data_probe.py \
  --reproduction-dir lab/work/REPRO \
  --package-dir lab/work/已构建管理包
```

按 [复现说明](../lab/README.md) 创建干净 Ubuntu 种子，再按安装说明构建当前离线包。该实验在普通 Ubuntu 的新 overlay 中增加两块一次性 USB 数据盘，不要求保护根盘启动。映射创建、分区、格式化只针对新虚拟镜像，不传入宿主块设备；通过真实包和固定管理入口接入，保持原文件描述符执行写入、fsync 和 O_DIRECT 读取，检查错盘拒绝、设备重编号、挂载/子挂载连续、只读保持及下一次冷启动的自动维护。结束后只对虚拟盘副本做只读文件系统检查。

当前结果见 [2026-10-04 普通 Ubuntu 数据盘验收](../research/2026-10-04/STANDALONE-DATA.md)，此前根盘和数据盘共同实验见 [2026-10-01 数据盘扩展报告](../research/2026-10-01/DATA-GUARD.md)。USB 硬件缓存掉电损失、全部文件系统/应用行为和任意设备故障仍不在这些实验的证明范围内。
