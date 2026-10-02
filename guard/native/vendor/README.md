本目录固定依赖 nlohmann/json **v3.12.0**，用于解析现有登记记录和保持 Python 事务日志兼容。头文件直接取自上游发布标签，未修改；许可为 MIT，见 `LICENSE.nlohmann-json`。

- 来源：https://github.com/nlohmann/json/tree/v3.12.0
- 头文件：https://raw.githubusercontent.com/nlohmann/json/v3.12.0/single_include/nlohmann/json.hpp
- `json.hpp` SHA-256：`aaf127c04cb31c406e5b04a63f1ae89369fccde6d8fa7cdda1ed4f32dfc5de63`
- `LICENSE.nlohmann-json` SHA-256：`46a65cffd1ea955132d95a8dd921640714a8d6b537d2e4e482d31145ae95b603`

构建不需要联网下载依赖。C++ 运行时动态使用发行版的 libdevmapper、OpenSSL libcrypto、libstdc++ 和 libc；构建清单记录实际 ELF 与共享库散列，完整依赖被放入救援 tmpfs。
