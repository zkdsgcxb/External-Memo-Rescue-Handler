# 只读 manager plan 与确定性摘要验收

日期：2026-10-05。工作区切片完成，交协调者审阅。**没有 stage/cancel 持久化实现，没有新状态目录、部署、提交、发布或长测。公开支持范围未决定，没有把本机内核加入白名单，也没有跨内核构建/验证。**

## 实际入口与输出

现有管理入口增加 `manager plan --device <对象> [--json]`。它接受既有映射、其唯一底层分区或挂载位置；解析复用 `manage.resolve_map()`，不另写一套设备选择器。中文输出和 JSON 都列阻碍；没有交互确认或执行计划的分支。JSON 模式只向 stdout 输出一个 JSON 对象，读取前的 I/O 提示写 stderr。

当前实现中 `support_policy_unknown` 始终是 blocker，`status` 始终为 `blocked`，没有覆盖支持策略的 CLI 参数。符合既有技术条件的未登记数据映射仍可完成只读介质核验，给出候选预览；这不等于接受其发行版/内核组合。`admission_passed` 只是现有准入调用当时通过，之后输入变化或策略未知仍禁止准备。

| 对象 | 此切片结果 |
| --- | --- |
| 已有约定 DM、未登记的受支持数据卷 | 前置证据齐全时复用完整准入，形成候选内容与效果预览；公开支持未知仍 blocked |
| 已登记数据盘或已保护根盘 | 明确已有登记，不重新学习身份，不运行一套新准入/控制器 |
| 无稳定映射的裸分区 | 返回缺少映射的阻碍，不建图 |
| 裸分区正在挂载、系统盘兄弟分区等 | 在可识别前置冲突时先拒绝，不进入介质探测 |
| 未保护根盘、新拓扑或关键输入不可读 | 给出阻碍，不扩展支持范围或构建启动产物 |

退出码 0 表示计划报告成功生成，**不表示计划可执行**；自动化必须读取 `status` / `confirmation.blockers`。参数错误沿用 argparse 的退出码 2。尚未安装包含本轮改动的真实包，当前旧固定入口不能据此认为已经具备 plan。

## 实现与复用边界

- `guard/planning.py` 组织一次只读计划，增加输入绑定、摘要和呈现；不保存候选、不增加锁、worker 或后台进程。
- `manage.py` 仅增加参数与分发；已有 register/install/prepare 等写入路径未被调用或改造。
- `discovery.collect(inputs=...)` 增加进程内输入交接点，复用原有发现和配置复读。默认 discover 对外行为不变，不把全部私有输入附加到普通发现输出。
- `ConfigurationSnapshot.fingerprints()` 输出稳定的来源标签、存在性和内容 SHA-256。管理候选投影去掉未用于判断的随机 program 显示 token，路径仍由完整收据摘要绑定；doctor 本身的脱敏输出未改。
- `admin.data.collect()`、`record_from_profile()`、`check_environment()` 和 `discovery.map_topology()` 分别负责既有完整准入、唯一登记规范、末次环境复查和表核对；没有复制介质身份验证逻辑。
- `discovery.query_mapper().snapshot_by_uuid()` 用于明确选中的 DM 前后查询；只在模块已加载、控制节点已存在时进入，不运行 DM probe，不创建控制节点/映射的业务路径。

一次 plan 运行两次 discovery；对可进入准入的选中对象，再前后读取上下文和 DM 表，并复查现有环境规则。未登记映射在发现层的 `unmanaged_upper_layers` 不能直接当完整准入已拒绝：plan 将该判断交回现有 `collect()`，额外持有者或不支持的拓扑仍由原规则拒绝。其他明确冲突、环境能力未知或发现读取异常会阻止介质准入。

已有根盘 owner 不能使另一张未登记数据映射被误认作“已登记”；测试覆盖这个共存场景。只读 plan 不改变现有旧 register 立即激活等生命周期行为，这些仍是后续待审阅问题。

## 相比 discover 新增了哪些 I/O

“只读”在这里表示不提交配置/介质写入动作，**不表示无 I/O，也不保证不阻塞**。discover 已会读取普通文件、程序 ELF 和内核元数据，缓存未命中也可能产生正常文件 I/O；plan 另增加所选介质身份读取。

成功的文件系统数据准入路径：

| 调用 | 介质探测次数 |
| --- | ---: |
| `usb_identity()` 初始 `blkid -p -o export` | 1 |
| 初始 `identity_layout()` | 1 |
| fstab 的 LABEL/PARTLABEL 冲突核对（按需缓存） | 0–1 |
| `Admission.verify()` 的两次身份验证与一次布局验证 | 3 |
| `Candidate.revalidate()` 布局验证 | 1 |
| **完整准入成功合计** | **6–7** |

这些 probe 都针对所选分区，失败路径可能更少；文件系统准入不调用 LVM 子命令。USB 唯一性检查会查看其他设备的 sysfs 元数据，不探测其他盘的文件系统。

准入还只读打开选中块分区（`O_RDONLY | O_NONBLOCK`），调用 `BLKGETDISKSEQ`、`BLKGETSIZE64`、`BLKSSZGET`，并查询 DM 表和 target version。原 collect 有三次 active/inactive 快照；plan 另在前后各查询一次选中 UUID 的表，还复用两轮 discovery。**6–7 是 blkid 次数，不是 plan 的总系统调用或查询次数。**

前后上下文还读取：PID 1 的完整 mountinfo、swaps、fstab，分区起点/编号/逻辑块大小、partition/map holders，以及当前管理包的 `administration.json`。读取沿用 Metadata 的普通文件/大小限制；固定特权入口对当前包的信任核验仍由已有 bootstrap 负责。缺少包清单、权限不足或来源读取失败时给阻碍，不能从工作区 sudo 执行源码绕过可信入口。

## 超时核对：不是硬性总截止

本次只读查阅了项目调用链和本机 Python 3.12 的 `subprocess.py`，没有通过卡住实盘或长测验证超时。

| 位置 | 当前实现 | 精确限制 |
| --- | --- | --- |
| `admin.admission.readonly()` → `rescue.command()` | 身份外部命令 `timeout=min(timeout, 3)` | 3 秒后尝试终止子进程；不是整次 plan 或 collect 的预算 |
| `admin.data.collect()` 末段 | `Admission.verify(monotonic()+15)` 并共用至 revalidate | 15 秒阶段边界检查，之前的 2–3 次 blkid、环境/隔离检查不包含在内；最后 check_map 也不带此 deadline |
| `manage.resolve_map()` 解析挂载目录 | `guard.data.run(findmnt)` 的 timeout=45 | 挂载路径解析的独立尝试超时，不适用直接设备路径 |
| `diagnostics.bounded_output()` 查询 systemd | 4 秒循环/等待预算、32 KiB 管道输出上限 | finally 中仍会 kill 后 wait；不能宣称整个函数绝对 4 秒返回 |
| 同步 DM task、设备 ioctl、sysfs/普通文件读、`/proc/*/comm` 扫描 | 没有统一可中断截止 | 不能靠阶段前后检查打断已经进入的内核调用 |

`rescue.command()` 使用 `subprocess.run(timeout=...)`。本机 Python 超时分支执行 kill 后调用不带 timeout 的 wait；不可中断的内核等待可能使回收继续阻塞。`O_NONBLOCK` 不能为后续所有读取/ioctl 提供整体硬截止。readonly 也不会按准入剩余时间缩短下一条 3 秒命令预算。

因此本轮没有修改生产超时逻辑，也没有包装一个“超时即可靠返回”的新 worker。终端和 JSON 明示 `hard_total_deadline=false`，分别列身份命令、挂载解析、systemd 查询和最终准入预算。

## 摘要绑定什么

`plan_digest = SHA256(canonical(confirmation))`，复用 `admin.admission.digest()` 的排序键、紧凑 JSON 与拒绝 NaN 规则。JSON 的 `observation` 单独保留显示时间和随机 snapshot ID，它们不进入摘要。

`confirmation` 包含：

- `support_policy` 的未知状态和所有 blocker；未知不会退化成当前 uname 白名单。
- 选中映射名称/UUID、卷级关联和挂载范围，明确不是整盘覆盖。
- 拟确认的实际效果：只保存私有候选、固定目录类别和四种文件、候选登记完整内容，不激活，重启本身不生效。本轮实际效果另列 `current_effects`，持久写入为空。
- 完整准入返回的 USB/分区/文件系统身份、布局与几何等候选内容；`initial_node/sys_path/diskseq` 独立保留为当次输入，不塞回持久登记。
- 两轮发现输入摘要：块节点及依赖、挂载/swap、boot ID、内核/接口/排队状态、可信配置来源指纹。当前策略保守绑定整轮发现输入，其他设备/挂载的变化也可能要求重新规划。
- 选中 DM 的 UUID、实际 active/inactive 表、安全 flags 和设备号；排除 open_count/event_nr 等无关活动计数。
- 前后 mountinfo/fstab/swap/分区几何/管理代码清单的有界内容摘要，holders，以及完整准入后的环境复查结果。

同样的重要输入和效果会得到同一个摘要。介质身份、设备实例、挂载、登记、准备收据、候选库清单、包或效果变化则改变摘要；本轮内前后变化还明确加入失效 blocker。最终准入失败、instance 绑定失败时不输出持久候选登记。

这些是两次或阶段边界的观察，**不是原子快照**：不能发现改动后又恢复原值，也不能阻止某个输入最后一次读取后再改变。介质身份本身也只在现有准入点读取，之后的最终介质探测属于未来真正接入步骤。后续 stage 必须在合作锁内重新核验、重建并比较确认摘要，不能把旧 JSON 当执行脚本。

当前格式是候选持久化前的审阅协议；未来若确认效果或支持策略改变，摘要也应随之改变，不为兼容旧确认而绕开核验。

## 测试、产物与缺证

本轮专项 `test_planning.py` **24/24**；相关回归 discovery **46/46**、admission **23/23**、data 套件 **39/39**、manager **27/27**，合计 **159 项**通过，`git diff --check` 通过。

专项使用已有 discovery fixture 和可信临时目录，复用真实发现代码，介质/环境/DM 访问用注入适配器。覆盖随机 token/时间独立、效果和候选内容绑定、当次实例、设备/挂载/登记/收据/候选/包变化、fstab/swap/bind-root/holders、DM 变化与无关计数、换盘结果拒绝、准入失败/超时、不重复重试、竞争环境、输入不可读、根盘共存、不重复登记、写入/启停路径禁止、中文/JSON、提示在探测前、无 stage/cancel 命令。

命令、源码摘要和读取边界见 [测试记录](../../lab/results/2026-10-05-readonly-plan.json)，[中文 fixture 预览](../../lab/results/2026-10-05-plan-preview.txt) 是测试数据，不是本机实测计划。只有 help 参数解析在源码入口执行，未对真实设备运行 plan，未读取实盘做本轮介质核验。

| 层次 | 当前证据 |
| --- | --- |
| 源码 | 本轮工作区未提交，保留之前改动；新增只读 plan 与小范围发现输入交接 |
| 构建/安装候选 | 未构建真实新包、未部署或切换；没有新增跨内核产物 |
| 运行实例 | C++ 核心及实际服务未改；没有新增实机恢复或安装验证 |
| 后续写入 | stage/cancel、候选状态目录及启动提交均未实施 |

仍缺未来真实可信包入口与目标环境的验证；fixture 通过不能代替所选介质实测、硬超时保证或公开支持承诺。公开支持方向等用户决定，候选持久化等协调者统一审阅后再推进。
