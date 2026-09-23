# External-Memo-Rescue-Handler

针对本机 USB 外置根盘故障的 RAM 救援终端原型，实现在 [`ram-rescue-demo/`](ram-rescue-demo/README.md)。源码与开发数据放在 shared 卷的本项目目录；安装后的系统运行包位于 Ubuntu 的 `/usr/local/lib/ram-rescue-demo`，运行时工具位于 `/run/ram-rescue-demo` 的 RAM 文件系统。

可重复的虚拟 USB/UAS 断联实验见 [`lab/README.md`](lab/README.md)：真实内核、USB 根盘、LVM/ext4、RAM 救援通道与 QMP 故障注入，实验只使用新建的虚拟磁盘。

**交互方式：本机已安装版是 F9/F10 手动救援；实验新版是后台自动排队与重接，不依赖弹窗。** 新版只在虚拟机中运行，尚未部署到真实根盘。自动恢复的机制、超时退路与实测见 [`lab/AUTOMATIC.md`](lab/AUTOMATIC.md)。

各部件的职责、我们新增的工作、复用的开源实现与可替换边界见 [`ARCHITECTURE.md`](ARCHITECTURE.md)。

## 能力边界

- 正常启动并准备服务后，提供 F9/F10 两个独立密码登录入口、RAM 工具环境与内核日志收集。
- 核验预先登记的 USB 设备、分区、PV/VG 身份；人工确认后，只尝试刷新已激活的 `ubuntu` 或 `shared` 线性 LV 映射。
- 当前构建器绑定本机设备和 Python 3.12/x86_64 工具布局，不是任意磁盘的通用恢复工具。更换设备需要审查登记逻辑。
- 与宿主共用内核，不能覆盖启动早期故障、kernel panic、全局死锁；内核 I/O 阻塞可能让命令无法及时退出。
- 映射恢复不代表文件系统、失败写入或应用恢复；没有自动 fsck、重挂载、USB 重置或网络登录。
- 登录后是 root shell，helper 的操作限制不是安全沙箱；人工命令仍能访问宿主设备。
- RAM 日志重启即失；真实掉盘救援效果尚未完成现场验证。

2026-09-23 只读检查：prepare、tty9、tty10、log 四个服务均 active，两个终端及日志服务均 enabled；运行挂载具备 `noswap`，slice 限额 768 MiB、禁止 swap。此状态不代替人工登录和故障现场验证。历史制作记录见 [`VALIDATION.md`](ram-rescue-demo/VALIDATION.md)。

## 版本管理

仓库根目录是 `External-Memo-Rescue-Handler`，主分支为 `main`。跟踪源码、服务配置、测试、说明和故障摘要；忽略镜像、构建目录、生成的设备清单/校验文件、缓存及原始故障日志。忽略只影响 Git，不删除现有文件。

```bash
git status
git diff
python3 -m unittest discover -s ram-rescue-demo/tests -p 'test_*.py' -v
git add <已检查的文件>
git diff --cached
git commit -m "说明本次变更"
```

新检出仓库不含运行镜像，需要先在匹配的健康本机上按子目录 README 构建，再进行隔离烟测与安装。源码提交不会更新已安装的运行包；运行包更新仍需显式构建、测试和重新安装。

公开版本包含源码、测试和说明，不包含运行镜像、密码散列或原始诊断日志。构建器仍绑定原开发设备的序列号；在其他机器上使用前必须审查并调整设备登记逻辑。

本地 Git 历史位于 shared 卷，不能代替独立介质备份；被忽略的构建产物和诊断数据也不会随 Git 推送备份。
