# 保护映射的挂载与子挂载

恢复块设备后，**仍存在的原挂载直接继续使用同一个 DM 设备**。不需要先卸载再挂载；原进程、文件描述符、bind mount 和嵌套文件系统因此有机会保持连续。已经卸载的文件系统重新挂载，只能恢复访问路径，不能恢复旧挂载对象或旧文件描述符。

`guard/mounts.py` 为明确登记的消费者生成原生 systemd `.mount` / `.automount` 单元。它是一次运行的配置生成器，不是另一个后台监视器。设备核验和路径恢复仍只有原来的 Guard 控制器负责；挂载层不重复检查 USB 序列号或扫描文件系统。

## 明确职责

- DM：稳定块设备以及符合条件的无路径 I/O 排队。
- Guard：登记身份核验、恢复路径和超时终态；根盘已有启动 owner 保持不变。
- systemd：按稳定设备与父挂载的依赖关系创建和停止挂载；automount 可在首次访问时补齐初始挂载。
- 文件系统和应用：自身的错误处理。此集成不会撤销已发生的 I/O 错误，不会把只读文件系统擅自改回读写。

`What=` 使用登记的 **DM UUID** 路径，`BindsTo=` 也只指向这个稳定 DM 设备。不能依赖底层 `/dev/sdX` 的 `.device`：短暂断开时正是这个旧设备消失，而上层保护映射仍然存在。

挂载 `Requires=` / `After=` 对应控制器。数据盘首次身份核验失败或控制器启动失败时，不应建立新挂载。正常恢复窗口内控制器一直运行，所以临时 USB 消失不会触发卸载。显式停止控制器会通过依赖关系请求正常卸载；忙碌的挂载仍按内核规则拒绝，不使用强制或 lazy 卸载，不终止应用。控制器因终态自行退出，不等于驱逐已经存在的挂载；新的挂载请求则必须通过控制器启动依赖，不能清空原来的终态事务。

已有 `/`、`/workspace` 等启动挂载仍由当前启动配置负责，生成器不接管 `/`、EFI 或系统目录。相同根盘上的其他已登记线性 LV 可以作为额外挂载的源，继续依赖原根盘 owner，不增加第二个 Guard。它们额外使用 `AssertKernelCommandLine=ram_rescue_guard=1`：普通启动时根盘服务可能因 Condition 被跳过，而被跳过本身不会让 Requires 失败，因此不能只依赖服务的 Condition。

## 生成可审阅配置

示例登记映射 `rr-data-example` 已由统一管理器维护。挂载计划文件：

```json
{
  "schema": 1,
  "mounts": [
    {"map": "rr-data-example", "where": "/mnt/protected", "automount": true},
    {"bind": "/mnt/protected/work", "where": "/mnt/work"}
  ]
}
```

```bash
python3 guard/mounts.py --plan mount-plan.json \
  --record /etc/ram-rescue-manager/devices/rr-data-example.json \
  --output lab/work/mount-units-review
```

输出目录必须尚不存在。生成器不安装配置、不改变活动挂载；管理员审阅之后使用现有 systemd 流程安装并启用所需单元。登记文件默认私有，读取它可能需要通过本地认证以管理员身份执行。文件系统类型由登记记录决定；不接受任意设备名、`remount` 或自动修复参数。嵌套 automount 被拒绝，防止其生命周期彼此牵制。路径格式限定为普通 ASCII 绝对路径；bind 源必须位于计划声明的文件系统之下，但这种词法检查不解析运行时符号链接。**计划、挂载点及 bind 源目录必须可信，源路径不能通过符号链接逃出所登记的文件系统。** 配置生成器不是对恶意可移动介质内容的隔离机制。

普通 mount 单元及 bind 单元可通过 `systemctl enable --now` 启用。automount 只启用 `.automount` 单元，首次访问创建实际挂载。它的闲置超时固定为零，不主动丢弃已有挂载。启用一个依赖它的子挂载会立即访问/拉起父挂载，因此这种情况下父挂载不再等到应用第一次访问才出现。

如果只是挂载路径尚未建立，这套原生依赖可以补齐它。若 ext4 已经中止日志、挂载已变为只读、设备替换或 Guard 进入终态，本集成不会不断重新挂载、偷偷执行 fsck 或强制改为读写。应保留证据，正常停止使用后再安排离线检查。

## 实验范围

`lab/mount_guard_probe.py` 在完整 Ubuntu QEMU 中使用保护启动根盘、额外 ext4 和 FAT32 稳定映射，覆盖首次 automount、嵌套独立文件系统、两种 bind 子路径、持续持有的文件描述符、错误身份拒绝及各盘重连。只读策略用受控 `remount,ro` 检查，不冒称已经复现文件系统日志损坏；关闭工作负载之后额外触发无路径超时，检查终态不能由重新挂载请求清除。测试结束正常卸载，再对离线镜像执行只读检查；结果以实际 `report.json` 为准。

原生行为依据：[systemd.mount](https://www.freedesktop.org/software/systemd/man/latest/systemd.mount.html)、[systemd.automount](https://www.freedesktop.org/software/systemd/man/latest/systemd.automount.html)、[systemd.unit](https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html)。QEMU 验收同时调用客户机实际版本的 `systemd-analyze verify`，避免只根据最新手册推断本机支持情况。
