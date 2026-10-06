# v0.0.1-beta 安装与使用

本指南针对 Ubuntu 24.04 x86_64 的 ext4 USB 数据分区。先阅读 [支持范围](SUPPORT.md)。不要在唯一一份重要数据上做断联实验。

## 安装

从同一 GitHub Release 下载程序 `.deb`、支持材料、`SHA256SUMS` 和验收报告。SHA-256 校验的是内容一致性；来源必须是你信任的发布页面。

```sh
sha256sum -c SHA256SUMS
sudo apt install ./ram-rescue-handler_0.0.1~beta+*.deb
rescue-guard-admin --version
sudo rescue-guard-admin device --help
```

安装不创建设备映射、不准备 RAM、不启用服务、不修改 fstab 或启动镜像。支持材料安装步骤及精确组合见发布附带的支持清单；缺少匹配的资格或官方内核参考文件时，计划和启用都会拒绝，不提供 `--force`。

普通 Ubuntu 可能尚未载入 multipath 模块。可以显式执行 `sudo modprobe dm_multipath` 后预检；不能同时运行发行版 `multipathd` 管理这些映射。本工具不自动修改全局内核排队超时或禁用其他服务。

## 选择与登记

先正常卸载目标分区并关闭使用它的应用。目标不能承载系统挂载、swap、上层 LVM/加密或其他 DM 使用者。原有 fstab 如通过裸分区、UUID 或标签选择该分区，需先由用户检查并调整；本工具拒绝这种双重访问方式，不自动重写 fstab。

```sh
sudo rescue-guard-admin manager discover
sudo rescue-guard-admin device plan --device /dev/disk/by-id/你的USB分区 --name rr-data-work
sudo rescue-guard-admin device enroll --device /dev/disk/by-id/你的USB分区 --name rr-data-work
```

`plan` 只读，但会读取所选介质。`enroll` 再次生成计划，交互时要求输入完整摘要；脚本需传入 `--expect-plan <plan_sha256>`。设备重插、内核/依赖变化会使原计划失效。

登记后状态为停用，尚未建立新映射，重启也不会启用。计划与状态 JSON 含本地设备标识，不应直接贴到公开 Issue；公开诊断使用脱敏导出。

## 启用与使用

```sh
sudo rescue-guard-admin device status --name rr-data-work
sudo rescue-guard-admin device enable --name rr-data-work
```

交互输入配置摘要后，工具会安装精确设备的 automount 排除、建立稳定映射、准备 RAM 工具并启动 C++ 控制器。首次开启排队发生在原生所有权锁及事务记录建立之后。自动化启用需使用 `status` 输出的 `config_sha256` 作为 `--expect-plan`。

成功必须显示 `state: active`；返回非零或 `blocked` 时先检查状态，不能视为已保护。`enabled_at_boot` 仅表示启用意愿，实际工作状态以服务及 `native.state` 为准。

随后由用户正常挂载稳定设备：

```sh
sudo mkdir -p /mnt/protected-work
sudo mount /dev/mapper/rr-data-work /mnt/protected-work
```

启用意愿跨重启保留；启动时重新核验支持材料和原盘身份后才创建映射。没有配置自动挂载，应用应在保护就绪、稳定设备挂载后再开始读写。启动时缺盘会拒绝启动该保护实例；插回后执行显式 `enable` 重试。不要绕过映射挂载其底层裸分区。

## 停止、停用与移除

先正常卸载稳定设备。工具不会替你强制卸载或结束进程。

```sh
sudo umount /mnt/protected-work
sudo rescue-guard-admin device stop --name rr-data-work
```

`stop` 仅安全停止本次控制器，保留映射、配置、锁和 RAM，未来开机意愿不变。原生层确认无消费者后关闭无路径排队；再次 `enable` 使用匹配的安全停止收据重新启动。

```sh
sudo rescue-guard-admin device disable --name rr-data-work
sudo rescue-guard-admin device remove --name rr-data-work
```

`disable` 先撤销未来开机启用，再请求本次安全停止。如果设备忙，命令失败且当前保护继续运行，但未来启用意愿已撤销；卸载消费者后重新执行。`remove` 需要配置摘要确认，只移除已停用、空闲且身份匹配的映射与配置，不改文件系统内容。

为避免复用过时的所有权证据，本次启动的锁、日志及 automount 排除保留到重启；同名设备在本次启动中不能重新登记。

## 卸载与升级

所有设备 `disable`、`remove` 后，正常重启使 RAM 运行环境退出，再执行：

```sh
sudo rescue-guard-admin device uninstall
sudo apt remove ram-rescue-handler
```

卸载检查拒绝仍有配置、保护映射、RAM 运行环境、未完成事务或旧根盘集成的情况。不会自动停止业务、移除磁盘或清除证据。操作收据保留在 `/var/lib/ram-rescue-handler/devices/`。

升级安装新包不重启控制器、不热替换 RAM；磁盘版本和运行版本可能不同。新包需要配套的支持材料，按其指南安排停用与正常重启。第一版尚无更早公开版本可做跨版本升级验收；安装器的升级路径必须保持不自动激活。

## 排障

```sh
sudo rescue-guard-admin device status
sudo rescue-guard-admin device support
sudo rescue-guard-admin manager doctor
sudo rescue-guard-admin manager export --output /你选择的本地目录/report.json
journalctl -u ram-rescue-devices.service -b
journalctl -u ram-rescue-data-rr-data-work.service -b
```

资格不匹配通常由内核更新、依赖更新、包或入口变化引起。不要编辑资格 JSON 绕过检查；保留原信息并请求新的组合验收。

`enable_incomplete`、原生 `interrupted` 或 owner-fence 相关错误表示应保留现状并分析事务。永久底层 I/O 阻塞可能无法用用户空间超时取消。不要删除 `/run/ram-rescue-data/` 下的锁或事务文件来强行重试。
