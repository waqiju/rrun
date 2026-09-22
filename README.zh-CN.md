# rrun

[![PyPI](https://img.shields.io/pypi/v/rrun-cli)](https://pypi.org/project/rrun-cli/)
[![Python](https://img.shields.io/pypi/pyversions/rrun-cli)](https://pypi.org/project/rrun-cli/)
[![License](https://img.shields.io/pypi/l/rrun-cli)](https://github.com/waqiju/rrun/blob/main/LICENSE)
[![publish](https://github.com/waqiju/rrun/actions/workflows/publish.yml/badge.svg)](https://github.com/waqiju/rrun/actions/workflows/publish.yml)

**[English](README.md)**

通过 SSH 在远程机器上执行本地脚本——**一律走 stdin 管道，绝不走命令行参数**——让引号、转义和
中文/UTF-8 编码在 `bash → ssh → cmd/powershell` 的多层旅途中完好无损。

- 支持 python / powershell / bash，按文件扩展名或远端 OS 自动推断
- 远端 stdout/stderr 原样回传；**退出码原样透传**，管道和 CI 直接可用
- ssh ControlMaster 连接复用：首连 ~0.5s，复用 ~0.02s
- `rrun setup` 一键初始化**远端统一 Python 3.12 venv**——远端无需访问外网
- 零第三方 Python 依赖

## 安装

```bash
pipx install rrun-cli    # 提供 `rrun` 命令（附带 `remote-machine` 别名）
# 或：pip install rrun-cli
```

> PyPI 发行名是 `rrun-cli`（`rrun` 因与 `run` 易混淆被 PyPI 列入禁用名单）；
> 安装后得到的命令就是 `rrun`。

**控制端要求**：Linux 或 macOS（Windows 请走 WSL），Python ≥ 3.10，以及 `ssh` + `sshpass`：

```bash
sudo apt install sshpass                        # Debian/Ubuntu
brew install hudochenkov/sshpass/sshpass        # macOS（sshpass 不在 homebrew-core）
```

**远端要求**：OpenSSH 服务（密码或密钥认证均可）。Windows 远端经 PowerShell 执行，Mac/Linux 远端经 bash 执行。

## 快速上手

生成机器清单、填入你的机器、验证：

```bash
rrun config init                         # 用内置模板创建 ~/.rrun/machines.json（权限 600）
rrun config edit                         # 用 $EDITOR 打开——把 my-* 示例条目换成你的机器
# 或： rrun config add                   # 交互式向导，追加一台机器

rrun machines                            # 确认机器清单已生效（脱敏显示）
printf 'Write-Output "hello 中文"\n' > demo.ps1
rrun exec my-win-box demo.ps1            # 按 .ps1 推断为 powershell
```

核心循环就这么多：本地写脚本 → 远端执行 → 拿回输出和退出码。

## 为什么

从 POSIX shell 在远程 Windows 上执行命令是个雷区：sshd 把你落进 GBK 代码页的 cmd，引号和 `$`
被沿途三层 shell 中的某层吞掉，任何非 ASCII 参数都会变成乱码。rrun 的规则：

- 脚本内容（UTF-8，中文随便用）一律走 **stdin**，绝不走命令行；
- PowerShell 载荷被 base64 包进**单行纯 ASCII wrapper**（`powershell -Command -` 按行执行 stdin，
  多行输入会静默失败），远端解码后以 ScriptBlock 调用；
- 命令行参数只放行 ASCII，安全透传（`sys.argv` / `$@` / `$args`）。更复杂的内容写进脚本或 JSON 文件。

完整的踩坑约定与实现内幕：[docs/remote-exec-conventions.zh-CN.md](docs/remote-exec-conventions.zh-CN.md)
（[English](docs/remote-exec-conventions.md)）。

## 子命令

| 命令 | 用途 |
|---|---|
| `rrun exec <host> <script\|-c ...>` | 在远端执行本地脚本 / 内联内容 |
| `rrun machines [--json]` | 列出合并后的机器（脱敏，含来源） |
| `rrun config [init\|add\|edit]` | 管理用户级机器清单（`init` 按内置模板生成、`add` 以向导/旗帜追加、`edit` 用 `$EDITOR` 打开）；裸 `config` 诊断来源链 |
| `rrun setup <host\|--all> [--force]` | 初始化远端统一 Python 3.12 venv（幂等） |
| `rrun pip <host> -- list` | 在远端统一 venv 中执行 pip |
| `rrun doctor <host\|--all>` | 健康检查：ssh + 认证 + 远端 python（`--all` 会向所有机器发起真实连接） |
| `rrun close [<host>\|--all]` | 关闭 ssh ControlMaster 复用连接 |

常用 `exec` 选项：`--lang bash|powershell|python`、`--workdir`/`--env K=V`（Windows+python 不支持）、`--timeout`、
`--python <path>`（跳过探测）、`--no-mux`、`-q`。

### 退出码

| 退出码 | 含义 |
|---|---|
| `0`–`254` | 远端脚本自身的退出码，原样透传 |
| `255` | ssh 传输层失败（连不上 / 认证失败 / 掉线） |
| `124` | 本地 `--timeout` 超时，本地 ssh 客户端已被杀 |

## 配置：machines.json

凭据放在本地 `machines.json` 文件里。`rrun config init` 会把内置模板写入 `~/.rrun/machines.json`（模板也可在 [src/rrun/machines.template.json](src/rrun/machines.template.json) 浏览）：

```json
{
  "defaults": { "windows": { "os": "Windows" } },
  "machines": [
    { "name": "my-win-box", "ip": "192.168.1.10", "os": "Windows",
      "user": "admin", "password": "secret" },
    { "name": "cloud-vm", "ip": "1.2.3.4", "port": 2222, "os": "Linux",
      "user": "root", "identity_file": "~/.ssh/id_ed25519" }
  ]
}
```

单机字段：`name`/`ip`/`user` 必填；`password`（明文，走 sshpass）或留空走**密钥认证**
（`identity_file` 可选——默认走 ssh key 链 / agent / `~/.ssh/config`，且启用 `BatchMode=yes`，
密钥不可用时快速失败而不是卡住提示）；`port`（默认 `22`）；`os`（`Windows` / `Mac` / `Linux`）；
可选 `hostname`（也可用于寻址）、`description`、`python`（显式远端解释器路径，跳过自动探测）。

来源链按机器名 merge，高优先级覆盖（全部可选，失败静默跳过）：

1. `$RRUN_CONFIG`（os.pathsep 分隔，可多文件）
2. `$REMOTE_MACHINE_CONFIG`（旧名，仍兼容）
3. `./machines.json`（当前工作目录）
4. `~/.rrun/machines.json`
5. `~/.rrun/machines.d/*.json`（按文件名排序——每份清单放一个文件/软链）
6. 旧位置兼容：`~/.remote-machine/machines.json`（POSIX）/ `C:\tools\remote-machine\machines.json`（Windows）

每个文件的 `defaults` 段（按 `os` 小写为键）在 merge 前应用。用 `rrun config` 诊断来源链，
`rrun machines` 查看机器（脱敏）。

## 远端 Python：统一 3.12 环境

执行 `python` 脚本时，rrun 要求远端存在 **3.12.x** 解释器，按序探测：

1. 统一 venv：`C:\tools\remote-machine\venv\Scripts\python.exe` / `~/.remote-machine/venv/bin/python`
2. standalone 基座：`...\python312\python.exe` / `~/.remote-machine/python312/bin/python3`
3. 存量装机：`C:\Python\Python312\python.exe` / PATH 上的 `python3.12`

全灭则跑 `rrun setup <host>`。它幂等且非侵入：机器缺少**能建 venv 的** Python 3.12 时——完全没装，
或解释器建不了 venv（Debian/Ubuntu 把 `ensurepip` 拆进了独立的 `python3.12-venv` 包）——
[python-build-standalone](https://github.com/astral-sh/python-build-standalone) 压缩包（Windows/macOS/Linux）
先下载到本机缓存（`~/.cache/rrun/`），再经 ssh stdin 推流到远端解压——零注册表、零 PATH 改动、零管理员权限，
远端全程不访问 GitHub。上次 setup 中途失败留下的半拉子 venv（python 能跑但缺 pip）会被自动检测并重建。
这些状态 `rrun doctor <host>` 都会预先报告。venv 内自带 `pip.ini`/`pip.conf`（不碰全局 pip 配置），并按包内置
`remote-requirements.txt` 安装标准依赖（可用 `~/.rrun/remote-requirements.txt` 覆盖）。

## 安全说明

- `machines.json` 存的是**明文密码**：保持本地存放、绝不提交入库（`rrun config init`/`add` 写用户清单时已自带 `600` 权限）；或把 `password` 留空改用密钥认证。
- rrun 经 `sshpass` 做密码认证；**也支持密钥认证**（password 留空即可，详见上文配置节）。
- 密钥认证以 `BatchMode=yes` 运行 ssh（无交互提示；密钥缺失/未授权时快速失败，不会把 stdin 里的脚本吃掉）。
- 审计日志绝不记录密码；`--env` 的值只以 key 的形式记录。

## 审计与本地状态

- 每次执行追加一行 JSON 到 `~/.rrun/log/remote-exec.jsonl`（host/lang/脚本 sha1/退出码/耗时）。
- 内联 `-c` 载荷归档在 `~/.rrun/drops/`，可复跑。
- 状态根目录默认 `~/.rrun/`，可用 `$RRUN_HOME` 覆盖。

## 升级 / 卸载

```bash
pipx upgrade rrun-cli
pipx uninstall rrun-cli
```

## 参与贡献

欢迎 issue 和 PR。开发环境：clone → `python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"` →
`pytest` + `ruff check .` → 改完用 `rrun machines` 冒烟。CI 跑单测、ruff 和基于真实 sshd 容器的端到端测试。
发版靠推 `vX.Y.Z` tag，CI 经 trusted publishing 自动发布到 PyPI。
见 [CHANGELOG.md](CHANGELOG.md)。

## License

[MIT](LICENSE)
