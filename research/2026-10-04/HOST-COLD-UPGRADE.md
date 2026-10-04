# 本机冷态部署记录

2026-10-05，本地管理员通过系统密码窗口认证后完成。最终软件与 QEMU 验收通过的产物已安装，**尚未重启进入新版本**。当前 Guard 和日志进程保持原实例，安装完成不代表新策略已经运行。

## 落盘与保留结果

| 对象 | 实际结果 |
| --- | --- |
| 管理包 | `ram-rescue-handler 0.3.0+2f5bf9a687263df093e11293`，dpkg 状态为 install ok installed；实际包通过最终普通 Ubuntu 42 项验收 |
| 固定入口 | `/usr/bin/rescue-guard-admin`，root 所有、0755；每次先验证已安装代码清单 |
| 保护镜像 | `/boot/initrd.img-7.0.0-34-generic-ram-rescue`，root 所有、0600，123,040,210 字节；落盘 SHA 与候选一致 |
| 镜像预检 | 实际内核、普通 initrd、LVM 配置、既有安装收据、菜单、当前设备登记及候选来源检查通过 |
| 冷态管理升级 | 保留登记，切换下次启动的管理版本；没有准备新 RAM 环境或启动控制器 |
| 日志 unit | 从已核验的 root 包资源冷更新，原文件 SHA 核对、私有备份、新旧 SHA 及 systemd 语法检查通过；执行 daemon-reload |
| 仍在运行的实例 | Guard PID 689，自 2026-10-04 20:29:29 CST 运行；日志 PID 1495，自 20:29:32 运行；部署前后均未变 |
| 引导与普通回退 | 镜像升级返回 `grub_changed=false`，保留普通 Ubuntu 首项和既有卸载资料；没有改 rEFInd 或重启机器 |

实际 `.deb` SHA256 为 `dc1829017a88c3812deaeeefef368231f4678d206cfda14d94888b1d79aa02ba`。输入 bundle SHA256 为 `578c7686e2a34d10abc21f080ada3e25ab4ea88d8d0ce836a3a73652df19082b`，封存到对应的 `/var/lib/ram-rescue-candidates/` 私有目录后才执行安装。

## 磁盘版本与运行版本

| 项目 | SHA256 |
| --- | --- |
| 新安装的保护 initrd | `619c03696311a71a9e5c950ccd06dd5a37d43c228f2e0379311fad200e2e547d` |
| 下次启动候选 C++ ELF | `479c8ba7e2445ae3fbb99d58e1b79d836170c6fe463953ca636cde9857ee8718` |
| 本次启动实际仍在运行的 ELF | `72d9d550079b4f73e1954e13f99fa614826758c623eec1d3026aa22bc891b21f` |

通过新固定入口执行的 `doctor` 返回当前映射 `ready`、owner 与本次启动匹配，两个持久安装收据均为 `installed`，根盘与管理候选均指向新 ELF。实际进程 ELF 仍是旧版本；当前根服务的 `NoNewPrivileges`、能力集合和只读限制也仍为旧策略。这是冷态更新的预期状态，不能把磁盘上的新配置写成已生效。

旧 RAM 环境的一项输入返回 `unavailable_or_untrusted`，因此本次未取得冻结库清单的匹配结果；不猜测具体原因，也不放宽可信文件检查。实际运行 ELF 的散列和 owner 核对成功。新包已在 QEMU 验证完整清单，实机下一次启动仍须重新核对该问题及实际库版本。

## 回退证据与下一次验收

旧保护镜像与收据位于 `/var/lib/ram-rescue-guard/upgrades/20261004T161634Z-3fd911e67f87/`；旧管理配置位于 `/var/lib/ram-rescue-manager/upgrade-qzwxwu7s/`；日志 unit 备份位于 `/var/lib/ram-rescue-package-review/20261005-roadmap-v9/ram-rescue-log.service.before`。这些都是 root 私有证据，不随公共仓库上传。普通 Ubuntu 回退入口仍保留；没有在本轮实机实际执行回退启动。

下一次选择现有保护入口启动后，核对实际 ELF、root/shared 稳定映射及读写挂载、Guard 和日志进程的真实权限/挂载视图、完整冻结库清单，并进行 F9/F10 密码登录、桌面应用、睡眠唤醒与正常关机观察。旧块层 WARNING 仍待定位，真实 USB 拔插尚未在本轮执行。

公开部署摘要为 [2026-10-05-host-cold-upgrade.json](../../lab/results/2026-10-05-host-cold-upgrade.json)。原始预检、收据和诊断保存在本地 `lab/work/roadmap-host-deploy-20261005/`，其中原始设备登记和 GRUB 内容未发布；摘要仅包含限定字段、文件散列和状态边界。
