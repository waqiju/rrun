# 远程执行约定与 rrun 实现内幕

**[English](remote-exec-conventions.md)**

在远程机器（尤其 Windows）上经 SSH 执行脚本的踩坑规则沉淀，以及 rrun 的实现方式。
日常用法见 [README](../README.zh-CN.md)。

**核心原则：含中文/特殊字符的内容绝不走命令行，一律走文件或 stdin 管道。**

SSH 到 Windows 默认落进 cmd、代码页 cp936/GBK，参数沿途经过最多三层 shell
（`bash → ssh → cmd/powershell`），每层都有自己的引号、转义、编码规则；
引号、`$`、`;`、`%VAR%` 各有坑。

## 规则表

| 规则 | 正确做法 | 反例（都踩过） |
|------|------|--------------|
| 中文参数 | **绝不走命令行**，写进 py/bat/json 文件（UTF-8）后执行 | `tool.exe --branch "【Testbed】Main->Dev"` 经 cmd 转码损坏 |
| 远程 Python | 用 `python -X utf8 -` 从 **stdin** 读脚本，绕开转义与编码 | `python -c "...中文..."` 必坏 |
| 远程执行器 | 优先用 `rrun exec`（自动管道+凭据+参数透传） | 手动 scp + ssh 反复重试 |
| PowerShell | `powershell -ExecutionPolicy Bypass -File x.ps1`（脚本文件 UTF-8），别在命令行写 PS 代码 | ssh 内联 `powershell -Command "$b=..."` 中 `$` 被本地 bash 吞掉 |
| cmd 分隔 | 顺序执行用 `&`（不是 `;`）；需要错误码就写 bat | `cmd; echo %ERRORLEVEL%` 中 `;` 被当成参数分隔 |
| bat 变量 | `set X=...& %X%` 同一行不展开（解析时已替换），要用则写 bat 文件 | `set P=...&& %P%` 报"不是内部或外部命令" |
| GUI 程序 | 必须 `subprocess.run()`（Python）或 `start /wait` 等退出码 | cmd 裸启动 GUI 程序立即返回，`%ERRORLEVEL%` 拿到的是启动码而非程序退出码 |

## rrun 实现内幕

### PowerShell 载荷

实测 `powershell -Command -` **按行**执行 stdin（多行 here-string 会静默失败）。
rrun 因此把脚本 base64 编码后内联进**单行纯 ASCII wrapper**，远端 UTF-8 解码后经
ScriptBlock 执行；控制台输出编码强制置 UTF-8，中文双向不乱码；透传参数在脚本内经 `$args` 访问。

### Python 载荷

`python -X utf8 -` 从 stdin 读程序。解释器由 `detect_remote_python` 解析：单次往返探测候选路径、
校验 `3.12.x`、缓存结果。候选顺序：

1. 统一 venv（`C:\tools\remote-machine\venv\Scripts\python.exe` / `~/.remote-machine/venv/bin/python`）
2. standalone 基座（`...\python312\python.exe` / `~/.remote-machine/python312/bin/python3`）
3. 存量装机（`C:\Python\Python312` / PATH 上的 `python3.12`）

全灭时 exec 热路径不做隐式安装，报错提示跑 `rrun setup`。
machines.json 的 `"python"` 字段或 `--python` 可跳过探测与版本校验。

### bash 载荷

`bash -s -- args...`，`--workdir`/`--env` 翻译成 `cd`/`env` 前缀。

### 传输层

- 命令行参数只允许 ASCII（python→`sys.argv`，bash→`$@`，powershell→`$args`）。
- ssh ControlMaster 连接复用（10 分钟）：首连 ~0.5s，复用 ~0.02s；`rrun close <host>|--all` 关闭。
- 退出码：远端脚本退出码原样透传；`255` = ssh 传输层错误；`124` = 本地 `--timeout` 超时。

## 环境初始化：`rrun setup`

远端统一 Python 环境的幂等初始化入口：

- 机器已有 3.12 解释器则直接用作 venv 基座；
- 没有则把 [python-build-standalone](https://github.com/astral-sh/python-build-standalone)
  压缩包下载到本机缓存（`~/.cache/rrun/`），经 ssh stdin 推流到远端解压——
  零注册表、零 PATH 改动、零管理员权限，远端全程不需要访问 GitHub；
- venv 内自带 `pip.ini`/`pip.conf`（镜像可在 `setup.py` 配置，不污染全局 pip 配置），
  按 `remote-requirements.txt` 安装标准依赖（包内置默认；`~/.rrun/remote-requirements.txt` 覆盖）
  并做 import 自检；
- `--force` 重建 venv，不动 python 本体。

venv 铺好后远程脚本**不再受 stdlib-only 约束**（默认含 requests）。
加依赖：改 `~/.rrun/remote-requirements.txt` → 重跑 `rrun setup <host>`（幂等补差额）——
或临时 `rrun pip <host> -- install xxx`。

## 审计

每次执行追加一行 JSON 到 `~/.rrun/log/remote-exec.jsonl`（host/lang/脚本 sha1/退出码/耗时）；
绝不记密码，`--env` 只记 key。内联 `-c` 载荷归档在 `~/.rrun/drops/`，可检查、可复跑。
