# Roadmap

**[English](ROADMAP.md)**

> rrun 当前范围的功能已经完备。本文件记录**已讨论并达成共识、但未排期**的设计——
> 写下来是为了让未来的迭代从结论出发，而不是从头再来。（记录于 2026-09。）

## Secret 管理（已达成共识的设计）

### 为什么

- **让秘密离开可能躺在 repo 目录树里的清单文件。** `machines.d` 软链模式让项目可以
  自带机器清单——但只要清单里是明文密码，凭据就物理上躺在工作树里（误提交、云同步）。
  迁移之后，清单的敏感度从「凭据级」降为「拓扑级」。
- **深化核心承诺**——凭据永不进入 agent 上下文——从 ssh 机器密码推广到**一切服务
  凭据**（P4、Jenkins、MySQL、Redis……），不再让服务密码以明文散落在 shell rc 和
  各工具的配置文件里。
- 一个 store、一套命令、一份纪律——而不是另起一个工具。

### 已拍板的决策

1. **`password_ref` 字段 → 内置 store**，位于 `~/.rrun/secrets.json`（权限 600）。
   - store **只有一个家**（`RRUN_SECRETS` 环境变量覆盖，供测试用）——刻意**不做**
     machines 那样的多来源链：秘密不该有任何通往 repo 的路径。
   - 只认显式引用，不做约定式 fallback。
   - 同一台机器同时写了 `password` 和 `password_ref` → 直接报错，不做静默优先级。
   - 命名空间约定：机器密码用 `machine/<name>`；服务秘密同样按命名空间组织
     （`mysql/prod-ro`、`p4/depot`、`jenkins/ci`、`redis/m101`）。

2. **`password_command` helper**——git credential-helper 式委托：一个 argv 数组
   （绝不接受 shell 字符串），其 stdout 即密码。
   - 一个机制覆盖 `pass`、1Password CLI、`sops`、`age`、OS keychain……
   - **不内置加密/解密。** 静态加密外包给 helper 生态。理由：加密的难点不在算法而在
     key 管理——passphrase 会杀死非交互的 agent/CI 流程（那是 rrun 的立身之本）；
     key 文件放在 store 旁边并不比 mode 600 强多少；key 放环境变量里同用户进程都能
     读到。ssh 的「未加密私钥 + 文件权限」是业界接受的先例，且 rrun 保持零依赖。
   - **不做 `password_file` 字段**——已被 helper 覆盖（`["cat", path]`）。

3. **`rrun secret` 命令族**
   - `set` / `rm`——`set` 交互式读取（getpass），绝不从 argv 收值，不进 shell 历史。
   - `list`——只列名字和描述，永不列值：这是 **agent 安全**的发现路径（agent 可以
     知道*有哪些*秘密可用，但永远看不到值）。
   - `show`——给人类打印明文；**要求 TTY**，让它保持为人类的逃生舱，而不是秘密流进
     agent 上下文的意外口子。
   - `export`——输出可 eval 的形式供人类 shell 使用
     （`eval "$(rrun secret export mysql/prod-ro)"`）。
   - `run --env VAR=ref [--env ...] -- cmd`——把引用解析进子进程环境变量：值不过
     argv（`ps` 不可见）、不过上下文、不过日志。各服务的 env 约定（`MYSQL_PWD`、
     `REDISCLI_AUTH`、`P4PASSWD`、`JENKINS_API_TOKEN`……）零 per-service 代码全覆盖。

4. **`rrun secret migrate`**——把清单里的明文密码一键搬进 store。
   - 默认 dry-run，`--apply` 才执行。
   - 扫所有清单来源；软链感知——改写的是**真实文件**（对 repo 内清单这正是期望效果），
     并在报告中明示。
   - 幂等、留 `.bak` 备份、跳过密钥认证的机器、拒绝损坏的 JSON。

5. **兼容性**：内联 `password` 永久支持。文档叙事从「明文是诚实的权衡」演进为
   「明文可用，store 是更进一步」——不打 deprecation 警告。

### 分期

- **v0.4.0——核心**：store + `password_ref` + `secret` 命令族 + `secret migrate`
  + SKILL.md 凭据纪律更新 + README Security/FAQ 叙事演进。
- **v0.5.0——生态**：`password_command` helper + `exec --secret-env VAR=ref`
  （把 store 里的值经 ssh 管道注入**远端**进程 env——补上今天 `--env KEY=VAL` 的值
  暴露在本地 argv 的洞）。
- **v0.6.0+——硬化，只在有真实需求时做**：内置静态加密、secret 使用审计（只记名字）、
  轮换辅助。

### 刻意留到实施期再定的细节

- `secrets.json` 的确切 schema（基线：`value` + `description` + `rotated_at`）。
- 报错文案、迁移报告格式。
