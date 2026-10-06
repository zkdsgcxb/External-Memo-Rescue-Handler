# 实际组合采集与真实 CLI 候选验收

2026-10-05。本轮闭合 `current_evidence()` 的实际路径，并用封存的真实 `.deb` 在隔离 Ubuntu VM 中完成固定管理入口的候选流程。**验收仅覆盖一个工程组合的已有单路径数据 DM / ext4 候选准备与撤销**；不是激活、根保护、恢复、完整发布或宿主接受证明。没有部署宿主、提交、推送或公开发布。

汇总与最终源码摘要见 [结果 JSON](../../lab/results/2026-10-05-real-candidate.json)，未经改写的 [VM 原报告](../../lab/results/2026-10-05-real-candidate/vm-report.json) 包含实际 CLI、返回码、原始计划、资格和前后状态。最终运行目录为 `lab/work/cq-1005/vm3`。

## 六项 binding 的实际证据

威胁边界沿用可信 root 与内核。不存在对恶意 root 的远程证明，也不要求运行内存逐字节等于磁盘；缺失、矛盾、读取失败仍返回 unknown 并阻断。资格文件不能补齐以下关联。

| binding | 实现与本次实际结果 | 证明边界 |
| --- | --- | --- |
| executing_management | 固定 `/usr/bin/rescue-guard-admin` 先复用已有逐文件可信代码清单检查；实际入口必须等于清单中 bootstrap 按安装根渲染后的字节，再把入口/清单摘要传入同进程采集模块；VM pass | 没有环境变量、外部“已验证”收据或 CLI 注入；信任受保护安装树及 Python 发行环境，不是内存证明 |
| running_kernel | `/sys/kernel/notes` 的 GNU build-id 与认证 image 解出的 vmlinux 对应；另查当前 `/boot/vmlinuz-<release>` SHA；VM pass，build-id `b39508a189f60b8b1c173fa3d73c8a8a9c8b25ed` | release 只定位材料；同名候选文件不足以通过。发现 livepatch 即拒绝，本轮不覆盖内存热修改 |
| loaded_modules | 必需 DM/USB/SCSI、文件系统模块及有限依赖闭包的磁盘摘要与官方提取记录一致；已加载项查 live、build-id、可用 srcversion；builtin 查 image 与 builtin 元数据；VM pass | 实际 20 项中 14 项 builtin、4 项 loaded identity matches、2 项明确 not_loaded；后两项不是“已加载通过”。本次资格仅 ext4，不授予 exfat/vfat 场景 |
| official_kernel_origin | Ubuntu archive keyring 验签 InRelease，签名 SHA256 核验 Packages，选出 amd64 image/modules 的版本、Source、包 SHA/大小；核对 HWE Source 家族；VM pass | `source.json` 是可信本地提取记录，发行包与提取过程另有报告。它不是 Canonical 对该 JSON 的签名；root 所有也不等于发布者签名 |
| runtime_artifacts | 复用现有 runtime manifest 校验，固定可信 fd 校验压缩归档、只读流式核对真实 ELF、maintain、共享库与内嵌 runtime 清单；VM pass | 指将供后续使用的惰性包内 runtime；未建立 RAM 环境，未启动 Guard，不称运行实例已验 |
| host_dependencies | 当前进程 exe、实际文件映射的设备号/inode、实际 libdevmapper，以及固定 blkid/findmnt/systemctl/gpgv 的有限库闭包；复用已有包归属/版本检查；VM pass，33 文件、27 个包条目 | 不扫描全机包数据库或库目录。Python 标准库采用可信发行包版本边界，非每个 Python 内存对象或全部 stdlib 文件的字节证明 |

实现集中在 `guard/current_support.py`。`admin_entry.py` 只交付既有核验结果；`support.py` 保持严格静态投影和固定资格读取，不新增第二套发现、准入或恢复状态机。`native_payload._linked_libraries` 改用既有有界命令包装；`ram_environment.package_manifest` 可接收已核验 fd，避免校验和读取两次打开不同归档。没有改 C++ 恢复核心。

内核/模块参考材料固定放在 `/usr/share/ram-rescue-handler/kernels/<release>/`，与 `/usr/share/ram-rescue-handler/qualifications/<subject>.json` 独立。前者的确切提取脚本、认证包和摘要由 `lab/kernel_reference.py` 留证。生产读取会重新检查签名材料、当前内核/模块与参考的关系；不会读取资格来声称内核身份通过。

采集是冷管理路径，未加入 Guard 常态监视。它有实际文件 I/O：包括约 7.4 MB 的 Packages、内核/有限模块、压缩 runtime 流和有限管理依赖；一份 plan 前后各采一次。现有完整介质准入也仍执行。文件/输出/数量有界，gpgv 10 秒、库/包查询沿用现有尝试超时；**没有宣称下层内核阻塞时的硬性总时限，也没有本轮性能峰值结论**。

## 真实材料与封存顺序

官方 -34 image/modules 已存在本机 apt 缓存，故实际没有下载大型镜像，也未在宿主安装包。先验签缓存仓库材料，再按已签名索引核对两个 `.deb`，只在新的项目实验目录解包。image 16,723,096 字节、modules 168,222,912 字节，合计 184,946,008 字节。源码中的 extract-vmlinux 帮助脚本原本硬编码 `/tmp`；实验只修改其一次性副本以把临时文件放回项目目录，原版和副本摘要均留存。

| 材料 | 精确绑定 |
| --- | --- |
| 内核路线 | Ubuntu 24.04 amd64，`7.0.0-34-generic`；image/modules 均 `7.0.0-34.34~24.04.1`，Source 为 `linux-signed-hwe-7.0` / `linux-hwe-7.0` |
| image `.deb` SHA256 | `2bd0e1fa6fd72b00041f4523b97b730c94a9c14198a8f07231d24d49d334bd37` |
| modules `.deb` SHA256 | `e81f48d95ef2ee583b342836491cc77c7fe3fbe62735b3283bc43aec5d1c7e8c` |
| 启动 image SHA256 | `73d9c6e40b210deb638070d6af591a68bb6d7ff0280d0ec34c9ccf705a4f47ac` |
| 真实管理包 | `lab/work/cq-1005/package-r3/ram-rescue-handler_27ab19e4da8c6aaab8dfeee0_amd64.deb` |
| 真实包 SHA256 | `c7390f352d0c3dbca476f3618842a5a95f5a4a7e8659e7f0d81f2b145fd5a56c` |
| 静态 subject SHA256 | `3d2c493cc6ab6162406ef09999fda5164d683b748e049c43d5ade42dd9cc6987` |
| 本地候选前置资格 SHA256 | `c8f0c4b7cbea87f012b0015047b90f325accba7b4c6a66bed939513e8047c041` |

这不是把 -34 写入生产白名单或公开最低要求。采集没有固定 release；其他组合缺少匹配证据/资格照常阻断。正常 HWE 当前版本的公开发布验收仍缺失。

顺序为：封存管理包 → VM 安装同包 → 无资格情况下运行原版固定 CLI plan → 六项 binding、完整准入通过，唯一 blocker 为 `combination_unvalidated` → 留存前置报告 → 根据该报告创建范围明确的外置资格 → 同包同入口 plan/stage/cancel。前置资格只授权保存候选，无须先完成 stage 才能生成；后续生命周期报告独立留存，不反写到被验包或前置资格，避免循环和自引用。

没有调用内部 lookup、注入 current、替换原版计划或提供 CLI 绕过。实验观察器写资格是受授权的验收制备行为，文件仅存在一次性 VM 和证据导出目录；没有写入宿主可信资格目录。

## 隔离 VM 的实际结果

复用 `lab/work/rp-1004c/seed/s0/usb.raw`、普通 Ubuntu initramfs 与核验一致的内核。新建 qcow2 overlay 和一次性数据盘文件，2 vCPU、3072 MiB RAM、无网卡、一次一台；所有 QEMU block 输入为 `lab/work` 下普通文件，无宿主真实块设备。observer 为验收创建现有单路径 DM 前置对象，这不是新增产品建图功能；没有登记或启用保护。

| 实际动作 | 结果 |
| --- | --- |
| 无资格原始 plan | rc 0；完整介质准入通过；六项 pass；资格 absent，状态 blocked、仅 `combination_unvalidated` |
| 写入真实前置资格后原版 plan | rc 0；ready；仅 `save_candidate_only`，非激活/恢复授权 |
| stage + 重复 stage | 均 rc 0；prepared，同 ID；enabled=false、reboot_activates=false |
| cancel + 重复 cancel | 均 rc 0；同一 cancelled 结果，cleanup_pending=false |
| observer 仅追加 fstab 注释 | subject 保持相同；本机 plan 摘要从 `b919b8c9…` 变为 `4c084d80…`；旧摘要 stage rc 2，reason=plan_changed |
| 实际 SIGKILL 后重入 | 真实 CLI rc -9；kill 后再次读到 preparing，四个材料齐全且载荷摘要匹配；同 ID 重试 rc 0、prepared |
| 顺序第二次启动同一 overlay | boot ID 改变；无候选数据 DM、无 ram-rescue-path、无 RAM runtime、无根配置；三个相关服务均 inactive/MainPID=0；未安装活动 registry/units |
| 重启后撤回资格并 cancel | rc 0，cancelled；候选不会因资格撤回而无法清理 |

stage/cancel 前后核对活动 registry/准备目录、`/boot`、fstab、根运行配置、相关 unit/udev 规则、DM UUID/slaves/**表内容**、挂载表及服务状态。第一阶段这些观察值相同；observer 明确修改 fstab 后，后续值与仅此修改的预期基线相同。重启比较持久文件并独立断言服务未启动。该结论是代码路径与明确检查范围的证据，不是所有系统调用的审计或真实掉电测试。

本轮最终新增实验目录占用 **2,560,917,504 字节（约 2.39 GiB）**，包括两次失败排查的产物，低于 8 GiB。实验运行时每 2 秒检查，达到 6 GiB 便中止以保留余量；这是监测停止机制，非文件系统硬配额。最终成功还要求实际占用不超限、监测无异常。seed 初次全量 SHA 验证后作为只读 backing 使用，结束 inode/大小/mtime/ctime 不变；没有虚报结束又做了全量哈希。全部 QEMU 进程已经退出。

## 故障修正、测试与可复核性

前两次真实 VM 均停在内核关联未知，未生成资格或 stage。它们的失败报告原样保留。首次错误过于笼统，补了有界 observations 错误信息，并修正 runner 失败后日志提前关闭。第二次定位到 sysfs initstate/srcversion 即使只有几个字符，st_size 仍报告 4096；将读取上限由 128 改为一页 4096，保留可信文件检查，补真实 stat 提示 fixture 后重建 r3。第三次 VM 完整通过，没有将 unknown 强改成 pass。

最终修正后针对性回归 **106 项全部通过**：current_support 17、support 24、planning 33、candidates 32，命令和输出见 [测试原记录](../../lab/results/2026-10-05-real-candidate/targeted-tests.json)。本轮此前 package 6、native_payload 11、ram_environment 9 项也通过，未因收尾重复这些已通过且未再改动的检查。链接/外部修改/部分写入/合作锁边界仍由候选 fixture 覆盖；实际 VM 新增完整 preparing 边界的真实 SIGKILL，不冒充对每个原子步骤的真实掉电或多进程压力验收。

固定包中 81 个 administration 成员与当前树逐一比较，80 个一致；唯一差异是验收后更新说明的 `guard/MANAGER.md`。**全部可执行管理代码与封存包一致**，没有把修改后的文档重打包后继续套旧资格。VM 实际 runner/observer 源码已原样封存；运行后 runner 仅把失败报告的 `active_protection_enabled=False` 改为 unknown/null，防止失败时虚报未启用。这个报告字段修正不涉及生产代码，也未声称新版 runner 重跑过 VM。执行时源码摘要与最终树摘要分开记录。

`kernel-v3` 的 source.json 记录其生成时脚本摘要；后续 current 采集的错误展示/sysfs 上限修正不改写那份历史生成记录。新的包、当前源码及真实运行结果分别记录实际摘要，不能把旧生成脚本摘要称作最终源码摘要。

## 旧证据复用与剩余缺口

本轮真实 runtime 流验证证明：C++ ELF `479c8ba7…`、maintain `e2cd3b56…`、11 项库与旧 v9 清单一致。v9 原恢复报告摘要仍匹配，可保留为相同组件的历史恢复证据；新包管理入口/清单及完整 runtime 归档都已改变，因此**本轮不继承“新包恢复通过”结论**。clean-reproduction 的自建 ELF `3d100689…` 不同，也不能当相同 ELF 证据。

原始 reproduction 路径更正继续保留：解析根 `/workspace/Project/rr-check-1004`，原 `lab/work/r1/reproduction.json` SHA256 `58ee0a6f3a2d9bb7e14370c952bcb72216df10c5647296a2f76fd8dd770db0e9`；引用的 11/11 原报告摘要匹配。没有删除或重写旧汇总。本轮后续状态说明与旧 fixture/静态资格报告分开，不覆盖它们的历史验收层级。

尚缺：面向用户的可信参考/资格分发与正常 HWE 当前组合验收；新候选后续激活/退出生命周期；本包 root 启动集成、真实拔盘恢复、实机安装接受、长期使用和资源峰值验证。本轮无需新增产品取舍，六项 binding 在约定威胁边界内已可实现；停在协调者审阅边界，不继续实施这些后续项。
