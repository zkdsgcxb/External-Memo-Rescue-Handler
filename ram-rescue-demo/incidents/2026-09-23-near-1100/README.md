# 2026-09-23 接近 11 点的根盘 I/O 故障

## 用户现场记录

经过 Hub；本次开机后没有合盖、睡眠或唤醒。git clone 中断，随后命令 I/O error；打开新终端时旧终端消失，Codex 无响应，桌面停住。F2 黑屏，F3 黑屏带鼠标，F4 显示 /var/.../journal 的数行 I/O error 后停住。随后用户重启，现已进入正常系统。用户时间为大致回忆，不是精确时间戳。

## 现场复查

故障前一轮 boot ID：40c8bc3630ce43a9927c12f7644e3b86。持久日志最后时间为 10:39:52，不能当作故障发生时间。重启后 boot ID：37a511d2e64044d694b2a8a936082a7d。当前启动有时钟校正迹象，因此不要仅靠跨启动墙上时间推断间隔。

保存的前一轮内核日志没有捕捉到本次末尾的 USB disconnect、UAS reset、块层 I/O error 或 ext4 journal abort；不能据此排除掉盘。当前启动 journald 报 journal 文件 corrupted or uncleanly shut down 并替换；这不足以证明 ext4 文件系统损坏。

当前 SSD 经 USB 3 Hub，驱动 uas；启动命令行尚未设置禁用 UAS 或 USB LPM 的实验参数。救援 demo 未安装，四个服务均 inactive，/run/ram-rescue-demo 与系统安装目录均不存在。F2/F3/F4 属于普通系统入口，本次并未测试 RAM 救援入口。

## 判断及边界

根卷和 shared 卷位于同一个 USB SSD/PV，底层失联可以同时影响 clone、程序加载、认证与 journal 写入。现象与已知掉盘问题高度一致，但此次缺少最终内核日志，不能确认是重枚举、Hub/线材/供电、UAS 还是其他底层故障。没有睡眠唤醒，故不应拿旧启动的休眠报错解释本次故障。

能够切换 VT 并产生错误输出，表明当时至少部分内核路径仍运行；不证明内核完全健康，也不证明之后没有局部或全局卡死。恢复 LVM 映射不等于恢复 ext4 journal 和已失败应用。

## 下次取证

先在正常系统安装并人工验证 F9/F10 的 RAM 救援入口。故障时先 rescue status、rescue log，再决定是否 rescue verify/refresh。RAM 日志重启即失，重启前拍照或保存到独立介质；不要依赖失联盘上的 /var/log。不要对宿主仍挂载的卷执行修复型 fsck。

旁边日志文件是本机诊断数据，可能包含本机路径、设备身份与应用日志，未经筛选不应直接公开。
