# 数据 DM 候选生命周期：工作区实现与 fixture 验收

**后续状态（2026-10-05）：** 本文保留最初 fixture 切片的结论。当前已另行完成实际组合采集及一个真实管理包在隔离 VM 内的候选流程，见 [后续报告](REAL-CANDIDATE-VALIDATION.md)；这不追溯提升本文的旧测试层级，也不代表宿主部署或恢复验收。

本轮按已审 [设计](CANDIDATE-LIFECYCLE-DESIGN.md) 实现最小 `manager stage/cancel`。只处理现有完整准入支持的、尚未登记的数据 DM，复用 C++ 唯一恢复核心，不修改 Guard。既有人工作业清退、发现与计划改动全部保留，没有提交、部署、重启、拔盘、推送或发布。

**首版产品目标已确认：Ubuntu 24.04 / x86_64 / 官方 HWE，逐个验收精确内核与软件包组合。当前没有可放行的真实组合。** `7.0.0-34-generic` 只是历史实验基线，不能作为公开硬要求，也不能用 uname/HWE 名称替代验收。本报告取代上一只读切片“支持策略未决定、尚无 stage/cancel”的当前状态；旧报告与结果 JSON 保留为当时的证据。

## 具体实现与职责

| 位置 | 本轮职责 |
| --- | --- |
| `guard/support.py`、`planning.py` | 将产品目标 decided 与具体组合 unvalidated 分开。默认没有生产验收记录；`combination_unvalidated` 阻止准备。内部 fixture lookup 必须匹配 subject 摘要、目标和证据摘要，CLI 不提供注入或强制放行 |
| `guard/candidate_ui.py` | 中文编号选择、计划、输入“确认”；JSON 使用 `--expect-plan`，不提示交互。不接受外部计划 JSON、输出路径或命令 |
| `guard/manage.py` | 复用 `exclusive_control`，确认后才创建管理锁并调用候选模块；status 新增独立 candidate_operations，不并入 devices |
| `guard/candidates.py` | 固定私有目录和四种文件、摘要绑定、准备/撤销/重入、配额与受限清理。没有激活消费者或常驻进程 |
| `guard/trusted_paths.py` | 将原有 bounded/change-detecting 描述符读取抽为 read_descriptor；既有 read_trusted 继续复用它，语义不变 |
| `discovery.ConfigurationSnapshot`、`diagnostics.Reader` | 取消前复用有界可信读取和前后比较，检查活动登记名单/内容与根配置；不另造锁或原子快照系统 |

取消活动检查只需要根配置中的映射关系，不读取根身份介质或启动 RAM 工具。登记不完整、不可读、链接或前后变化都会拒绝；没有登记时可离线撤销候选。已完成 cancelled 经完整性和摘要检查后直接返回，即使对象后来另有活动登记也不重复清理。

## 确认、I/O 和证据状态

1. 发现、选对象、构造计划、展示以及确认前拒绝/EOF/Ctrl-C 均不创建持久候选，也不创建管理锁。
2. 明确确认后进入现有合作锁，**重建完整计划**。摘要不同或组合未验收即拒绝，不悄悄替换确认内容。
3. 依次写 preparing 收据、manifest、规范化确认计划、候选登记，随后再次完整重建计划并复核实际文件。输入变化留下 failed_needs_review，不能写成功收据。
4. 同一有效 prepared 重用原 ID；完整 preparing 通过同一最终核验路径收尾。部分写入不会自动猜测修补；先标失败，允许安全取消，或再次确认建立新 ID。

首次成功 stage 经过三轮 plan（展示、锁内、物化后），每轮成功的现有准入路径有 **6–7 次选中分区 blkid 探测**，还包含只读打开、ioctl 和 DM/环境查询。不是无 I/O。身份命令 3 秒、挂载解析 45 秒、systemd 查询 4 秒为各自尝试超时，末段 15 秒为协作预算，**没有硬性总时限**；持锁重建也可能等待下层阻塞。重用完整候选少一轮物化；cancel 不做完整介质准入，只读取候选和可信配置。没有把“非写入”称作“零开销”。

| 状态/证据 | 实际含义 |
| --- | --- |
| `product_target: ... / target_state: decided` | 用户已选择首版产品目标，不证明某个包或内核可用 |
| `combination: unvalidated` | 当前没有匹配的真实组合验收记录，生产准备阻断 |
| `plan.status: ready` | 内部匹配验收 fixture 下可进入确认；本轮没有真实 ready 组合 |
| `prepared / enabled=false / reboot_activates=false` | 私有候选已准备；不等于待重启，不会靠重启启用 |
| discovery 的当前 owner / topology | 当次独立只读观察，不由候选收据推导 |
| `recovery_experiment: not_evaluated` | 当前流程没做恢复实验；历史 QEMU 不提升这个状态 |

本轮没有生产验收加载器。support subject 绑定当前发现环境、可信配置指纹和准入上下文（包括管理清单摘要），用于验证确定性门控和变更失效。它**尚非完整的发行版、官方内核来源和完整软件包认证模型**。内部 fixture 注入只证明匹配/拒绝逻辑，不能生成合格清单。

## 存储、失败与撤销

固定目录 `/var/lib/ram-rescue-plans/operations/<32位ID>/` 与活动 `/etc/ram-rescue-manager/devices`、现有 `/var/lib/ram-rescue-manager` 分开。目录 0700、文件 0600；路径及属主可信，文件必须普通单链接，拒绝符号链接。读写/删除使用已核验目录描述符；复用 atomic 的文件 fsync、replace、目录 fsync，新目录也同步父目录。

首个 operation.json 就内嵌预期 manifest，因此即使后续 manifest.json 尚未写出，也可检查部分生成物归属。manifest 不散列自身或可变收据，不产生哈希自引用。plan 绑定 selected_object 与 candidate 内容；候选登记复用 record_from_profile/validate_record，设备实例仅在计划输入里。每操作最多 1 MiB，活动/未完成最多 16，总操作记录最多 256（预留最终收据名额），达到限制拒绝新建，不自动删历史。

| 中断或外部变化 | 实现行为 |
| --- | --- |
| mkdir 后首收据前中断 | 空目录保留、计配额、status 显示需审阅。新确认可创建另一个 ID，并报告残留，不自动递归清理 |
| 固定文件部分物化 | 符合摘要的已写成员可撤销；stage 不自动补齐未知现场 |
| replace 成功后 fsync 报错 | 不假定没有写入；重入读取实际收据和文件决定状态 |
| 完整 preparing | 再次重建计划、复读文件；输入或文件改变即拒绝成功 |
| SIGKILL 留下陌生临时文件 | 不猜测归属、不清理。若可信收据还可提取摘要，该摘要仅用于拒绝同摘要重试，不授权删除 |
| cancel 中断 | 先持久化 cancelled+cleanup_pending，再逐项复核/删除 plan.json 与 candidate.json 并同步；重入只清理仍存在且匹配的成员，最后清掉 pending |
| 重复 cancel / cancelled 后 stage | 已完成取消不重复写；新准备生成新 ID，旧操作不复活 |
| 链接、硬链接、错误属主/权限、摘要损坏、未知字段/状态或外部改动 | 拒绝覆盖/清理并保留现场；不存在通用修复或强制取消参数 |

合作锁只串行化本项目合作管理操作；前后比较不能证明原子快照或检测变化后复原，也不阻止其他 root 在最后一次检查后修改文件。本轮没有宣称防御恶意 root、真实掉电持久性或任意文件系统损坏。

## 实际检查

针对性 fixture 回归 **204 项**：candidate 30、planning 26、discovery 46、manager 27、trusted_paths 7、package 6、admission 23、data 39。数字按 unittest 用例计算，原子边界、删除边界、链接种类等还含多项 subTest。命令、最终源码摘要和输出保存在 [结果 JSON](../../lab/results/2026-10-05-candidate-lifecycle.json)。stage/cancel 的真实 CLI 仅执行 `--help`，未对本机介质运行这两个动作。

候选测试覆盖：拒绝/EOF/Ctrl-C/无自动化摘要零候选写入；确定性计划变更与未知组合拒绝；实际复用准入 fixture；五次阶段原子写入、两次取消收据写入、两次删除各自之前/之后模拟进程丢失；replace 后错误；完整/部分重入；重复撤销；链接/硬链接/属主/权限/未知成员；同摘要损坏、孤儿保留、历史配额；确认后与重入核验期间篡改；登记同名/同 UUID、名单同数量替换、内容变更、悬空链接、不可读拒绝。锁测试用独立打开的 fd 持有同一现有 flock，验证 stage/cancel 无法进入回调；这不是多进程压力测试。

负面断言替换了活动管理写入、服务/设备命令及 subprocess，成功分支只留下私有候选和测试管理锁；不调用 register/prepare/install/upgrade/uninstall、DM mutation、mount/umount、udev 或启动配置写入。包测试只构建并清理微型 fixture `.deb`，不是本轮真实管理安装候选。崩溃测试使用 BaseException/故障注入而非杀实机进程或掉电。没有性能长测、QEMU、实盘准入、跨内核验证或新生产支持结论。

## 仍缺的组合材料与最小后续方案

已有成果可以提供证据链的输入，但不能直接新增合格项：历史 v9 的 package.json、administration.json、native runtime/library/dependency 清单、standalone-data 报告及 Ubuntu 签名材料。它们绑定旧管理代码与旧实验内核；acquire_kernel 可能复制宿主 image，不能仅凭 runner 名称推断官方 HWE 来源。没有完整冻结当前目标的内核 image/modules 包、版本、架构与仓库认证链。

**2026-10-05 路径更正：** 本报告初版曾写“原 reproduction.json 当前缺失”，这是把汇总的相对路径解析到主工作树所致。实际解析根为 `/workspace/Project/rr-check-1004`；`lab/work/r1/reproduction.json` 存在，SHA256 为 `58ee0a6f3a2d9bb7e14370c952bcb72216df10c5647296a2f76fd8dd770db0e9`。本次复核汇总引用的全部 11 份原始报告，摘要均匹配；原汇总与原始 JSON 均未修改。旧 candidate-lifecycle 结果 JSON 中本 Markdown 的摘要对应更正前版本，继续保留作历史证据，不冒充当前文件摘要。

**后续实现说明：** 上文的内部 lookup 与动态 subject 是本报告初版对应的旧切片；现已由 [静态资格切片](STATIC-QUALIFICATION.md) 替代。固定只读加载器已实现，生产当前运行关联证据仍为 unknown。本报告旧测试计数与来源摘要不提升为新切片验收。

最小后续路线（须另经协调者审阅，不在本轮执行）：

1. 选择一个实际目标组合，保存 Ubuntu 来源/版本/架构、已认证官方仓库索引与内核 image/modules 包摘要和版本。核验项目必需 DM 特性，不依赖内核名字推断。
2. 封存当前管理代码清单及真实包；核对能否复用原 C++ ELF、共享库、工具归档和 Ubuntu seed。源码 commit 不代表脏树，应使用实际逐文件摘要。
3. 经授权在隔离目标环境验证真实固定特权入口的计划、候选、取消与故障重入。恢复证据只有在被测 C++/库/内核等相关摘要相同且检查范围匹配时逐项继承；新增或变化部分另验收，不泛化旧 QEMU 全部结果。
4. 外置不可变组合验收记录绑定：发行版/架构、内核 image 与模块包/来源、管理清单、runtime/archive、ELF/库、真实 `.deb`、观察器源码和验收报告摘要、明确排除项。先封存包，再测试报告，最后验收记录；不得把包自身摘要嵌回该包造成自引用。未来名单读取与可信分发另做小切片。

当前验收层级是**工作区源码及 fixture**。没有本轮真实构建包、可信安装候选、已安装实例、恢复实验或实机接受证明。停在协调者审阅边界；不进一步实现激活、根盘集成、裸分区建图或卸载。
