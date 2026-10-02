# 实机 C++ 保护包部署与废弃观察器清退

2026-10-03（北京时间）。C++ 保护镜像和对应管理包已经写入本机，安装文件回读校验通过。**本次没有重启；当前会话仍由原 Python Guard 保护。下一次进入原有保护启动项，才会运行 C++。** 本报告不将镜像安装等同于实机启动或实际拔插验收。

## 清理与升级实现

- 删除 `guard/native/` 早期只读观察器的 7 个源码文件及其专用测试。它们没有当前恢复职责；历史实验通过固定 Git 提交继续复现。完整自动恢复只保留 `guard/native/runtime/`。
- `enroll.py` 现在也能只读登记已经处于保护映射上的本机：核验保留名称/UUID、原盘、表几何、排队状态和全部登记 LV 的直接依赖，完整身份复核后再检查一次拓扑。首次部署所需的直接 PV 登记继续保留。
- 新增 `upgrade.py`，在已有安装中校验匹配的 VM 报告、当前布局、内核、基础工具、镜像、收据和菜单。保存私有备份，原子替换同名保护 initrd；失败尝试回滚，未完成事务阻止继续升级。
- `manage.py upgrade` 为尚未登记数据盘的本机更新持久管理包。校验旧文件后保存配置和链接备份，安装独立代码版本，只执行 `daemon-reload`。不改当前 RAM alias，不启动第二个恢复进程。

本次没有改动 C++ 恢复核心，其实际 ELF SHA-256 仍为 `72d9d550079b4f73e1954e13f99fa614826758c623eec1d3026aa22bc891b21f`。没有新增日志守护进程、轮询或性能收益结论。

## 验证与实机结果

| 项目 | 结果 |
| --- | --- |
| 当前管理、实验与回归检查 | 249 项通过；包含保护布局登记、镜像升级和管理包失败回滚检查 |
| C++ 原生检查 | 核心 60、准入 37、控制器 13，共 110 项通过 |
| 本次完整 Ubuntu 生产包集成 | 32/32；根盘及 ext4/FAT 数据盘、原挂载、子挂载、原 FD、错误身份和终态检查通过 |
| VM 完整性 | 源码、种子镜像保持不变；实际服务启动 C++，无替换控制器命令的 drop-in；离线 ext4/FAT 只读检查返回 0 |
| 实机登记 | 当前受保护根卷和 shared 布局重新核验通过，无 DM 修改 |
| 镜像部署 | 已替换 `/boot/initrd.img-7.0.0-34-generic-ram-rescue`，文件散列与候选一致 |
| 管理包部署 | 已更新 `/usr/local/sbin/rescue-guard`；实际运行新安装命令，状态仍能识别旧会话的根盘 owner |
| 当前会话 | 原 Guard PID 683 保持 active/ready；DM 表不变，根卷与 shared 仍读写挂载；RAM 内核日志服务 active |
| 下一次实机启动 | 待用户重启进入保护项，尚未验收 C++ 实机启动与实际短断 |

新保护 initrd SHA-256：`8d1fe2eefcc911cbe77370024492c33166358d5661a707460e4ea8fe3790b232`。

内核、普通 initrd、GRUB 菜单和保护项 hook 的前后散列一致；未操作 rEFInd、EFI 或现有挂载。镜像中的救援密码数据库保持锁定，由现有启动准备服务在本机供给，不将密码数据库放入构建产物。

原始 VM 报告：`lab/work/cpp-int-1002-233809-370024/report.json`。构建、实际登记、升级结果和私有诊断保存在 `lab/work/host-cpp-deploy-20261002/`。公开检查、源码与 ELF 散列见 [部署证据](../../lab/results/2026-10-03-host-cpp-deployment.json)；设备登记和实机原始诊断未公开提交。

## 激活与回退

沿内置 SSD 的 rEFInd 进入 Ubuntu 的 GRUB，选择原来的：

```text
Ubuntu USB root protection (7.0.0-34-generic)
```

这个入口现在指向 C++ 镜像。启动后应核对实际 `/proc/<MainPID>/exe` 与清单散列、唯一 owner、根卷和 shared 的稳定映射、ready 状态，以及桌面和常用程序。当前窗口中直接 `systemctl restart ram-rescue-guard` 不是激活步骤。

不热替换的原因是现有协议：停止旧 owner 的 `ExecStopPost` 会关闭无路径排队并终结事务；新控制器发现旧事务后拒绝重新启动恢复循环。不能删除事务或换锁目录绕过单 owner 约束。

若新保护启动失败，原普通 Ubuntu 项仍可使用。旧保护镜像与收据保存在 `/var/lib/ram-rescue-guard/upgrades/20261002T160717Z-59f261ddf525/`；管理配置备份位于 `/var/lib/ram-rescue-manager/upgrade-kwv3af2c/`。旧安装版本作为本次部署回退资料保留，待新启动验收后再决定清除。升级入口的用法和边界见 [保护镜像升级](../../guard/README.md#升级已有保护入口)、[管理包升级](../../guard/MANAGER.md#升级管理包)。

## 现场故障资料

现有记录机制继续工作：

- `/run/ram-rescue-guard/state/`：状态、恢复事务与监督记录。
- `/run/ram-rescue-demo/var/log/kernel-live.log` 及轮转副本：直接从内核读取的有界 RAM 日志，每份约 4 MiB，合计约 8 MiB。
- `journalctl -b -u ram-rescue-guard.service`：当前启动的服务日志；本机已经启用持久 journal。
- 本次构建另保留独立调试符号，位于私有构建目录的 `build/native-runtime/guard-runtime.debug`，不增加救援 tmpfs 的负担。

发生问题后，优先记录时间、设备动作和应用表现；恢复后先保存 RAM 日志与事务文件，再考虑重启。持久 journal 位于根盘，断盘期间可能写不进去；RAM 记录不依赖根盘，但重启即失。这两类记录有互补作用，不代表能记录内核完全死锁之后的事件。
