# External-Memo-Rescue-Handler

**Linux USB 存储短暂断联恢复工具 · v0.0.1-beta**

[English](README.en.md) · [安装与使用](docs/USAGE.md) · [支持范围](docs/SUPPORT.md) · [架构](ARCHITECTURE.md) · [安全](SECURITY.md) · [参与开发](CONTRIBUTING.md)

通过稳定的 DM multipath 映射和 C++17 后台控制器，在原 USB 盘短暂断联、重新枚举后核验身份并重新接入，让等待中的 I/O 有机会继续。恢复期间读写可能阻塞；本工具不能撤销已返回应用的 I/O 错误或修复损坏的文件系统。

```text
应用 → ext4 → 稳定 DM 映射 → 原 USB 数据分区
                 ↑
          C++ 身份核验与重接
```

## 首版提供什么

- 普通 USB 数据分区的只读预检、确认登记、首次建图和显式启用。
- 原盘身份核验、唯一恢复执行者、恢复事务记录与死亡接管。
- 临时安全停止、持久停用、开机启用、移除配置和卸载检查。
- RAM 工具环境、状态查看及本地脱敏诊断。
- Debian 安装包、可复现构建入口、普通回归和隔离 QEMU 故障实验。

**安装包不启动保护；登记配置也不启用。** 只有明确执行启用命令才会修改映射并启动控制器。候选流程的 `save_candidate_only` 资格不能授权启用。

这是 Beta 发布。公开数据盘入口限定 Ubuntu 24.04 / x86_64 / ext4 / 单路径 USB 数据分区，并核对已验收的精确内核和软件组合。未知组合会拒绝启用，详见 [支持矩阵](docs/SUPPORT.md)。现有根盘、LVM 根保护及 F9/F10 RAM 救援保留为[高级实验功能](guard/README.md)。

## 开始使用

从 [GitHub Releases](https://github.com/zkdsgcxb/External-Memo-Rescue-Handler/releases) 获取同一次发布的程序包、支持材料和校验清单。先按 [安装指南](docs/USAGE.md) 核对支持条件；不要直接套用历史实验里的安装命令。

统一入口是 `rescue-guard-admin device`。常用操作为：

```text
plan → enroll（停用配置）→ enable → status
                          ↓
                  stop / disable → remove → uninstall
```

`stop` 只停止本次运行；`disable` 同时撤销未来开机启用。忙设备不会被强行卸载或结束应用。原生事务、锁和 RAM 工具会保留到重启，卸载指南说明了何时需要先正常重启。

## 构建和检查

工具链安装在 Ubuntu；源码、构建和实验文件存放在 workspace。

```bash
git clone https://github.com/zkdsgcxb/External-Memo-Rescue-Handler.git
cd External-Memo-Rescue-Handler
mkdir -p lab/work
python3 -B guard/native/build_runtime.py --output lab/work/native
python3 -B -m unittest discover -s lab/tests
python3 -B -m unittest discover -s ram-rescue-demo/tests
```

需要 Python 3.12、C++17 编译器、binutils 和 OpenSSL 开发头文件。完整构建见 [原生运行时](guard/native/README.md)，隔离复现见 [实验室](lab/README.md)。构建和普通测试不安装宿主服务；VM 实验不透传真实块设备。

## 边界

- 不接管正在使用的裸分区，不格式化磁盘，不自动 fsck，不强制读写重挂。
- USB 元数据和文件系统标识用于防误接，不构成设备认证。
- RAM/chroot 与宿主共享内核，不是 root 安全沙箱。
- 内核死锁、永久底层 I/O 阻塞、掉电丢失的缓存，以及应用自己的超时，不在恢复保证内。
- VM 验收与实机证据分别记录。历史实机记录不自动适用于本次发布。

## 项目导航

| 路径 | 内容 |
| --- | --- |
| `guard/` | C++ 恢复核心、Python 冷管理、打包与启动集成 |
| `ram-rescue-demo/` | RAM 工具与高级救援终端 |
| `lab/` | 普通回归、一次性 QEMU 故障实验和公开结果 |
| `research/` | 按日期保留的历史设计与验收，不是当前安装指南 |
| `docs/` | 本次发布的用户文档与支持范围 |

自有代码使用 [0BSD](LICENSE)。第三方组件保持各自许可证，二进制发布附带其清单、许可文本与对应源码材料，见 [第三方说明](THIRD_PARTY.md)。
