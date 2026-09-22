# 为什么 Windows+python 不支持 `--workdir`/`--env`

**[English](windows-python-workdir-env.md)**

决策记录。`--workdir`/`--env` 对 Windows+python **永久不支持**——`rrun exec` 抛出的
`ValueError` 是设计决定，不是待办缺口。请在脚本内自行设置（`os.chdir` / `os.environ`）。

## 支持矩阵

| | bash | powershell | python |
|---|---|---|---|
| POSIX | ✅ `cd`/`env` 前缀 | （默认不用） | ✅ `cd`/`env` 前缀 |
| Windows | （默认不用） | ✅ wrapper：`Set-Location` / `$env:` | ❌ **不支持（设计决定）** |

## 为什么偏偏缺这一格

`--workdir`/`--env` 需要一个 rrun 完全可控的注入层：

- **POSIX（任意语言）**：远端命令由 `sh` 解析（POSIX 保证），`cd X && env K=V ...`
  前缀永远合法。
- **Windows+powershell**：远端命令是固定 ASCII 常量（`powershell ... -Command -`），
  stdin 上的载荷是 **rrun 自己生成**的 PowerShell 语法 wrapper，`Set-Location` /
  `$env:` 想插就插。
- **Windows+python**：载荷就是用户脚本本身——没有可以注入 shell 语法的地方。剩下的
  唯一杠杆是远端命令行，但它由 sshd 默认 shell 解析：默认 `cmd.exe`，若服务端配置了
  `DefaultShell` 则是 PowerShell。`cd /d X & set K=V & python ...` 与
  `Set-Location X; $env:K='V'; python ...` 语法互斥，**不存在可移植的 shell 层注入点**。

## 比对的方案

### A. cmd 风格前缀 —— 否决：不可移植

`cd /d X & set K=V & python ...` 在 sshd `DefaultShell` 为 PowerShell 时直接坏掉，
且两个 shell 的引号/转义规则互不相同。用哪个 shell 是**服务端配置**，客户端无法
可靠探测。

### B. PowerShell wrapper 包住 python —— 否决：热路径代价 + 编码坑

在 PS wrapper 里设好位置/环境变量，再把脚本经 stdin 转发给 python。Windows
PowerShell 5.1 向原生进程管道输送的是重编码字符串——慢，且会损坏非 ASCII/二进制
数据。更糟的是**每次** python exec 都要多付一层解释器开销，而不只是用到
workdir/env 的那些。

### C. python preamble 注入 —— 否决：半成品语义等于埋坑

在载荷头部拼 `import os; os.chdir(...); os.environ[K]=V`。payload 通道完全可控，
所以**机械上可行**，但是：

- 解释器启动后才设的环境变量影响不了启动期变量（`PYTHONPATH`、`PYTHONUTF8`、
  `PYTHONHASHSEED`）——比 POSIX 的 `env` 前缀**静默地弱**；
- `<stdin>` 里每条 traceback 的行号都会偏移 preamble 的行数；
- 边界问题接踵而来（preamble 是否计入 `content_sha1`/审计？workdir 的 ASCII 规则？）
  ——单个都 trivial，但加在一起就是一个**语义按平台分叉的功能**。用户会在其上构建，
  然后才撞上这些缺口。

### D. 显式报错 + 用户侧绕行 —— 采纳

脚本开头写 `os.chdir()` / `os.environ` 只有两行，完全可见，且语义就是 Python 本身
的语义。用户成本 trivial；A–C 的成本是隐藏的失败模式。

POSIX+python 保留 `cd`/`env` 前缀（语义更强：启动期生效）。双机制并存是有意的——
用户可观测行为一致，只是实现不同。

## 附注

- ≤ 0.2.1 版本的报错是 "not supported **yet**"；"yet" 被移除，因为它暗示着
  "计划支持"，导致 review 时反复被提起。
- 顺带说明：Windows+python 的透传参数用的是 POSIX `shlex` 引号规则——对 rrun
  允许的纯 ASCII 简单参数无害，但也是让 Windows python 命令行保持最小化的又一个理由。
