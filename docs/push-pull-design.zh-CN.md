# rrun push / pull 设计稿（v0.3.0）

**[English](push-pull-design.md)**

文件传输功能的设计记录。历史上在 v0.2.0 规划中定为「值得单独设计」的候选，v0.3.0 实现。
本文含实机调试沉淀的 **OpenSSH-Windows stdio 陷阱**，后续动传输协议前必读。

## 设计原则（从 rrun 既有基因推导）

1. **管道即协议**：数据走 ssh stdin/stdout 管道，永不走命令行。不引入 scp/sftp 子进程
   （scp 已被 OpenSSH 弃用；sftp 把路径放回命令行，编码地狱回归）。
2. **静态脚本 + 带外参数**：远端脚本不含未编码的用户数据；路径/哈希经 argv（POSIX）
   或 base64 内嵌（Windows）传入。**注入与编码问题从构造上免疫**。
3. **原子替换**：永远写 `<dest>.rrun-tmp-<token>` → 校验 → rename。覆写安全，
   因此**不需要 `--force`**。
4. **哈希即收据**：两端各算 sha256；远端 rename 前校验，不符删临时文件 exit 3。
5. **stdout 只载货**：`rrun pull host path - | tar xz` 必须无损；状态全走 stderr。

## CLI 形态

```
rrun push <host> <local> <remote>      # 文件 → 文件
rrun push <host> - <remote> < data     # stdin 推流
rrun pull <host> <remote> <local>      # 文件 → 文件
rrun pull <host> <remote> - > data     # 拉流到 stdout
```

- **host 在前**：与 exec/setup/pip/doctor 一致，拒绝 scp 的 `user@host:path` 语法。
- **目录语义沿用 cp**：目标是已存在目录或以 `/` 结尾 → 保留源 basename。
- **flags 与 exec 对齐**：`--timeout`、`--no-mux`、`-q`。
- **默认覆写**（原子 rename 兜底）；**自动创建父目录**（ansible copy 语义）。
- `~` 在远端脚本内展开（argv 传入的路径不会被 shell 展开）。

## 传输协议（最终实现）

### POSIX

```
push: 文件 --裸二进制--> ssh stdin --> 静态 bash 接收器(argv 传参) --> tmp --> sha256 --> mv
pull: 静态 bash 发送器 --裸二进制--> ssh stdout --> 本地 tmp --> sha256 --> os.replace
```

EOF 收尾，回执 `RRUN <size> <sha256>` / `RRUN-ERROR <msg>` 走 stderr。

### Windows（实机调试后的修订版）

```
push: 文件 --分块 base64 内嵌--> N 次 -Command - 单行调用 --> tmp 逐块追加 --> sha256 --> rename
pull: 单条 -EncodedCommand --> 裸二进制 stdout 流 --> 本地 tmp --> sha256 --> os.replace
```

- **push ≤512KB：单次调用成形**（resolve → 写 → 校验 → rename → 回执）；
  更大则 init（resolve + 清理陈旧 tmp + base64 回传解析后路径）→ append×N → finalize。
- 所有路径与数据经 base64 内嵌进单行 PS 命令——**线上全程纯 ASCII，CJK/特殊字符免疫**。
- 实测吞吐 ~2MB/s（ mux 复用下每次调用 ~0.3s）——bot 场景的文件量级完全够用。

### 为什么是这套看起来绕的方案——OpenSSH-Windows stdio 陷阱（9.5p1 实测）

调试过程中否定了三个「显然更优」的方案，记录备查：

1. **`-EncodedCommand` + stdin 流式推送（ReadLine 或裸 Read）**：间歇挂起。
   根因：sshd-win32 的 stdin 泵对「进程启动后到达的数据 + 需要流控（>~64KB 在途）」
   投递不可靠——数据写在管道里，远端 PowerShell 却永久阻塞在读上。32KB 以下
   （启动窗口内到达、无需流控）则稳定。
2. **RDY 握手（等远端就绪再发）**：更糟——必然落入「启动后到达」分支，100% 挂。
3. **延迟 EOF / 小块慢发**：无改善。

而 **`-Command -` 通道（exec 同款）承受 1MB 级单行载荷稳定**（PowerShell 宿主的
stdin 读取代码路径不同，且 exec 生产环境每日在用）。故 Windows push 收敛为
exec 通道的分块追加。

**另一个重要发现——mux 主连接 wedge**：超时被杀的 ssh 客户端会把该 host 的
ControlMaster 主连接打入 wedge 状态，之后**所有**复用该连接的操作（包括 exec）
全部挂起，直至 ControlPersist（600s）过期。传输层对策：超时即主动
`close_mux` 自愈（`_heal_mux`）。这也解释了调试中期「什么都挂」的假象——
`rrun close <host>` 后一切恢复。

### Windows pull 不受 stdin 泵影响

stdout 方向（远端→本地）无此缺陷：`[Console]::OpenStandardOutput()` 裸二进制
直写，1MB 随机数据 10/10 sha256 一致。注意必须 `$ProgressPreference='SilentlyContinue'`，
否则 stderr 混入 CLIXML 进度噪声污染协议通道。

## 完整性 & 退出码

- 退出码：**0** 成功 / **1** 远端失败 / **2** 本地用法错误 / **3** 完整性校验不符 /
  **255** 传输层 / **124** 本地超时。
- 审计：同一个 `~/.rrun/log/remote-exec.jsonl`，`op: push|pull`、size、sha256、verified、duration。

## 明确的非目标（v0.3.0 不做）

| 不做 | 理由 |
|---|---|
| 目录递归 `-r` | **已确认推迟**。扩展点已留好：目录 = tar 流走同一管道（POSIX 直推；Windows 侧可复用分块通道传 tar 包再远端展开，Win10+ 自带 bsdtar）。等真实需求再开 |
| rsync 式增量/续传 | rsync 的本职，不重复发明 |
| 权限/时间戳保留 | v1 只传内容 |
| 进度条 | bot 工具，stderr 一行摘要足够 |
| 多机并发 / `--stream` | A4/A5 候选，本轮明确排除 |

## 改动面 & 验证

- 新增 `src/rrun/transfer.py`，`__main__.py` 接线；复用 `_ssh_args`/`_ssh_run`/registry，零新依赖。
- 单测：命令组装（单行/ASCII/注入安全）、POSIX 脚本 argv 往返、路径/`~` 语义、
  回执/CLIXML 噪声解析。
- CI e2e（sshd 容器，POSIX 路径）：文本、CJK 文件名、随机二进制 sha256 比对、
  自动建目录、原子覆写、stdin/stdout 管道。
- Windows/Mac 实机冒烟：1MB×10 稳定性、5MB 分块、CJK 路径、目录目标、错误路径。
