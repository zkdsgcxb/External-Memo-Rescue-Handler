# 制作验证记录

日期：2026-09-23（Asia/Shanghai）。构建环境：本机 Ubuntu 24.04，6.8.0-139-generic，x86_64。

## 2026-10-03 救援会话改进候选

- 不改变正在运行的主机服务或既有密码。首次安装新增至少 15 字符、UTF-8 不超过 1024 字节的口令规则，允许空格和中文，不要求字符类别；拒绝换行和不可打印字符，避免 NUL 或过长输入被截断；认证继续由 BusyBox 实现。未认证的 VT 保持阻塞等待，不按固定时间重启登录进程，已认证 shell 才使用提示符时限。
- 本机 BusyBox 1.36.1 ash 经两次隔离 PTY 实测，设置 `TMOUT=2` 后等待 6–7 秒仍不退出。因此新增发行版 Bash，使用其内置提示符超时；不添加后台监视进程。
- 27 项普通用户回归通过：15 项既有恢复逻辑、5 项新设密码策略、7 项新会话/打包测试，输出保存于 `work/p0-final-unit-tests.log`。新会话测试在隔离 user namespace + chroot 内运行实际 Bash 和脚本，未暴露宿主设备；覆盖提示符自动退出、前台命令完成、`0` 禁用、禁止 profile 和环境钩子、无效/注入/缺失配置拒绝、打包路径逃逸拒绝。
- 最终候选与已安装基础工具包逐文件比较：新增 Bash 为 1,446,024 字节；`libtinfo`、`libc` 和动态加载器已存在，会话文件净增 1,447,770 字节（约 1.38 MiB）。这是展开文件字节，不是进程 RSS；构建值写入 `build.json` 的 `rescue_session`，依赖和脚本均记录散列。
- 保护 initrd 构建会从当前源码覆盖旧包的会话脚本、配置和 Bash 依赖，相关源码计入构建 `source_sha256`；不会因为基础工具包仍是旧版就漏装会话策略。
- 完整 Ubuntu QEMU 最终验收 `lab/work/cpp-int-1003-041222-160301/report.json` 为 36/36 通过，源码与种子盘未变化，ext4/FAT 检查均返回 0。在恢复场景前、恢复场景后分别完成真实 BusyBox 的六项认证检查：锁定账户、错误密码、缺失 shadow 均拒绝；正确口令可在新 shell 执行标记；显式退出和空闲退出后再次要求密码。测试凭据在 guest 内随机生成，临时 root 均已清理，没有读取真实账户库。此证据覆盖隔离 PTY 认证，不替代本机 F9/F10 手工验收或根盘仍失联期间的认证试验。
- 认证测试第一次遇到的三个问题均属于 fixture：RAM 的普通 `/dev` bind 未带 devpts 子挂载、guest `/run` 的 `noexec` 阻止测试副本执行、锁定密码散列触发 BusyBox `bad salt` 拒绝而非 `Login incorrect`。最终 fixture 在私有挂载命名空间内创建独立 devpts 和 32 MiB 可执行 tmpfs；仅锁定/不可用散列测试接受已观测拒绝诊断。生产 `/dev` 与 `/run` 挂载策略未改变。
- 根盘在凭据复制完成前失联仍可能阻塞或拒绝首次终端准备；默认镜像账户锁定，无免密降级。凭据复制到 RAM 之后，F9/F10 的认证与上述会话计时不再需要根盘读操作。

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
