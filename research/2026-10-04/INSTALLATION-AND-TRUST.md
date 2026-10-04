# 可信安装、运行副本与冷态升级

本轮将普通用户的源码构建与特权执行分开。源码、构建产物、测试镜像留在工作区；安装之后只通过 root 所有的固定入口执行管理代码。该机制验证本地文件完整性和权限，不提供发行者签名，也不能把从不可信来源下载的包变成可信软件。

## 信任顺序

1. 审阅源码来源、提交及实际变更；在普通用户下构建和测试。
2. 构建 `.deb`，检查其内容与 SHA256。包没有 maintainer scripts，不自动启用服务、登记磁盘或修改启动配置。
3. 通过本机管理员认证将已审阅包复制到私有 root 目录，再核对该副本的 SHA256，最后使用系统 `dpkg` 安装这个固定副本。
4. 后续操作使用 `/usr/bin/rescue-guard-admin`。入口以 Python 隔离模式运行，只使用标准库核验自身版本目录，再加载清单中的管理代码。
5. 保护镜像输入另行封存：普通用户生成候选 bundle；固定入口按操作者已审阅的 SHA256 复制到 root 私有目录，再检查大小、成员类型、构建清单和测试报告。封存本身不改启动配置。

散列用于固定本次已审阅的字节；旁置 SHA256 文件不是发布者认证。若用户主动授权安装不可信的 root 软件，此项目无法提供额外隔离。当前不新增在线更新器、网络控制端口或下载后自动安装。

## 构建离线管理包

使用同一次已验证构建的通用 RAM 工具和原生 ELF，例如：

```bash
python3 guard/package.py --output lab/work/admin-package \
  --base-rescue-dir lab/work/reproduce/base-rescue \
  --native-binary lab/work/reproduce/protected/native-runtime/guard-runtime
```

`package.json` 记录包 SHA256、源码提交、管理版本、RAM 归档及原生依赖。版本同时包含源码和 RAM 归档内容，工具或库改变会生成新版本；gzip 时间戳不会造成无意义版本变化。构建器拒绝携带 ASan/UBSan 库的实验运行包。

安装前使用 `dpkg-deb --info`、`dpkg-deb --contents` 检查本地产物。以下需在自己的终端交互认证；替换 `候选.deb` 和 `已审阅SHA256`，不能把示例当作已验证结果：

```bash
sudo install -d -m 0700 /var/lib/ram-rescue-package-review
sudo install -m 0600 lab/work/admin-package/候选.deb \
  /var/lib/ram-rescue-package-review/candidate.deb
sudo sha256sum /var/lib/ram-rescue-package-review/candidate.deb
# 与独立记录的已审阅SHA256逐字核对后：
sudo dpkg --install /var/lib/ram-rescue-package-review/candidate.deb
```

工具不会替代上述来源审阅。安装后的 `/usr/lib/ram-rescue-handler/<版本>/`、入口及父目录均要求 root 所有、非组/其他用户可写；代码成员必须是普通单链接文件，路径各级不跟随符号链接。内核设备路径和 sysfs 有单独语义，不能套用普通配置文件规则。

## 数据盘独立运行

普通 Ubuntu 启动即可使用新包。当前内核须匹配包记录，stock `multipathd.service/socket` 不得同时管理设备。此外，全局 `dm_multipath.queue_if_no_path_timeout_secs` 须显式设置为至少 10 秒，满足 8 秒准入预算及兜底余量；本轮普通启动 VM 使用内核参数 `dm_multipath.queue_if_no_path_timeout_secs=10`。默认值 0 不满足此有限兜底契约，登记会拒绝；项目不擅自改全局设置。预先建立本项目支持的稳定映射后：

```bash
sudo /usr/bin/rescue-guard-admin manager register --device /dev/mapper/rr-data-example
sudo /usr/bin/rescue-guard-admin manager install
sudo /usr/bin/rescue-guard-admin manager doctor
sudo /usr/bin/rescue-guard-admin manager export
```

登记可以在安装统一服务之前完成。管理服务按启动需要准备 `tmpfs,noswap` 工具环境，绑定实际 `/dev`、`/proc`、`/sys`、`/run`，只启动登记对象。若保护根盘已有同版本 RAM 环境则复用；否则使用独立 RAM 工具。已运行环境遇到不同版本会拒绝热替换。服务使用真实的 `/run/ram-rescue-manager/rootfs` 挂载路径；复用根盘时建立绑定视图，不复制第二套工具页。

数据启动准入只读检查本次 RAM 准备时固定的宿主 `fstab` 文件。文件在原 inode 上的编辑仍可见；若通过原子替换更换了 inode，再次管理准入会拒绝并要求正常结束数据使用后重启。直接由 udev 启动的已登记实例沿用这一冷态基线，因此修改已登记设备的 fstab 规则后，应重新启动并核验。实际挂载、swap、设备实例及身份检查仍使用实时内核状态。该单文件绑定由冷管理过程完成，恢复和死亡接管不新增宿主根盘路径访问，也不获得 `CAP_SYS_PTRACE`。

数据映射的创建、文件系统挂载及退出仍由调用者和原生 systemd 配置负责。该包不自动格式化、不创建 DM 映射、不接管任意 U 盘，也不取消错误身份的最后一次核验。

## 封存和安装保护镜像

构建和完整 VM 通过之后，先在普通用户下生成 bundle：

```bash
python3 guard/stage_inputs.py bundle \
  --build-dir lab/work/host-build \
  --enrollment lab/work/host-enrollment/enrollment.json \
  --vm-report lab/work/已通过实验/report.json \
  --output lab/work/reviewed-image.tar
```

记录输出 SHA256，通过固定入口封存：

```bash
sudo /usr/bin/rescue-guard-admin image-inputs stage \
  --bundle "$PWD/lab/work/reviewed-image.tar" --sha256 已审阅SHA256
```

封存成功返回 `/var/lib/ram-rescue-candidates/<SHA256>/`。此后首次安装使用 `install-image`，已有保护入口使用 `upgrade-image`，传入该目录的 `build.json/initrd.img/enrollment.json/vm_report.json`。默认先只读预检；明确加 `--install` 才写入。当前 root 所有的管理包必须与镜像构建源码清单一致，否则拒绝执行。

```bash
sudo /usr/bin/rescue-guard-admin upgrade-image \
  --build-dir /var/lib/ram-rescue-candidates/已审阅SHA256 \
  --enrollment /var/lib/ram-rescue-candidates/已审阅SHA256/enrollment.json \
  --vm-report /var/lib/ram-rescue-candidates/已审阅SHA256/vm_report.json
# 审阅预检后，对同一组参数加 --install。
```

保护镜像升级保留旧 initrd、收据和事务证据；写入失败执行已有事务回退。原普通 Ubuntu 启动项保持可用，rEFInd 和当前 RAM owner 不由该命令修改。真实重启、F9 登录、桌面应用与睡眠唤醒需要另行现场验收。

## 已有救援终端的日志服务冷更新

`upgrade-image` **只更新保护 initrd，不自动更新宿主的 `ram-rescue-log.service`**。新日志服务资源已经随离线包安装到 root 所有的版本目录，但现有 F9/F10 救援终端安装仍需单独替换该 unit。下面仅适用于已经正常安装救援终端的机器，不是新建终端安装器，也不升级普通回退启动使用的旧 RAM 工具归档。保留 `/etc/ram-rescue-demo/shadow`、现有账户、F9/F10 unit 和 prepare 配置。

先完成前述离线包的审阅和安装。通过固定入口执行一次只读命令，入口会先核验该版本的整个管理资源清单；如果出现入口或清单校验错误，停止更新。下面所有“已核验版本”“审阅编号”均须换成实际值，资源路径必须是已安装的 `/usr/lib/ram-rescue-handler/<版本>/`，不能指向工作区：

```bash
sudo /usr/bin/rescue-guard-admin manager doctor
systemctl show ram-rescue-log.service --property=FragmentPath,DropInPaths,ActiveState,MainPID
systemctl cat --no-pager ram-rescue-log.service
```

仅在 `FragmentPath=/etc/systemd/system/ram-rescue-log.service`、没有 drop-in、现有文件是 root 所有的普通 unit 且与已知安装相符时继续。存在额外配置时先审阅，不覆盖它们。核对新 unit 的资源 SHA 与 `administration.json` 中的值，再将旧 unit 保存到新的 root 私有备份目录；原权限和 SHA 也应记录在审阅材料中。

```bash
python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["files"]["ram-rescue-demo/src/ram-rescue-log.service"])' \
  /usr/lib/ram-rescue-handler/已核验版本/administration.json
sha256sum /usr/lib/ram-rescue-handler/已核验版本/ram-rescue-demo/src/ram-rescue-log.service
sudo stat -c '%U %a %n' /etc/systemd/system/ram-rescue-log.service
sudo sha256sum /etc/systemd/system/ram-rescue-log.service
sudo install -d -m 0700 /var/lib/ram-rescue-log-review
sudo mkdir -m 0700 /var/lib/ram-rescue-log-review/审阅编号
sudo install -m 0600 /etc/systemd/system/ram-rescue-log.service \
  /var/lib/ram-rescue-log-review/审阅编号/ram-rescue-log.service.before
```

核对备份 SHA 与旧 unit 相同，再从已经核验的 root 包路径复制新资源，检查落盘 SHA 和 unit 语法。若检查失败，恢复私有备份及记录的原权限，检查通过后再继续。此处不读取或复制 shadow，也不调用旧的工作区安装脚本。

```bash
sudo sha256sum /var/lib/ram-rescue-log-review/审阅编号/ram-rescue-log.service.before
sudo install -m 0644 \
  /usr/lib/ram-rescue-handler/已核验版本/ram-rescue-demo/src/ram-rescue-log.service \
  /etc/systemd/system/ram-rescue-log.service
sudo sha256sum /etc/systemd/system/ram-rescue-log.service
sudo systemd-analyze verify --man=no /etc/systemd/system/ram-rescue-log.service
sudo systemctl daemon-reload
```

**不执行 restart、stop 或重新启动当前日志服务。** `daemon-reload` 只让 systemd 读取下一次启动的定义，当前进程及其权限并不会因此改变。此步骤也不自动修改服务的 enable 状态。下一次从匹配的新保护镜像启动后，核对服务的实际 PID、`NoNewPrivileges`、能力集合和进程挂载视图：实际 `/` 应为 `ro`，`/var/log` 应为 `rw`；结合本次 journal 检查是否启动失败。配置文本存在不是生效证明，其它隐式挂载也不能据此声称全部只读。

```bash
systemctl show ram-rescue-log.service \
  --property=ActiveState,MainPID,RootDirectory,NoNewPrivileges,CapabilityBoundingSet,ReadWritePaths
logger_pid=$(systemctl show ram-rescue-log.service --property=MainPID --value)
test "$logger_pid" -gt 1
sudo awk '$5 == "/" || $5 == "/var/log" { print $0 }' "/proc/$logger_pid/mountinfo"
journalctl -b --no-pager -u ram-rescue-log.service
```

普通回退启动仍使用其已安装的旧 RAM 工具包；本轮没有提供该整包的自动升级入口或完成其新限制组合的实机验收。需要回退 unit 时，从上述私有备份恢复并 `daemon-reload`，在正常冷启动后核对，保留原救援凭据。

## 依赖更新与管理升级

通用包记录 ELF 工具、共享库及 Python 扩展的来源包/版本/架构与 SHA256；原生包另记录 C++ 与 libdevmapper 闭包。未纳入 dpkg 管理的输入明确标为未知，不猜版本。纯 Python 标准库文件不逐个进行诊断哈希；其包版本通过对应的标准库/扩展来源记录，清单并非对整个 RAM 文件系统的完整性度量。

宿主 `apt upgrade` 不会自动更新已复制到 initrd 或 RAM 的库。`doctor` 按需比较冻结清单与宿主，不在健康监视中追加扫盘。处理流程是重新构建 → 同版本内核下验证 → 安装新管理包/专用镜像 → 下次启动核对真实 ELF、库与 owner。

管理配置升级使用新包的固定入口：

```bash
sudo /usr/bin/rescue-guard-admin manager upgrade
```

它保留数据登记，但要求数据 DM 映射全部已经正常退出、相关控制器均为 inactive；运行中、失败或正在退出的实例都会阻止升级。既有规则、配置、入口链接和收据先备份，发生写入失败时尝试回退。命令不挂载工具、不替换当前 RAM、不拉起新控制器，仅为下次启动切换持久管理版本。根盘保护镜像须单独配套更新。需要冷回退时重新安装已保留的旧包，再执行同一 `manager upgrade`；完整旧管理版本仅在代码、依赖清单和归档逐项一致时复用，不就地修补。仍要求数据映射已退出，回退后再次启动并核对实际运行副本。

自动 Guard 仍保留为 DM ioctl 所需的 `CAP_SYS_ADMIN`；chroot、systemd 限制及 root 文件权限都不是抵抗 root 管理员的边界。介质解析 helper 继承 Guard 的限制，尚未独立降 UID。真实设备身份真实性认证、Secure Boot 引导链改造和通用热升级均不在本轮能力内。
