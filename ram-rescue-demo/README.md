# RAM 救援终端 demo

这是运行时救援工具：Ubuntu 已经正常启动、随后 USB 根盘掉线时，尝试保留一个可认证、可诊断、可手动恢复 LVM 映射的文本入口。与正常系统共用内核；无法兜底 kernel panic、全局死锁或启动阶段尚未运行本服务的故障。

## 文件位置与当前状态

- 源码、制作过程、测试数据、打包结果：`/workspace/Project/External-Memo-Rescue-Handler/ram-rescue-demo`，位于 shared 卷。
- 管理员安装后，系统专用运行包：`/usr/local/lib/ram-rescue-demo`，位于 Ubuntu 系统卷。
- 独立救援密码的散列：`/etc/ram-rescue-demo/shadow`，只有 root 可读；不会读取或更改 Ubuntu 密码。
- 真正运行中的救援根目录：`/run/ram-rescue-demo`，独立 `tmpfs,noswap`。
- 本目录中的 `manifest.json` 记录实际镜像大小、构建内核和登记的设备身份。

制作完毕不等于已安装。必须运行下一节的管理员安装命令并看到成功提示，两个终端才会常驻并在以后开机时自动准备。

## 安装与第一次验证

在正常 Ubuntu 的终端执行：

```bash
sudo python3 /workspace/Project/External-Memo-Rescue-Handler/ram-rescue-demo/install.py
```

先输入本机 sudo 密码；然后设置一个独立救援密码，无最低长度要求，但不能为空，两次输入须一致。密码输入不会显示，也不会出现在命令参数或日志中。安装器会先进行隔离工具测试、错误密码拒绝测试和正确密码登录测试，再安装服务。服务就绪检查通过后才启用开机自启。

1. 按 `Ctrl+Alt+F9`，部分笔记本还需 `Fn`。
2. 用户名输入 `rescue`，密码输入刚设置的独立救援密码。
3. 依次运行 `rescue status` 和 `rescue verify`。前者只读内核状态，后者只对登记的盘做身份与 LVM 元数据读取。
4. 输入 `exit` 退出 root shell，返回密码登录界面。
5. 也验证一次 `Ctrl+Alt+F10`。两个终端互相独立，供一个命令卡住时换另一个入口。
6. 本机当前桌面通常位于 `Ctrl+Alt+F2`；以后可能改变，可在正常系统中通过 `/sys/class/tty/tty0/active` 确认。

不要为了测试而拔掉正在使用的根盘。安装和上述检查不会刷新真实 LVM 映射，也不会修复或重挂载文件系统。

在正常 Ubuntu 中查看就绪状态及实际内存占用：

```bash
sudo python3 /usr/local/lib/ram-rescue-demo/check.py
```

## 遇到故障时

切到 F9，使用 `rescue` 登录。救援 shell 的 `/` 是 RAM 工具环境，不是原 Ubuntu 根目录。

```text
rescue status
rescue log
rescue verify
```

- `status` 列出匹配 USB 序列号的磁盘、登记 LV 当前依赖、原系统 ext4 挂载状态；不扫描所有磁盘。
- `log` 显示当前内核环形日志。
- `verify` 对 USB VID/PID/序列号、容量、分区 UUID、PV UUID、VG UUID/名称逐项核对。
- 找不到盘、出现重复序列号、身份不符时会停止；不会根据新的盘符盲目修改映射。

当盘重新出现、`verify` 成功，而原 LV 仍引用旧设备时：

```text
rescue refresh ubuntu
```

屏幕会说明计划操作，并要求手动输入：

```text
REFRESH vgportable/ubuntu
```

需要恢复 shared 卷时，单独运行：

```text
rescue refresh shared
```

确认文本为 `REFRESH vgportable/shared`。每次只尝试刷新一个登记的、已经激活的线性 LV；不自动激活未知卷，不修改 PV 元数据，不执行 fsck，不重挂载，不重置 USB，不关机。

如果已正确依赖当前设备，命令直接报告无需刷新。确认后会再次核验设备身份，防止等待输入期间盘符发生变化。刷新使用系统自带 LVM，并禁用对 udev、D-Bus、监控守护进程的等待；与宿主共用 `/run/lock/lvm` 的 RAM 锁目录，避免两个相互隔离的 LVM 锁空间。

LVM 刷新可能等待内核 I/O；用户空间超时不能保证打断 D-state。如果一个终端卡住，尝试另一个终端。内核整体不可调度时两个终端也可能不可用。

刷新成功只表示映射重新指向已核验设备，不代表 ext4、应用或先前失败的写入已修复。查看 `rescue log`，再判断是否抢救数据或重启。

## 文件系统检查与数据转移

包内包含 `/sbin/e2fsck`、`blkid`、`dmsetup`、`lvm`、`lsblk`、`findmnt` 和 BusyBox 常用工具。

**不要对仍被宿主挂载的根卷或 shared 卷执行修复型 e2fsck，也不要把同一个文件系统再挂载一次来写入。** 独立 chroot 不会卸载原系统的文件系统。宿主挂载信息可看：

```text
cat /proc/1/mountinfo
```

离线修复通常应在 Live 系统中、明确卸载目标卷之后进行。本 demo 优先提供访问恢复与诊断，不自动进行文件系统修复。

救援环境里的 `/mnt` 可用于人工挂载确认过的另一块接收盘。正常系统 root 可从 `/proc/1/root` 访问，但故障时访问它同样可能失败或阻塞；不要在救援终端启动时自动进入这个目录。

## 日志、内存与边界

- 工具展开约 55 MiB，以 `manifest.json` 为准；运行进程、认证、日志会再占用内存，安装后 `check.py` 给出实际值。
- 救援文件系统上限 256 MiB，按使用增长，不预占全部容量。
- 整个救援 slice 的内存上限为 768 MiB；文件系统设 `noswap`，所有救援服务设 `MemorySwapMax=0`。不关闭正常 Ubuntu 的 swap，也不依赖它。
- 独立日志进程直接读取内核消息，存入 `/var/log/kernel-live.log`，每个文件最大约 4 MiB，保留一个轮转副本，总量约 8 MiB。没有网络监听或远程上传。
- `/var/log/rescue-actions.log` 记录 helper 调用动作；交互式 shell 的完整输出不自动录制。
- RAM 中的日志、临时文件、任何手动复制进去的数据都会在重启或卸载 demo 后消失。恢复后应先把有价值的日志复制到持久存储。
- 独立密码在启动时复制进 RAM；退出终端请执行 `exit`，不要把 root shell 留在无人看管的屏幕上。
- 目前不提供网络登录、不改 GRUB/initramfs、不在启动失败前期提供救援终端、不保证故障盘或内核恢复。

## 卸载与维护

先退出两个救援 shell，在正常 Ubuntu 终端执行：

```bash
sudo python3 /usr/local/lib/ram-rescue-demo/uninstall.py
```

只移除本 demo 的服务、密码散列和系统运行包；shared 卷上的源码、测试数据与打包结果保留。若 RAM 挂载仍忙碌，会保留安装文件并报告，避免在使用中强制删除。

系统工具或库更新后，建议在健康系统中重新构建、测试、卸载并重新安装。镜像内的库是制作时的副本，不会自动跟随 apt 更新。重建前确认原设备仍是 `/dev/sda`，否则构建器会拒绝而不是登记其他磁盘。

```bash
cd /workspace/Project/External-Memo-Rescue-Handler/ram-rescue-demo
python3 build.py
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 tests/smoke.py
```

## 现成开源方案与复用范围

- [BusyBox](https://busybox.net/)：使用本机 Ubuntu 的静态 BusyBox，提供 shell、密码校验和基本工具，没有自写密码算法。
- [systemd debug-shell](https://github.com/systemd/systemd/blob/main/units/debug-shell.service.in)：提供预运行文本终端的现成机制。原始调试服务没有本 demo 的独立 RAM 工具环境与密码登录设计。
- [SystemRescue](https://www.system-rescue.org/manual/Booting_SystemRescue/)：成熟的独立救援系统，可加载进 RAM；通常需要另行启动。
- [zramroot](https://github.com/Neol00/zramroot)：把完整正常系统放入 RAM，适合另一种内存预算。
- 本 demo 的自写部分主要是本机打包/部署、日志轮转、设备身份核验与人工确认后调用现成 LVM；没有修改内核或实现文件系统修复算法。

## 验证结果

制作验证详情见同目录 `VALIDATION.md`。管理员安装器会再执行需要真实 root 权限的完整登录测试和实际服务检查。未完成管理员安装和现场故障验证前，不应把 demo 当作已经证明可承受掉盘的方案。
