# Ubuntu 主机内核升级预检与回退设计

**后续状态：已按本方案安装并实际启动官方 HWE 7.0，初步启动检查通过，保留 6.8 回退。** 实际内置 rEFInd 引导链和回退步骤见 [实机安装记录](HOST-INSTALL.md)，日志及验收边界见 [首次启动记录](HOST-POSTBOOT.md)。下文保留安装前的预检时点数据。

日期：2026-09-25。本文记录只读预检与安装模拟，不表示已经安装或启动新内核。原始脱敏包计划见 [host-upgrade-preflight.json](host-upgrade-preflight.json)。项目故障实验与主机桌面验收是两种证据，不能互相替代。

## 1. 建议与适用范围

建议保留 Ubuntu 24.04 用户空间，使用该发行版正式提供的 `linux-generic-hwe-24.04` 更新内核；保留当前已运行的 6.8 内核及其模块作为回退。先做内核单变量验证，再决定是否单独更新 NVIDIA 驱动、桌面或 multipath-tools。

这不需要混装其他发行版的 glibc、systemd、udev 或内核模块。截至本次查询，Ubuntu 官方已将 **7.0 列为 24.04.5 的正式 HWE 内核，2026 年 8 月发布，标准支持至 2029 年 4 月**；本机仓库候选为 `7.0.0-34.34~24.04.1`。GA 6.8 也仍在支持期，留在 GA 并非放弃安全更新。HWE 的优势是更新的内核功能与硬件支持，并不代表所有工作负载都会变快。不要把 `-edge` 或上游 mainline 测试包等同于正式 HWE。[Ubuntu HWE 文档](https://ubuntu.com/kernel/docs/reference/hwe-kernels/)

## 2. 主机实际状态

| 检查项 | 2026-09-25 实测 | 对升级的影响 |
| --- | --- | --- |
| 系统 / 正在运行内核 | Ubuntu 24.04.5；`6.8.0-139-generic` | 当前安装的是 GA 元包 `linux-generic`，并未安装 HWE 元包 |
| 已安装的实际内核 ABI | 仅 `6.8.0-139-generic` | `vmlinuz.old`、`initrd.img.old` 都仍指向同一 ABI，不是第二份可用回退内核 |
| CPU | Intel Core i9-13900H | 是 Raptor Lake，不能把针对更新 Panther Lake 的优化直接算作本机收益 |
| 核显 / 独显驱动 | Intel `i915`；RTX 4060 Laptop 使用 `nouveau` | 当前没有专有 NVIDIA 驱动或 NVIDIA DKMS 升级兼容链需要保留 |
| DKMS / NVIDIA 命令 | `dkms`、`nvidia-smi` 不存在；未查到已安装的 DKMS/NVIDIA 驱动包 | 内核首轮升级无需附带 NVIDIA 安装 |
| Secure Boot | disabled | 本次不存在新 DKMS 模块的 MOK 注册前置要求；仍应使用 Ubuntu 正式内核包 |
| `/boot` | 与根目录同在 USB 盘 LVM ext4 上 | 根文件系统剩余约 82 GiB；不存在独立 `/boot` 小分区容量限制，但升级时仍依赖 USB 盘持续在线 |
| `/boot/efi` | USB 盘上的 ESP，约 2 GiB 可用 | 它不同于固件启动入口指向的 rEFInd ESP，完整链仍待确认，见第 5 节 |
| 包状态 | `dpkg --audit` 无输出；没有 hold 包 | 未见包数据库未完成状态；不等于所有程序已验证 |
| 根盘保护 | 本机未部署实验室自动恢复映射 | 升级内核不会自动为原根卷安装 Guard 或 multipath 保护 |

当前 initramfs 中已找到 `xhci-pci`、`uas`、`usb-storage`、LVM 程序与启动脚本。当前内核的 `CONFIG_SCSI`、`CONFIG_BLK_DEV_SD`、`CONFIG_BLK_DEV_DM`、`CONFIG_EXT4_FS` 为内建，`CONFIG_DM_MULTIPATH=m`；因此 initramfs 没有 `dm-mod.ko` 本身不是遗漏，不能只按模块文件名判断支持。`MODULES=most` 是当前生成策略。升级后必须针对新 ABI 重新检查，旧 initramfs 不能直接搭配新内核。

## 3. 精确包计划与空间

内核计划使用在项目私有目录刷新的 Ubuntu 签名索引；未改主机的 `/var/lib/apt/lists`。元数据刷新于 2026-09-25，候选同时来自 `noble-updates` 与 `noble-security`。模拟是一个时点结果，正式执行前仍要重新解析依赖。

| 方案 | 包变化 | 下载量 | 包元数据估算净增空间 |
| --- | --- | --- | --- |
| 推荐：增加正式 HWE 元包 | 10 个新包，0 升级，0 删除 | 241,991,674 B，约 230.8 MiB | 481,859,584 B，约 459.5 MiB |
| 备选：继续 GA，只更新其 ABI | 7 个新包，5 个升级，0 删除 | 191,875,846 B，约 183.0 MiB | 303,181,824 B，约 289.1 MiB |

这里的空间是包 `Installed-Size` 的净差，不包括生成的 initramfs、下载缓存、解包临时空间和文件系统分配开销。当前 6.8 的 initramfs 约 79.3 MiB，可供数量级参考，新版大小不能直接据此保证。

HWE 方案的新包清单：

| 包 | 版本 |
| --- | --- |
| `linux-generic-hwe-24.04` | `7.0.0-34.34~24.04.1` |
| `linux-image-generic-hwe-24.04` | 同上 |
| `linux-headers-generic-hwe-24.04` | 同上 |
| `linux-image-7.0.0-34-generic` | 同上 |
| `linux-modules-7.0.0-34-generic` | 同上 |
| `linux-hwe-7.0-headers-7.0.0-34` | 同上 |
| `linux-headers-7.0.0-34-generic` | 同上 |
| `linux-hwe-7.0-tools-7.0.0-34` | 同上 |
| `linux-tools-7.0.0-34-generic` | 同上 |
| `libllvm19` | `1:19.1.1-1ubuntu1~24.04.2` |

此版本的正式依赖计划没有独立的 `linux-modules-extra-7.0.0-34-generic` 包。应以目标内核包实际内容和依赖为准，不照搬 6.8 的包拆分规则，也不把其他 ABI 的模块目录复制过来。

正式安装候选命令是 Ubuntu 官方给出的 `apt-get install --install-recommends linux-generic-hwe-24.04`，由管理员执行。本文未执行此命令。并存安装后，6.8 与 7.0 元包可以分别跟随自己的更新序列；不能在首次验证前执行自动清理把回退内核删掉。

## 4. 更新能带来什么，以及不能据此推断什么

官方发行说明确实列出 6.8 到 7.0 期间的调度能力、可调低延迟机制和新硬件支持，例如 `sched_ext`。但“内核具备某机制”不等于默认启用某个新调度策略；这里也不建议为了尝鲜同时换 eBPF 调度器、启用实时内核或调整大量内核参数。[Ubuntu 26.04 面向 LTS 用户的内核变更说明](https://documentation.ubuntu.com/release-notes/26.04/summary-for-lts-users/)

对本机，合理的验收目标是桌面响应、编译吞吐、待机功耗、显示/外接屏、休眠唤醒和 USB 行为。若声称“CPU 更快”“内存更省”或“USB 不掉盘”，必须有同一机器、同一负载、相近温度/电源模式下的测量。QEMU 能验证内核与实验存储栈能否协作，不能模拟本机 NVIDIA/Intel 显示、电源、真实 USB 桥和固件全部行为。

对本项目，新内核可能带来 USB/UAS/SCSI/DM 缺陷修复，但**不会因为版本号提高，就自动补全设备身份准入、管理器死亡后的事务接管、文件系统错误历史与断电持久性证明**。这些仍以版本对照故障实验为准。

升级内核也不自动把 GNOME 46 或 Plasma 5.27 升级为新一代桌面，不自动更换 Mesa、NVIDIA 用户态库或恢复守护程序。桌面观感问题与根盘保活问题应分别验收。

## 5. rEFInd 与回退：当前最容易误判的地方

只读 EFI 启动项显示，当前 `BootCurrent` 对应 **`rEFInd (internal)`，固件入口指向内部 NVMe 的 ESP**，该分区目前未挂载。当前 `/boot/efi` 却是 USB ESP。`BootCurrent` 不足以排除后续再次链式加载其他副本，因此在 `/boot/efi/EFI/refind/refind.conf` 读到的配置不能直接视为最终活动配置；完整引导链仍待确认。本文未挂载内部 ESP，也未改 NVRAM。

USB ESP 上存在 Ubuntu `grubx64.efi`、`shimx64.efi` 和一个将控制权转交给 LVM 上 `/boot/grub/grub.cfg` 的配置。该最终 `grub.cfg` 普通用户不可读。本机 `/boot` 在 LVM，rEFInd 发行版自带的 ext4 驱动并不等于提供 LVM 读取能力；因此回退设计应验证 **实际活动 rEFInd → Ubuntu GRUB → 指定内核** 的路径，不能假定 rEFInd 会直接列出 LVM 内所有内核。[rEFInd 作者的 Linux 引导说明](https://www.rodsbooks.com/refind/linux.html)

当前 `/etc/default/grub` 是 `GRUB_TIMEOUT_STYLE=hidden` 和 `GRUB_TIMEOUT=0`。在这种配置下，只写一句“失败时选旧内核”不够：GRUB 可能立即跳过菜单。进入第一次新内核启动前，需要具体完成以下准备：

1. 保留 `linux-image-6.8.0-139-generic`、`linux-modules-6.8.0-139-generic`、`linux-modules-extra-6.8.0-139-generic`，必要时将它们标记为手动安装，确认对应 initramfs 仍在。不要把 `.old` 符号链接当作独立备份。
2. 只读确认实际活动 rEFInd 的 Ubuntu 入口，备份相关配置。在现有链上提供可访问的 GRUB 菜单，例如 `GRUB_TIMEOUT_STYLE=menu`、`GRUB_TIMEOUT=10`，然后正常重新生成配置；不得直接把手工修改写入生成的 `grub.cfg`。这里列的是后续实施要求，尚未修改。
3. 验证生成菜单确实同时包含 `7.0.0-34-generic` 与 `6.8.0-139-generic`，新内核和新 initramfs 版本匹配，旧入口没有被替换。
4. 首次新内核启动失败或桌面异常时，经实际 rEFInd 的 Ubuntu/GRUB 入口选择 `Advanced options for Ubuntu` 中的 `6.8.0-139-generic`。成功进入后以 `uname -r` 确认。菜单标题以本机生成结果为准，当前未读取或启动验证。
5. 如果 rEFInd→GRUB 链本身不可用，使用独立 Ubuntu Live 启动介质恢复；保留在同一故障 USB 盘里的旧内核无法应对整盘不可读。

GRUB 对 `timeout=0` 的行为及菜单选项有明确说明；目前不能把按 Esc 抢时机当作已验证的可靠回退入口。[GNU GRUB 配置文档](https://www.gnu.org/software/grub/manual/grub/html_node/Simple-configuration.html)

此次内核升级无需重新运行 `grub-install --removable` 或替换 `EFI/BOOT/BOOTX64.EFI`。可移动介质默认路径能改善引导器发现，但不能解决 Linux 运行期的块设备重接。

## 6. NVIDIA 是独立的可选阶段

本机 `ubuntu-drivers devices` 当前推荐 `nvidia-driver-595-open`，但实际仍运行 `nouveau`。如果后续决定改变 GPU 驱动，可优先考虑 Ubuntu 预编译、与 HWE ABI 配套的模块，避免无必要地引入 DKMS 构建链。官方也将预编译模块与 DKMS 列为两种方式。[Ubuntu NVIDIA 安装文档](https://documentation.ubuntu.com/server/how-to/graphics/install-nvidia-drivers/)

基于主机原有 2026-09-23 仓库缓存，`linux-modules-nvidia-595-open-generic-hwe-24.04` 可与 `nvidia-driver-595-open=595.91.07-0ubuntu0.24.04.1` 一起解析；连同 HWE 共 32 个新包、0 删除，约 539.9 MiB 下载、1,413.8 MiB 包空间净增。完整记录在 JSON 的 `optional_gpu_case`。项目私有内核索引只启用了 main/restricted/universe，没有 multiverse，因而没有重新验证这个位于 multiverse 的驱动元包；正式 GPU 变更前必须刷新完整对应仓库并重新模拟。

这项变更不属于内核首轮建议。NVIDIA 包可能引入 nouveau 黑名单和用户态显示库改变，**仅切回旧内核不一定能撤销 GPU 驱动变更**。若日后实施，必须另行准备旧 ABI 对应的 NVIDIA 模块或完整驱动回退方案，并验证 GNOME/Plasma、Wayland/X11、外接屏和休眠。

## 7. 进入主机启动试验的验收清单

- 项目 VM 中目标内核能启动，并通过与旧内核相同的基础恢复实验；失败结果也必须记录。
- 主机安装事务完成，`dpkg --audit` 无异常；新内核 image/modules/initramfs 匹配，USB/UAS/LVM/根文件系统的启动所需组件存在。
- 实际 rEFInd→GRUB 回退入口可访问，旧内核完整保留；仅生成配置不算已经验证启动菜单。
- 首次重启后确认 `uname -r`，检查 kernel/udev 日志是否有新增模块、固件、GPU 和存储错误。
- 验证根卷、`/workspace`、网络、音频、外接屏、中文输入、开发工具和一轮正常休眠/唤醒。主机上不使用拔根盘作为升级验收动作。
- 确认基线稳定后再讨论性能测量与下一项组件变更。保留旧内核到实机验收完成。

截至本文生成，上述主机安装、菜单调整、重启及新内核实机验证均未进行。
