# 远程执行约定（rrun 踩坑与规则）

> 本文是 rrun 背后的运维约定沉淀：SSH 到 Windows（默认 shell 为 cmd、代码页 cp936/GBK）执行命令时，
> 中文参数/脚本经 `bash -> ssh -> cmd` 多层转码极易损坏；引号、`$`、`;`、`%VAR%` 各有坑。
> **核心原则：含中文/特殊字符的内容绝不走命令行，一律走文件或 stdin 管道。**

## 规则表

| 规则 | 内容 | 反例（踩过） |
|------|------|--------------|
| 中文参数 | **绝不走命令行**，写进 py/bat/json 文件（UTF-8）后执行 | `p4 integrate -b "【Testbed】Main->Dev"` 经 cmd 转码损坏 |
| 远程 Python | 用 `python -X utf8 -` 从 **stdin** 读脚本，绕开转义与编码 | `python -c "...中文..."` 必坏 |
| 远程执行器 | 优先用 `rrun exec`（见下），自动管道+凭据+参数透传 | 手动 scp + ssh 反复重试 |
| PowerShell | `powershell -ExecutionPolicy Bypass -File x.ps1`（脚本文件 UTF-8），别在命令行写 PS 代码 | ssh 内联 `powershell -Command "$b=..."` 中 `$` 被本地 bash 吞掉 |
| cmd 分隔 | 顺序执行用 `&`（不是 `;`）；需要错误码就写 bat | `cmd; echo %ERRORLEVEL%` 中 `;` 被当成参数分隔 |
| bat 变量 | `set X=...& %X%` 同一行不展开（解析时已替换），要用则写 bat 文件 | `set P4=...&& %P4%` 报"不是内部或外部命令" |
| GUI 程序 | Unity 等必须 `subprocess.run()`（Python）或 `start /wait` 等退出码 | cmd 裸启动 Unity 立即返回，`%ERRORLEVEL%` 拿到的是启动码 0 |
| P4 中文 | 分支名/描述一律 P4Python 脚本或 `p4 -x argfile`（UTF-8） | `p4 submit -d "中文描述"` 经 cmd 变乱码 |
| P4 charset | 含非 ASCII 的 utf8 文件操作一律显式 `-C utf8` | cp936 会话 resolve/submit 误报 tampered |

## rrun 用法

```bash
rrun machines                          # 列出可用机器（脱敏，含来源）
rrun config                            # 查看 machines.json 来源链解析
rrun exec pc_build temp/xxx.py         # lang 按扩展名推断
rrun exec pc_build temp/xxx.ps1        # Windows 默认 powershell
rrun exec mac_mini temp/xxx.sh
rrun exec pc_build -c "Get-Date"       # 内联（落盘 ~/.rrun/drops/ 留档）
rrun exec pc_build temp/xxx.py -- arg1 arg2  # ASCII 参数透传
rrun setup pc_build                    # 初始化统一 python 环境（幂等）
rrun setup --all                       # 批量初始化所有机器
rrun pip pc_build -- list              # 在统一 venv 中执行 pip
```

约束与说明：

- **PowerShell 载荷**：实测 `powershell -Command -` 按行执行 stdin（多行 here-string 会静默失败），
  故 rrun 把脚本 base64 编码后内联进**单行纯 ASCII wrapper**，远端 UTF-8 解码后经 ScriptBlock 执行；
  输出编码已置 UTF-8，中文双向不乱码；脚本内 `$args` 访问透传参数。
- 命令行参数只允许 ASCII（python→sys.argv，bash→$@，powershell→$args）；
  中文/特殊字符写进脚本内容或 JSON 配置文件。
- 退出码：远端脚本退出码原样透传；255 = ssh 传输层错误（连不上/掉线）；124 = 本地超时（--timeout）。
- ssh ControlMaster 连接复用（10 分钟），复用后每次 ~0.02s；`rrun close <host>|--all` 关闭。
- **python 环境（基线锁 3.12）**：exec 时按序探测并校验版本号 == 3.12.x——
  统一 venv（`C:\tools\remote-machine\venv\Scripts\python.exe` / `~/.remote-machine/venv/bin/python`）→
  standalone 基座（`...\python312\python.exe` / `~/.remote-machine/python312/bin/python3`）→
  存量 3.12（`C:\Python\Python312` / `python3.12`）；全灭则报错提示先跑 `setup`（热路径不做隐式安装）。
  也可在 machines.json 加 `"python"` 字段或用 `--python` 显式指定（跳过探测与版本校验）。
- **setup 子命令（统一环境的初始化入口，幂等）**：机器已有 3.12 则直接用作 venv 基座；
  没有则把 python-build-standalone 压缩包（本机缓存 `~/.cache/rrun/`）经 ssh stdin
  推送解压——零注册表、零 PATH、不改 python/py 指向，纯新增目录。随后建 venv、
  在 venv 内写 pip.ini/pip.conf（阿里云镜像源，不污染全局）、安装标准依赖
  （包内置 `remote-requirements.txt`，可被 `~/.rrun/remote-requirements.txt` 覆盖）并 import 自检。
  `--force` 重建 venv（不动 python 本体）。新机器接入、venv 损坏、补装依赖都重跑 `setup <host>` 即可。
- `--workdir` / `--env KEY=VAL` 仅 bash/powershell 支持（python 请在脚本内 os.chdir/os.environ）。
- 每次执行追加审计日志 `~/.rrun/log/remote-exec.jsonl`（host/lang/脚本 sha1/退出码/耗时；
  不记密码，env 只记 key）。

## 依赖第三方库的远程脚本

统一 venv 铺好后远程脚本**不受 stdlib-only 约束**（默认含 requests）。加新依赖的流程：

1. 把包名写进 `~/.rrun/remote-requirements.txt`（覆盖包内置默认清单）；
2. 重跑 `rrun setup <host>`（幂等，只补装差额）；
3. 临时 ad-hoc 装包：`rrun pip <host> -- install xxx`。
