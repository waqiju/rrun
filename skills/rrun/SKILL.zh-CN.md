---
name: rrun
description: >-
  用 rrun CLI 在远程机器上执行脚本、传输文件（Windows 走 PowerShell，macOS/Linux 走 bash，
  Python 锁定 3.12）。适用于：在远程主机上执行命令或脚本、初始化远端 Python、向远端推拉文件、
  以及 ssh 引号/转义/中文编码出问题的时候。凭据只存在于磁盘上的 machines.json —— 绝不读进对话。
license: MIT
compatibility: 控制端为 Linux/macOS（Windows 走 WSL），Python >= 3.10，另需 ssh 和 sshpass。
metadata:
  homepage: https://github.com/waqiju/rrun
---

# rrun —— 为 agent 而生的 SSH 远程执行

[English](SKILL.md)

[rrun](https://github.com/waqiju/rrun) 把**本地**脚本经 ssh stdin 管道送到远端执行——
绝不走命令行参数——让引号、转义和中文/UTF-8 内容在 `bash → ssh → cmd/powershell`
多层旅途中完好无损。可以理解为 `ssh host < script`，外加机器清单、原子文件传输、
以及一键铺好的远端 Python 3.12。

给 agent 的契约：**你写文件，rrun 负责送达并执行，你读输出和退出码。**
没有引号谜题，也永远没有密码进入你的上下文。

## 凭据纪律（不可违反）

- **绝不读取、打印、打开** `machines.json` 或任何可能存放凭据的文件：
  `~/.rrun/machines.json`、`~/.rrun/machines.d/*.json`、`./machines.json`、
  `$RRUN_CONFIG` 指向的文件。`rrun machines` 会**脱敏**列出清单——那是你唯一的视图。
- **绝不让用户把密码贴进对话**。机器缺失或认证失败（退出码 255）时，
  请用户在**自己的终端**里跑 `rrun config add`（交互向导）或 `rrun config edit`，然后你再重试。
- 也不要在你写的脚本里内嵌密码、token 等秘密。注意：内联 `-c` 载荷会归档到 `~/.rrun/drops/`。

## 引导检查

1. `command -v rrun` —— 不存在则 `pipx install rrun-cli`（或 `pip install rrun-cli`；
   PyPI 发行名是 `rrun-cli`，装好的命令是 `rrun`）。控制端还需要 `ssh` + `sshpass`
   （`sudo apt install sshpass`；macOS：`brew install hudochenkov/sshpass/sshpass`）。
2. `rrun machines` —— 脱敏列出合并后的机器清单（含来源）。目标主机不在其中就停下，
   把配置步骤交还给用户（见上）。
3. `rrun doctor <host>` —— 在依赖之前先检查 ssh + 认证 + 远端 python。

## 核心工作流：exec

```bash
# 1. 在本地写脚本（UTF-8，中文随便用）——比如 temp/ 目录下或 mktemp。
# 2. 远端执行。语言按扩展名推断（.py/.ps1/.sh），否则按远端 OS
#    （Windows = powershell，其他 = bash）。
rrun exec <host> script.py
rrun exec <host> script.py arg1 arg2        # 参数：仅 ASCII，强制校验
rrun exec <host> -c 'Write-Output hello'    # 短小的内联载荷
rrun exec <host> script.sh --timeout 600    # 默认无超时；超时退出码 124
```

- **远端 stdout/stderr 原样回传；退出码原样透传**（0–254 是远端脚本自己的）。
  其余退出码：`255` = ssh 传输层/认证失败（不要盲目重试——先 `rrun doctor <host>`），
  `124` = 本地 `--timeout` 超时，`3` = push/pull 完整性校验不符。
- **中文和特殊字符绝不走命令行参数**——写进脚本内容，或写成 JSON 文件让脚本去读。
- `--workdir` / `--env K=V` 存在，但 **Windows + python 不支持**（设计如此）。
- `-q` 隐藏 `[remote-exec]` 信息行，stdout 保持干净可管道。
- 复杂任务优先用 python：远端跑统一的 **Python 3.12** venv。没有的话
  `rrun setup <host>` 一键铺好（幂等、非侵入；远端全程不访问外网）。
  装包：`rrun pip <host> -- install requests`。

## 文件传输

```bash
rrun push <host> ./local.bin /remote/path   # 原子：临时文件 + sha256 校验 + rename
rrun pull <host> /remote/path ./local.bin   # 自动建父目录；~ 在远端展开
rrun pull <host> /remote/path - | tar xz    # '-' 表示流到 stdout / 从 stdin 读
```

仅限单文件——目录会被拒绝（tar 走管道是预留的扩展方向）。默认覆写，且安全（原子 rename）。

## 故障排查

| 症状 | 动作 |
|---|---|
| 退出码 255 | `rrun doctor <host>`；认证问题 → 用户自己跑 `rrun config edit` |
| 被杀/超时的传输之后一切挂起 | `rrun close <host>`（关闭卡死的 mux 主连接）再重试 |
| 远端报 "python 3.12 not found" | `rrun setup <host>` |
| 不确定哪份清单在生效 | `rrun config`（来源链诊断） |
| 之前跑过什么 | `~/.rrun/log/remote-exec.jsonl`（审计日志，不记录任何秘密） |

## 深度参考

- README（安装、machines.json 字段、全部子命令）：
  https://github.com/waqiju/rrun/blob/main/README.zh-CN.md
- 踩坑约定与实现内幕（编码/转义规则表）：
  https://github.com/waqiju/rrun/blob/main/docs/remote-exec-conventions.zh-CN.md
- push/pull 传输设计：https://github.com/waqiju/rrun/blob/main/docs/push-pull-design.zh-CN.md
