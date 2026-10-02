# 统一后台维护

使用一个入口登记和查看保护对象，不需要选择“根盘模式”或“数据盘模式”。程序根据已有映射与登记身份选择核验策略；已有根盘控制器由启动流程负责，数据映射出现时由 systemd 自动启动同一套 Guard。

新的保护镜像只使用完整 C++ 运行时；管理命令与 `admin/` 中的冷态登记、只读校验仍由 Python 实现。启动时校验并复用 RAM 内的 ELF 与共享库，一小段 exec 入口交接到原生程序，没有新增常驻 shell。缺少原生运行包时明确拒绝启动，不回退到旧 Python 控制器。因此仍运行旧 Python 保护包的会话不能用新管理工具新增维护实例，需先完成原生镜像部署并进入新保护启动。

正常维护不弹窗，也不需要拔插后手动运行命令。没有新增定时扫盘的总管进程：`ram-rescue-manager.service` 只在启动时准备 RAM 文件，完成后为 `active (exited)`；持续监视仍由原有 Guard 实例承担。

## 当前管理范围

**管理对象是已登记、已建立的稳定 DM 映射。** 根盘映射由现有 initramfs 集成建立；数据映射由调用者或自己的启动配置建立。统一维护服务接手其后续恢复，不创建文件系统、不自动挂载，也不把正在使用的裸分区在线改接。

根盘已有 owner 会被直接识别，同一个根盘上的根 LV 与 shared LV 属于同一保护对象。登记数据映射时检查 USB 身份、分区和文件系统；首次登记后按原身份验证重连盘，不能把后来插入的另一块盘重新学习为原盘。

陌生设备保持系统原有行为。数据模式支持的文件系统、映射拓扑、无序列号设备限制以及不能覆盖的故障，沿用 [数据映射边界](DATA.md)。原根盘同一物理盘上的 EXCHANGE 等裸分区，不会因为安装统一服务就自动获得保护。EFI 继续由已有原生 udev/systemd 集成处理。

需要自动挂载、嵌套文件系统或 bind 子挂载时，可为已登记映射生成 [systemd 挂载计划](MOUNTS.md)。挂载依赖由 systemd 维护，Guard 继续只负责块设备准入和恢复；计划生成器不会直接更改当前挂载。共同断联、连续恢复及资源结果见 [QEMU 优化验收](../research/2026-10-01/OVERNIGHT-OPTIMIZATION.md)。

## 安装与使用

在当前已进入保护启动、根盘 Guard 正常运行的本机上执行：

```bash
pkexec python3 guard/manage.py install
pkexec /usr/local/sbin/rescue-guard status
```

安装器添加统一入口、持久化 systemd/udev 配置和独立代码版本，不替换正在运行的根盘代码，不改 initrd、GRUB、rEFInd 或 fstab。后续启动自动准备维护环境。已有根盘自动出现在统一状态中，不要求重新登记：

```bash
# 可选核对：自动认出根卷对应的已有保护实例，不启动第二个 owner。
pkexec /usr/local/sbin/rescue-guard register --device /
```

对于已经按约定建立的额外数据映射，只需登记一次：

```bash
pkexec /usr/local/sbin/rescue-guard register \
  --device /dev/mapper/rr-data-example
```

也可指定该映射的唯一底层 USB 分区；程序查找对应的受支持映射，不要求用户声明设备用途。若选中裸分区且尚无稳定映射，会明确拒绝登记。映射名字和 UUID 的接入约定见 [DATA.md](DATA.md)。

登记后，当前存在的映射立即进入维护；后续匹配的 DM 映射出现时自动启动。**重启后自动维护，不等于自动重建数据映射**：若未配置数据映射的启动创建，状态会显示 `waiting_for_map`。精确的裸分区自动挂载排除规则仍保留，因此登记前应安排好其映射创建方式。

状态中 `ready` 表示当前控制器已就绪；`waiting_for_map` 表示登记存在但映射尚未出现；`expired/failed/interrupted/blocked` 需要检查日志，不能通过重复热插拔清空旧事务、无限延长恢复期限。过去的事件另列为 `last_state`，不将它当作当前设备仍受保护的证据。

```bash
systemctl status ram-rescue-manager.service
journalctl -b -u 'ram-rescue-maintain@*.service'
```

命令触发的管理员认证使用本地系统认证。后台恢复本身不弹认证窗口。

## 升级管理包

本次升级入口支持只有根盘保护、尚无登记数据盘的已有安装。在 [保护镜像升级](README.md#升级已有保护入口) 完成后执行：

```bash
pkexec python3 "$PWD/guard/manage.py" upgrade
```

入口核验已安装文件和收据，要求数据登记目录为空且无运行中或失败的数据控制器。保存旧配置、链接目标与收据后，安装独立的新代码版本，原子更新持久配置和命令链接，执行 `daemon-reload`；不准备当前 RAM 环境，不启动或停止控制器。失败会尝试恢复旧配置，外部改动或回滚失败会保留待检查记录。

下一次从已升级的保护项启动时，管理服务才使用新 C++ 包。当前启动中的旧 owner 和 RAM alias 保留；`rescue-guard status` 可继续查看当前根盘。已登记数据盘的在线升级不在此入口范围内。

## 生命周期与资源

- 精确的 DM_NAME + DM_UUID udev 规则设置 `SYSTEMD_WANTS`，只启动对应登记的维护实例；裸 USB 分区规则仅排除桌面自动挂载。
- Guard 不绑定裸设备的 systemd 生命周期。USB 消失时，Guard、稳定映射、原挂载与原进程继续存在，便于原有 I/O 排队和恢复。
- 持久登记放在 `/etc/ram-rescue-manager/devices/`，不保存 `/dev/sdX`、sysfs 实例路径或 diskseq；每次冷启动重新核验、生成当次运行配置。
- 代码与当次登记复制到 RAM。准入及其子进程继承同一 owner fence；准备过程直接进入共用 `run_owned()`，不释放锁后另启竞争者。
- `ExecStopPost` 必须匹配本次 systemd invocation、owner 和配置摘要才能接管。未获得旧实例锁的新服务，没有权限在退出时清理旧实例。
- 继续使用 `Restart=no` 和终态记录，不增加重复恢复循环。数据实例合计 CPU 20%／20 ms、内存 256 MiB、零 swap；原根盘的配额独立保留。统一入口没有把三个 Guard 合并成一个进程。

## 撤除与失败检查

先按正常文件系统流程退出并删除额外数据映射，再撤除统一管理集成：

```bash
pkexec /usr/local/sbin/rescue-guard uninstall
```

注册的数据映射仍存在时，撤除会拒绝，防止移除裸分区排除规则后出现二次挂载。撤除不停止现有根盘保护；登记、版本文件和安装收据保留。旧设备数据库里的属性可能保留到重新插入设备。

首次安装失败会保留 `/var/lib/ram-rescue-manager/install.json` 及已写入内容，明确记录未完成状态；不会覆盖已有安装，也没有通用就地升级或失败后自动重装功能。核对日志和收据后再处理，不能把部分安装视为已启用。

实验与部署证据见 [统一后台维护验收](../research/2026-10-01/UNIFIED-MANAGER.md)。
