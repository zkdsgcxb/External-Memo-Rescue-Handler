# 静态组合资格与本机确认摘要

**后续状态（2026-10-05）：** 下文保留本切片当时的 unknown/fixture 边界；实际 current 采集和一个真实包的隔离 VM 候选验收现已闭合，见 [后续报告](REAL-CANDIDATE-VALIDATION.md)。原始结果 JSON 与其历史摘要未改写。

2026-10-05。本轮完成工作区实现与专项 fixture 验收，等待协调者审阅。只读资格加载器已实现；**生产当前证据采集尚不能建立完整运行关联，仍返回 unknown，真实候选准备继续阻断。** 没有建立任何真实合格组合，也没有生成可供安装的成功资格文件。

## 改动与字段边界

`guard/support.py` 替换旧动态 subject 与内部 lookup：显式投影静态组合、读取独立资格、核对独立当前证据。`planning.py` 保留现有发现和完整准入，通过前后采样及本机摘要会合这些证据。`candidates.py` 只调整准备门控的窄授权检查，恢复核心与取消流程未增加新状态机。

| 层 | 精确字段或数据 | 用途 |
| --- | --- | --- |
| 可分发 `subject` | `schema=1`；`platform.{id,version_id,architecture}`；`kernel.{release,image_sha256,modules_manifest_sha256}`；`administration.{manifest_sha256,entrypoint_sha256}`；`runtime.{manifest_sha256,archive_sha256,binary_sha256,libraries_manifest_sha256}`；`host_dependencies.manifest_sha256` | 相同软件组合的稳定标识，不因用户设备或挂载变化而变化 |
| 当前关联证据 | `executing_management`、`running_kernel`、`loaded_modules`、`official_kernel_origin`、`runtime_artifacts`、`host_dependencies` 六项独立状态，以及有界观察摘要 | 当前执行/使用的内容与 subject、来源之间的关联；必须全部 pass，不能由资格文件补齐 |
| 独立前置资格 | `schema=1`、`purpose=candidate_preparation_prerequisites`、`result=passed`、`subject_sha256`、显式 `scope`、四个外置 `evidence` 引用摘要 | 可信本地记录明确断言这个静态组合的候选准备前置条件通过 |
| 本机确认 | 设备身份、boot ID、diskseq、挂载、fstab、swap、holders、DM 表、登记/收据/候选输入、拟确认效果，以及上述当前采样与资格原始字节摘要 | `plan_digest`；用户确认的是这次对象、现场、资格与实际效果 |

subject 严格拒绝未知字段，包括嵌套动态字段、包自身/报告/资格摘要。平台限于用户已选定的 Ubuntu 24.04；唯一别名边界 `support.architecture()` 把 x86_64 和 amd64 统一为 amd64。不以任何 `-34` 名字或 HWE 字符串作为放行白名单；官方 HWE 来源仍需独立证据。

静态清单摘要未来必须覆盖实际固定成员、版本和文件摘要，而不是仅散列包名：模块清单覆盖必要模块与依赖、对应 image/modules 包；管理清单覆盖实际特权入口执行的代码；运行清单覆盖将使用的 archive/ELF/共享库；host_dependencies 覆盖实际冷管理依赖。本轮没有编造这些清单或实现通用认证生成器。

## 固定资格读取与信任

只读位置固定为 `/usr/share/ram-rescue-handler/qualifications/<subject_sha256>.json`。文件名来自严格的小写 64 位 SHA256，无目录扫描、任意路径参数或 CLI 绕过。每次至多 16 KiB，复用 `diagnostics.Reader` / `trusted_paths`：固定逐级目录描述符、root 所有、目录/文件不允许组或其他用户写、禁止符号链接、普通单硬链接文件、有界读取与读取期间变化检测。内部 fixture 只能通过 Python 注入临时根和测试 UID；生产 CLI 没有这些选项。

同一次读取的原始字节用于 SHA256 和解析；空白变化也撤销旧确认。JSON 拒绝重复键、非有限数、未知/缺失字段、不匹配 schema、坏摘要、不明确通过的结果。无记录、不可读、损坏、subject 不符或范围不符均阻断。

`scope` 只接受 `profile=host-data`、`topology=existing_single_path_dm`，以及非空、去重的显式文件系统列表。列表只能使用现有准入已定义的 ext4/exfat/vfat；这不代表三者都已获得真实组合验收。计划选中的文件系统还必须实际在该记录列表内，DM 关系仍由完整现有准入判定；不允许通配符、裸分区建图、根保护或激活用途。

`evidence` 精确为 `prerequisite_report_sha256`、`origin_report_sha256`、`test_source_sha256`、`package_sha256`。它们是可信资格作者对外置材料的引用，加载器不下载或重新执行报告。**root 所有只建立本地信任边界，不是发布者签名，也不证明报告内容真实。** 审阅来源材料和接受资格记录仍是独立分发/验收职责。

通过的门控状态明确为 `eligible_for_candidate_preparation`，唯一授权 `save_candidate_only`；`release_acceptance` 与 `recovery_acceptance` 均为 `not_evaluated`。候选保存为 prepared 仍不等于启用或待重启，单纯重启不会生效。

## 当前运行关联：已实现的保守边界

当前生产 `current_evidence()` 只做少量有界观察：uname 的 release/machine、`/usr/lib/os-release`、`/sys/kernel/notes`、本管理模块所在安装树旁的 administration/runtime 清单和固定管理入口。读取最多约 1.2 MiB/次；没有运行 subprocess、扫描库目录、散列整个 runtime archive 或打开块设备。来源不可读时保留 absent/unreadable，不把异常当成功。

这些材料仍不足以证明：

- 正在运行的内核对应哪一个经认证的官方 image，而非只找到同名 `/boot` 文件；
- 已载入模块对应哪些来源文件及依赖，而非只验证磁盘上的候选副本；
- 执行中的管理代码、实际使用/将使用的运行产物和冷管理依赖，已经按同一清单完整核验。

因此当前采集返回 `subject=None`、六项 `unknown`。已有特权入口的本地代码完整性核验还没有作为可信进程内结果传入本流程；本轮不新增可伪造环境标记或“已经验证”收据。即使安装一个 matching 资格文件，也不能替代当前关联证据或使生产准备成功。JSON reasons 明确列出 `static_subject_incomplete` 与各项 `current_*_unknown`。

成功 fixture 独立提供完整 current 数据，并在临时可信树写入资格文件，通过真实加载器读取；不再使用旧 `support_lookup` 直接返回“接受”。这只验证组合/信任/确认逻辑，不能证明 fixture 哈希代表任何实际内核。

## 与计划、候选和撤销的衔接

完整准入前后各采样一次 current 与资格，将两个快照摘要纳入 confirmation；样本变化或变不可读时加入 `support_inputs_changed`。资格原始内容摘要也明确展示在 policy 内。动态现场仍保留在原有 discovery/context/instance 输入中，未从本机准入删掉。

计划还交叉核对静态 kernel.release 与本次 discovery/admission 观察到的 release，以及 administration.manifest_sha256 与准入上下文实际读到的管理清单。管理清单改用原始字节的有界可信读取，避免 `Metadata.text()` 去除首尾空白后与产物 SHA256 不同。矛盾时 `support_context_mismatch` 阻断；没有增加第二套准入逻辑。

确认后已有 stage 路径在合作锁内重新构造计划，物化后再次完整核验。资格变化和软件组合变化都会撤销旧确认；写后变化留 `failed_needs_review`，不会保存成功结论。取消只依赖候选归属/摘要与活动配置检查，资格被撤销也仍可安全取消。

前后比较有界但不是原子快照，不能检测变化后完全复原，也不防止其他 root 在最后检查后改文件。资格读取有字节上限，没有宣称内核 I/O 的硬性总时限。plan 原有介质读取、6–7 次成功路径 blkid 尝试及协作超时边界均保留。

## 专项验收

本轮 **123 项** unittest 通过：support 24、planning 33、candidate 32、manager 27、trusted_paths 7。子场景由 subTest 覆盖，计数不另膨胀。实际命令、输出及最终源码摘要见 [本轮结果 JSON](../../lab/results/2026-10-05-static-qualification.json)。未重复旧长测或构建真实包。

| 场景 | 证据 |
| --- | --- |
| 两台不同身份/boot/diskseq/挂载/fstab 现场，组合相同 | 相同 subject/资格 policy，两份 ready 本机计划摘要不同 |
| 内核、模块、管理入口/清单、ELF、archive、运行库、管理依赖变更 | 各静态字段变化均改变 subject，旧资格不能匹配 |
| 架构规范化 | amd64/x86_64 的 subject、资格和计划摘要一致；其他平台/架构拒绝 |
| 文件系统或拓扑范围越界 | 即使 subject 匹配仍阻断；根保护/激活/恢复/完整发布目的、通配符拒绝 |
| 资格格式与可信路径 | 未知/重复键、坏 JSON/摘要、过大记录、符号/硬链接、父路径链接、错误属主/权限、FIFO 拒绝 |
| 独立当前证据 | 资格有效，但任一 current binding unknown/fail 仍拒绝；uname/候选文件不提升为 current pass |
| 计划与物化边界 | 资格出现、消失、变更、权限改变或 current 状态改变均撤销旧确认；写后变更留下失败收据 |
| 撤销与副作用 | 撤回资格后能取消；保留既有崩溃/重复撤销/合作锁 fixture；只读 loader 无写打开、设备访问或子进程；准备不触发活动登记/DM/服务/挂载/引导写入 |

首次 support 测试暴露临时测试树受 umask 影响产生组可写父目录；可信读取正确拒绝，已修正 fixture 目录权限后通过。未为让测试通过放宽生产权限检查。权限、崩溃与锁均为隔离 fixture 检查，不是实机掉电或恶意 root 验证。

## 旧证据路径更正与下一步缺证

此前 CANDIDATE-LIFECYCLE 初版称原始 reproduction.json 缺失，是相对路径解析根错误。实际根为 `/workspace/Project/rr-check-1004`；其中 `lab/work/r1/reproduction.json` SHA256 为 `58ee0a6f3a2d9bb7e14370c952bcb72216df10c5647296a2f76fd8dd770db0e9`。本轮按此根复核 clean-reproduction 汇总引用的 **11/11 份原报告**，摘要全部一致。旧汇总、原始报告和旧测试结果不修改；旧文字报告添加带日期的更正与后续实现说明，旧结果 JSON 的文档摘要仍指向其历史版本。

包、资格、报告避免自引用：先冻结可重用的管理/运行/依赖内容和包，独立完成前置与来源报告，最后制作外置资格。subject 不包含 `.deb` 自身 SHA、资格或报告 SHA；外置资格可以引用包 SHA。资格不打回那个被验收的原包；后续候选生命周期验收报告可引用资格 SHA，不能再倒灌进前置报告要求自身成立。

没有“先 stage 才能生成 stage 前置资格”的循环：前置资格只依据独立产物/来源/前置准入检查与相应 fixture 证据；在完整当前关联采集另行实现并验证后，隔离 VM 才能用真实固定入口验证 stage/cancel，结果形成另一份后置验收报告。这也明确了当前阻塞：**只靠现有加载器加资格文件，尚不能做真实 CLI 的成功验收**；下一个小切片应先设计/补齐运行关联采集的可信输入，不能用资格声明绕过缺证。

历史 `7.0.0-34-generic` 仅可作为首个工程验收组合，不是公开最低内核。正常官方 HWE 当前版本的公开发布验收缺口保留；旧 seed/ELF/库/报告仅在相关摘要与覆盖范围实际匹配时复用。用户首版 Ubuntu 24.04 amd64 官方 HWE / 逐组合验收的决定不变。

本轮没有下载大型镜像、构建真实候选、启动 VM、实机执行 stage/cancel、部署、重启、拔盘、提交、推送或发布。验收级别仍为**工作区源码与 fixture**，当前已有未提交修改保留。交协调者审阅后再安排下一步。
