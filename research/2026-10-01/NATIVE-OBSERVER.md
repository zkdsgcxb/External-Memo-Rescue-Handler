# C++ 只读观察器对照实验

## 结论与范围

C++ 原型在相同实际 DM 健康检查下，显示出较低的常驻内存和 CPU 开销。
它没有恢复事务、设备准入、挂载或超时接管功能，**不作为生产 Guard 替代品部署**。
原有 Python Guard 在整个实验中继续唯一负责每个映射的恢复。

比较双方都是只读观察器：精确 DM UUID、单 multipath target、底层 `dev_t`、
sysfs 真实路径、`diskseq`、当前路径 A 和任意 F 状态。每秒兜底检查，
100 ms 合并事件，持久加载 libdevmapper，不执行磁盘身份探测或 ioctl 路径探测。
当前路径必须 A 的条件比生产健康循环严格，因此不宣称完整行为等价。

## 环境和方法

- 完整 Ubuntu，Linux 7.0.0-34-generic，x86-64 QEMU/KVM，2 vCPU、3 GiB RAM。
- 已登记 ext4 数据盘的稳定 multipath 映射；根盘和另一个 FAT32 数据盘仍受原 Guard 保护。
- 同一个 VM 中顺序运行；C++ / Python 次序为 C-P、P-C、C-P，双方各三次。
- 每次预热 5 秒、稳态约 20 秒，100 ms 名义采样；使用实际时间差计算 CPU。
- CPU 分母是一个逻辑核心，100% 表示一个核持续占满。
- 观察器各自独立 systemd cgroup；原 Guard、采样进程、udev、内核工作线程均不计入。
- 同时记录 cgroup `usage_usec` / `memory.current` / `MemoryPeak` 和进程 RSS、PSS、私有页。
- 原始样本、观察结果、二进制和 Python 模块 SHA256 都保留在机器可读报告中。

## 稳态实测

| 观察器 | 重复 | CPU 均值，单核 % | 约 100 ms 窗口最高 % | PSS 均值 MiB | RSS 均值 MiB | cgroup 内存均值 MiB |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| C++ | 1 | 0.01633 | 0.27973 | 1.743 | 4.648 | 0.641 |
| Python | 1 | 0.04776 | 0.66776 | 10.234 | 15.174 | 7.599 |
| Python | 2 | 0.04076 | 1.02882 | 9.767 | 14.664 | 6.996 |
| C++ | 2 | 0.02033 | 0.41106 | 1.684 | 4.520 | 0.746 |
| C++ | 3 | 0.01890 | 0.44840 | 1.759 | 4.652 | 0.496 |
| Python | 3 | 0.03922 | 0.63323 | 9.832 | 14.754 | 6.996 |

按三次实际稳态时长合并：C++ **0.01852%**，Python **0.04258%**。
C++ 在 59.67 秒内消耗 11.05 ms CPU，Python 在 59.74 秒内消耗 25.44 ms。
绝对差异约为每秒 0.24 ms CPU；在完整控制器设计中，是否值得增加另一套实现，
还必须考虑设备准入、事务日志、崩溃接管和维护成本。

这些是观察器间的比较。不能把 C++ 数字与包含恢复 worker 的完整 Guard 数据
直接相减，然后称为“全部功能迁移后的节省”。

## 峰值、内存与启动解释

- 窗口最高值是约 100 ms 的 CPU 时间平均，不能解释为某个时刻没有超过该占用。
- 三次 cgroup `MemoryPeak`：C++ 671744 / 786432 / 520192 字节；
  Python 7979008 / 7335936 / 7335936 字节。
- cgroup 文件页记账受首次加载者和缓存归属影响，因此本实验 cgroup 内存低于
  进程 PSS 并不矛盾；两者不能相加，也不应只挑较小数字报告。
- 报告单列 exec 到首样本的 CPU：C++ 1316–1521 μs，Python 6644–10712 μs。
  首样本在 exec 后约 6–12 ms，可能早于 Python 完成初始化。
  **这不是完整启动成本，也不是启动峰值。** 5 秒预热明确排除在稳态均值外。
- 本轮没有测观察器的恢复 CPU，因为原型没有恢复功能。完整 Guard 的故障、
  事件风暴和限额实验应使用独立的完整控制器报告。

## 验证与证据

构建和解析器检查通过；CLI 拒绝缺失身份、无效 diskseq、无界/非数值运行时长。
专门的 4 项 Python 测试包含真实编译后的 C++ 测试程序，以及计量公式验证。
ARM64 和 RISC-V64 解析器已交叉编译并通过 QEMU user-mode 执行；这仅证明该
用户态解析器可以在对应 ISA 运行，不证明其完整块设备行为或恢复已验证。

最终测量报告：
`lab/work/native-1001-043125-533469/report.json`

SHA256：`3f951e6bebfb8e95c2578cc1a6f37d2609beed00ecda20b201c5c93b1050733d`

报告 `passed=true`，六次观察均健康，各输出 25 次状态；VM 正常关机。
只读基础镜像哈希未变，ext4/FAT32 数据镜像离线只读 fsck 均返回 0。
测量所用源文件版本以报告中的 payload SHA256 为准。

额外只读判定与热拔插验收：
`lab/work/native-1001-044113-628528/report.json`

SHA256：`803ac83066e4d2c05b892aeb1aca3443dfc3ae1ec3a16328bd2c0874fccdd000`

- 实际运行中错误 map UUID 返回 `control-uncertain` / exit 1；
  错误 `diskseq` 返回 `path-unavailable` / exit 2。
- QMP 拔插实际间隔 **0.304 秒**，C++ 和 Python 观察器均由 ready 变为 unavailable。
- 第一次检测设备已消失时，DM 仍报告原路径 A、没有 F；下一次才观测到 F。
  这验证了设备实例检查与 DM 状态检查不能简单互相替代。
- 唯一 Python Guard 成功恢复 ext4；原进程/文件描述符继续使用，最长一次工作负载
  等待约 **2.013 秒**，ext4/FAT32 持久数据核对均通过。
- 观察器保持原 `diskseq`，因此正确恢复后仍报告旧实例失效并以 exit 2 退出。
  它不会把检测结果误当成准入授权。恢复由已有 Guard 完成。
- 此场仅验证正确性，和另一正确性 VM 同时运行；没有将其资源或时延用于性能对比。

复现：

```bash
python3 guard/native/build.py --output lab/work/native-build
python3 -m unittest discover -s lab/tests -p 'test_native_observer.py' -v
python3 guard/native/vm_probe.py
python3 guard/native/vm_probe.py --fault-only
```

代码与运行约束见 `guard/native/README.md`。所有镜像均为 lab 下的一次性文件，
无宿主设备透传、无宿主服务修改、无宿主实盘拔插。
