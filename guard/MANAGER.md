# 统一后台维护

使用一个入口登记和查看保护对象，不需要选择“根盘模式”或“数据盘模式”。程序根据已有映射与登记身份选择核验策略；已有根盘控制器由启动流程负责，数据映射出现时由 systemd 自动启动同一套 Guard。

新的保护镜像只使用完整 C++ 运行时；管理命令与 `admin/` 中的冷态登记、只读校验仍由 Python 实现。启动时校验 RAM 内的 ELF 与共享库，一小段 exec 入口交接到原生程序，没有新增常驻 shell。普通 Ubuntu 可从离线安装包独立准备数据维护环境，无需先进入保护根盘启动；当前已运行的不同版本 RAM 环境不会被热替换。缺少匹配当前内核的原生运行包时明确拒绝启动。

正常维护不弹窗，也不需要拔插后手动运行命令。没有新增定时扫盘的总管进程：`ram-rescue-manager.service` 只在启动时准备 RAM 文件，完成后为 `active (exited)`；持续监视仍由原有 Guard 实例承担。

## 当前管理范围

**管理对象是已登记、已建立的稳定 DM 映射。** 根盘映射由现有 initramfs 集成建立；数据映射由调用者或自己的启动配置建立。统一维护服务接手其后续恢复，不创建文件系统、不自动挂载，也不把正在使用的裸分区在线改接。

根盘已有 owner 会被直接识别，同一个根盘上的根 LV 与 shared LV 属于同一保护对象。登记数据映射时检查 USB 身份、分区和文件系统；首次登记后按原身份验证重连盘，不能把后来插入的另一块盘重新学习为原盘。

陌生设备保持系统原有行为。数据模式支持的文件系统、映射拓扑、无序列号设备限制以及不能覆盖的故障，沿用 [数据映射边界](DATA.md)。原根盘同一物理盘上的 EXCHANGE 等裸分区，不会因为安装统一服务就自动获得保护。EFI 继续由已有原生 udev/systemd 集成处理。

需要自动挂载、嵌套文件系统或 bind 子挂载时，可为已登记映射生成 [systemd 挂载计划](MOUNTS.md)。挂载依赖由 systemd 维护，Guard 继续只负责块设备准入和恢复；计划生成器不会直接更改当前挂载。共同断联、连续恢复及资源结果见 [QEMU 优化验收](../research/2026-10-01/OVERNIGHT-OPTIMIZATION.md)。

## 安装与使用

先按 [可信安装说明](../research/2026-10-04/INSTALLATION-AND-TRUST.md) 审阅、构建和安装离线包。特权操作只使用 root 所有的固定入口，不从用户可写工作区提权加载 Python。普通启动不要求根盘 Guard 或 `nompath`，但内核须匹配包记录、stock `multipathd.service/socket` 不得竞争，且全局 `dm_multipath.queue_if_no_path_timeout_secs` 须由管理员显式配置为至少 10 秒；默认 0 会被拒绝，工具不擅自修改全局策略。

```bash
sudo /usr/bin/rescue-guard-admin manager install
sudo /usr/bin/rescue-guard-admin manager status
sudo /usr/bin/rescue-guard-admin manager doctor
```

安装器添加统一入口、持久化 systemd/udev 配置和独立代码版本，不替换正在运行的根盘代码，不改 initrd、GRUB、rEFInd 或 fstab。后续启动自动准备维护环境。已有根盘自动出现在统一状态中，不要求重新登记：

```bash
# 可选核对：自动认出根卷对应的已有保护实例，不启动第二个 owner。
sudo /usr/bin/rescue-guard-admin manager register --device /
```

对于已经按约定建立的额外数据映射，只需登记一次：

```bash
sudo /usr/bin/rescue-guard-admin manager register \
  --device /dev/mapper/rr-data-example
```

也可指定该映射的唯一底层 USB 分区；程序查找对应的受支持映射，不要求用户声明设备用途。若选中裸分区且尚无稳定映射，会明确拒绝登记。映射名字和 UUID 的接入约定见 [DATA.md](DATA.md)。

登记后，当前存在的映射立即进入维护；后续匹配的 DM 映射出现时自动启动。**重启后自动维护，不等于自动重建数据映射**：若未配置数据映射的启动创建，状态会显示 `waiting_for_map`。精确的裸分区自动挂载排除规则仍保留，因此登记前应安排好其映射创建方式。

`manager status` 中 `ready` 表示当前控制器已就绪；`waiting_for_map` 表示登记存在但映射尚未出现；`expired/failed/interrupted/blocked` 需要检查日志，不能通过重复热插拔清空旧事务、无限延长恢复期限。过去的事件另列为 `last_state`，不将它当作当前设备仍受保护的证据。按需 `doctor` 进一步核对本次启动、实际 owner、运行副本、映射和依赖；其 `ready` 仍不保证文件系统与应用没有错误。

```bash
systemctl status ram-rescue-manager.service
journalctl -b -u 'ram-rescue-maintain@*.service'
```

命令触发的管理员认证使用本地系统认证。后台恢复本身不弹认证窗口。

## 升级管理包

先以可信流程安装已验证的新包，再调用固定入口：

```bash
sudo /usr/bin/rescue-guard-admin manager upgrade
```

升级可以保留数据登记，但要求全部本项目数据 DM 映射已按正常流程退出，相关控制器均为 `inactive`；运行中、失败或正在退出的实例都会阻止升级。命令核验已有配置、规则和收据，备份后安装独立版本并切换持久入口，执行 `daemon-reload`；不准备或替换本次 RAM 环境，不启动或停止 owner。失败尝试恢复旧配置，外部改动或回退失败保留待检查记录。

新版本仅在下一次启动生效。根盘保护镜像需另按 [保护镜像升级](README.md#升级已有保护入口) 配套更新；普通数据维护不要求进入根盘保护项。冷回退时安装保留的旧包，再执行同一 `manager upgrade` 并重启。已有完整旧版本只有在代码、依赖清单和归档逐项一致时才复用，支持 A→B→A；不修补或覆盖被改动的旧目录，不提供活跃 owner 的热升级。

## 生命周期与资源

- 精确的 DM_NAME + DM_UUID udev 规则设置 `SYSTEMD_WANTS`，只启动对应登记的维护实例；裸 USB 分区规则仅排除桌面自动挂载。
- Guard 不绑定裸设备的 systemd 生命周期。USB 消失时，Guard、稳定映射、原挂载与原进程继续存在，便于原有 I/O 排队和恢复。
- 持久登记放在 `/etc/ram-rescue-manager/devices/`，不保存 `/dev/sdX`、sysfs 实例路径或 diskseq；每次冷启动重新核验、生成当次运行配置。
- 普通启动使用独立 `tmpfs,noswap`；匹配的根盘环境通过 bind 挂载复用同一 RAM 文件，不复制第二套工具页。服务的 `RootDirectory` 使用真实 `/run/ram-rescue-manager/rootfs`，管理路径 `tools` alias 不作为 systemd 根目录。
- 冷态准备只读绑定宿主 fstab 单文件；原 inode 上编辑可见，原子替换后再次管理准入拒绝并要求正常结束数据使用后重启。udev 自动启动沿用本轮冷态基线；修改相关 fstab 规则后应重启核验。实际挂载、swap 和设备身份仍实时检查。恢复/死亡接管不重新访问宿主 fstab，不增加 `CAP_SYS_PTRACE`。
- 代码与当次登记复制到 RAM。准入及其子进程继承同一 owner fence；准备过程直接进入共用 `run_owned()`，不释放锁后另启竞争者。
- `ExecStopPost` 必须匹配本次 systemd invocation、owner 和配置摘要才能接管。未获得旧实例锁的新服务，没有权限在退出时清理旧实例。
- 继续使用 `Restart=no` 和终态记录，不增加重复恢复循环。数据实例合计 CPU 20%／20 ms、内存 256 MiB、零 swap；原根盘的配额独立保留。统一入口没有把三个 Guard 合并成一个进程。

## 撤除与失败检查

先按正常文件系统流程退出并删除额外数据映射，再撤除统一管理集成：

```bash
sudo /usr/bin/rescue-guard-admin manager uninstall
```

注册的数据映射仍存在时，撤除会拒绝，防止移除裸分区排除规则后出现二次挂载。撤除不停止现有根盘保护；登记、版本文件和安装收据保留。旧设备数据库里的属性可能保留到重新插入设备。

首次安装失败会保留 `/var/lib/ram-rescue-manager/install.json` 及已写入内容，明确记录未完成状态；不会覆盖已有安装，也没有任意部分安装的就地修补或失败后自动重装功能。核对日志和收据后再处理，不能把部分安装视为已启用。

当前普通启动的真实离线包、两次冷启动、挂载与恢复结果见 [独立数据盘验收](../research/2026-10-04/STANDALONE-DATA.md)。此前部署记录见 [统一后台维护验收](../research/2026-10-01/UNIFIED-MANAGER.md)。
