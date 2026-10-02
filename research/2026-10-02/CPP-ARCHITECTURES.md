# C++ 运行时的指令集验证

本轮将完整 C++ 运行时交叉编译为 AArch64 和 RISC-V LP64D ELF，并在 QEMU user mode 中实际执行。两个架构均通过 37 项 admission 用例、13 项控制器配置及表结构用例，以及 5 组与 Python 逐字比较的规范 JSON 向量。测试前后全部运行时源码哈希一致。

| 验证内容 | AArch64 | RISC-V 64 |
|---|---|---|
| 完整运行时编译、链接 | 通过 | 通过 |
| `guard-runtime --version` 实际执行 | 通过 | 通过 |
| 持有 FD、diskseq、身份、布局、截止时间、owner epoch、并行拒绝等 admission 用例 | 37/37 | 37/37 |
| 配置路径、命名空间、整数溢出、DM 表及规范化边界 | 13/13 | 13/13 |
| Python 规范 JSON：Unicode、浮点、极值及身份记录 | 5/5 | 5/5 |
| 本轮外架构 Linux 内核热拔插恢复 | 未测 | 未测 |

这些结果证明完整源代码可在两种 ABI 上构建，且相关用户态路径可以实际运行；不把 QEMU user mode 等同于外架构内核、USB 驱动、引导链和完整恢复验证。此前 Python ARM64 内核实验也不计为本轮 C++ 内核验收。完整 `core_test` 的自执行子进程用例需要外架构执行入口；本轮没有注册主机 `binfmt_misc`，因此仅在外架构执行其规范 JSON 模式，完整核心资源所有权测试由本机原生测试覆盖。

工具来自已有用户目录中的 Ubuntu GCC 13.3.0 交叉编译器和 QEMU。仅补充解包匹配版本的 `libssl-dev` ARM64/RISC-V 包，使用 Ubuntu archive keyring 验证缓存的 InRelease 签名、对应 Packages 索引 SHA-256 和包 SHA-256；未使用 apt 安装、未改变主机加载器或内核设置。包版本与哈希保存在结果 JSON 中。

交叉测试为避免再引入外架构 C++ 共享库安装，采用 `-static-libstdc++ -static-libgcc`；libc 和 OpenSSL 仍动态链接。这不是生产 x86_64 包的链接策略，也不用于和生产包比较体积或常驻内存。

复现命令（工具目录需先准备好相同包）：

```sh
python3 lab/cpp_architecture_probe.py \
  --tools "$HOME/.local/share/ram-rescue-abi-tools" \
  --output lab/work/cpp-architecture-recheck
```

完整可发布结果：[cpp-architecture-results.json](cpp-architecture-results.json)。原始命令及标准输出保存在 `lab/work/cpp-architecture-1002/commands.jsonl`，原始报告位于同目录的 `report.json`；公开结果保留原始报告 SHA-256，工具及仓库绝对路径替换为 `${TOOLS}` / `${REPO}`。
