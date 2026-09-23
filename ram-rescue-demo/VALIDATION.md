# 制作验证记录

日期：2026-09-23（Asia/Shanghai）。构建环境：本机 Ubuntu 24.04，6.8.0-139-generic，x86_64。

## 已完成

- 从本机已安装工具构建自包含 rootfs；普通文件总计 57,644,362 字节（54.97 MiB），压缩包 21,914,636 字节（20.90 MiB）。以 `manifest.json` 与 SHA-256 校验文件为准。
- 12 项恢复逻辑测试通过，输出在 `work/unit-tests.log`：重新枚举、重复序列号、错误序列号/容量/PV UUID/分区 UUID/LV UUID、非线性 LV、已经正确连接时不操作、取消操作、确认期间设备消失、仅刷新指定 LV。
- 所有 Python 文件通过语法编译检查，三个 shell 脚本通过语法检查。
- systemd 的 slice、prepare、两个 VT 共用模板、日志服务通过 `systemd-analyze verify`。检查在独立用户/挂载命名空间中提供临时安装路径，没有安装或启动宿主服务。
- 普通用户隔离烟测通过，输出在 `work/smoke-tests.log`：把整个包解压到单独 tmpfs，chroot 后执行静态 shell、Python 标准库、LVM 配置验证、e2fsck/blkid 版本命令、dmsetup 动态库解析、救援帮助。
- 在隔离伪终端中确认 BusyBox login 拒绝错误密码；测试密码只进入一次性测试环境，没有写入发布的工具包。
- smoke 测试不暴露真实硬盘、NVMe 或 `/dev/mapper` 设备节点，不修改真实映射，不进行拔盘模拟。
- 压缩包校验通过。

## 留给管理员安装阶段的强制检查

- 正确密码的完整 root 登录。普通用户命名空间的 `setgroups` 限制使其不能替代真实 root 登录测试。
- `tmpfs,noswap` 的实际挂载。本机内核明确禁止非特权用户命名空间使用 `noswap`，因此普通用户烟测使用普通 tmpfs；**正式安装没有降级逻辑，noswap 不成功就失败**。
- 实际 systemd cgroup 的 `MemorySwapMax=0`、768 MiB 内存限额、服务进程根目录确实位于 RAM。
- 真实 tty9/tty10 的人工登录与切换。

安装器会先在隔离挂载空间中完成前两个测试，再安装并启动服务。服务检查失败会尝试撤销本 demo 的安装，不把失败状态当作已安装成功。

## 尚未验证的故障能力

- 没有主动断开正在运行的根盘；真实掉线时终端可用性和 LVM 刷新成功率需要现场验证。
- 文件系统已经报错、只读、日志中止或应用已损坏的情况下，恢复映射不保证系统能继续使用。
- 内核 panic、全局死锁、终端驱动停止响应时无保证。
- 制作阶段尚未管理员安装，因此当时没有本机实际常驻内存测量；54.97 MiB 是文件负载，不是总进程 RSS。

## 安装状态

制作阶段结束时未安装、未启用服务。`sudo -n true` 返回需要密码。

最后安装命令：

```bash
sudo python3 /workspace/Project/External-Memo-Rescue-Handler/ram-rescue-demo/install.py
```

安装后状态命令：

```bash
sudo python3 /usr/local/lib/ram-rescue-demo/check.py
```

## 2026-09-23 版本库建立时复查

只读检查确认四个救援服务均 active；tty9、tty10 和日志服务均 enabled。运行目录为带 noswap 的 tmpfs，slice 的 MemoryMax=805306368、MemorySwapMax=0。瞬时 MemoryCurrent=94150656 字节（约 89.8 MiB，随运行变化，并非进程 RSS）。这更新了上面的历史安装状态，但未重新执行特权安装检查、人工 VT 登录或真实掉盘验证。
