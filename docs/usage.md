# clu 使用说明

> SSH 会话保持/管理 + Slurm 集群与普通服务器的 AI agent 能力层。
> 面向高校师生的单用户工具：Claude Code（MCP）与 `clu` CLI 共享同一 daemon。

---

## 1. 它解决什么问题

- **AI 直连集群**：在 Claude Code 里说"看看队列""抢张 A100""传数据集上去"，
  clu 经你自己的 SSH 通道完成，无需把密钥交给任何云服务
- **会话不丢**：交互式资源（salloc）跑在远端 tmux 里，断网/合盖不丢 allocation
- **连接复用**：本地 daemon 持有长连池，命令秒回、断线自动重连
- **留痕**：每次执行写 SQLite，跨会话可查"上次那个作业怎么样了"

## 2. 安装

```bash
# 推荐：uv（https://docs.astral.sh/uv/）
uv tool install clu-ssh          # 发布后可用

# 开发者：从源码
git clone https://github.com/QiandingHuang666/clu-ssh
cd clu-ssh && uv sync
alias clu='uv run --project /path/to/clu-ssh clu'
```

要求：macOS 或 Linux；Python ≥ 3.11；SSH **公钥认证**（不支持密码登录的连接）。

## 3. 五分钟上手

```bash
# ① 导入你现有的 ssh config（一次性副本，只导公钥可达条目）
clu connection import

# ② Slurm 集群的条目要标成 cluster（import 默认按 IP 启发式标 instance/local）
clu connection add gzu --host hpc.example.edu.cn --user you --port 22 --kind cluster
#    或者把导入的条目改 kind：编辑 ~/.config/clu/connections.toml

# ③ 拉起 daemon（此后任何命令都会自动 ensure，通常不用手动）
clu server ensure

# ④ 试试
clu connection check gzu          # 经连接池真连一次
clu exec gzu "squeue -u $USER | head"
```

## 4. 核心概念

| 概念 | 说明 |
|---|---|
| **daemon** | 本机常驻进程，持有 SSH 连接池、tmux 会话、转发与留痕。缺席时任意命令自动拉起（`clu server ensure` 模式），`clu server stop` 停止 |
| **连接** | `~/.config/clu/connections.toml` 中的一条远程主机，带 `kind`：`cluster`（Slurm，启用作业工具）/ `instance`（租用机）/ `local`（局域网） |
| **跳板** | 连接可设 `proxy` 指向另一连接（单级 ProxyJump），认证与隧道复用连接池 |
| **profile** | 学校/集群定制（公共资源目录、重操作名单、抢占分区），挂在连接上，代码零硬编码 |
| **留痕** | 每次执行写 `~/.local/share/clu/ledger.sqlite3`，`ledger_query` 按连接/关键词/作业号检索 |
| **known_hosts** | 首次连接记录主机密钥，之后严格校验，**变更即拒绝**（防中间人） |

## 5. CLI 参考

### 5.1 速查

```
clu server   ensure | status | stop
clu connection  import | add | remove | list | check
clu exec     LABEL COMMAND...
clu resources LABEL [过滤]
clu alloc    LABEL [资源]
clu attach   LABEL PURPOSE
clu jobs     LABEL [--states RUNNING,PENDING]   实时队列（squeue 实时查询）
clu submit   LABEL [-f 脚本 | --script 内容 | stdin]
clu job      list | log
clu session  list | release
clu forward  add | list | remove
```

### 5.2 逐条说明

**连接**

```bash
clu connection import [--config ~/.ssh/config] [--overwrite]   # 一次性导入公钥条目
clu connection add gzu --host hpc.example.edu.cn --user you --kind cluster \
                     [--port 22] [--identity ~/.ssh/id_ed25519] [--proxy 跳板label] [--profile 名]
clu connection remove gzu
clu connection list                # 直读配置文件 + daemon 在线时附池状态
clu connection check gzu           # 经连接池真连一次并执行 true
```

**远程执行**

```bash
clu exec gzu "squeue -u $USER"       # 受安全门约束（危险命令/登录节点重操作会拒绝）
clu exec gzu "df -h" --timeout 30
```

**资源查询**

```bash
clu resources gzu                                  # 分区聚合（紧凑视图）
clu resources gzu --partition gpu-a100             # 指定分区
clu resources gzu --cpu-free                       # 只看有空闲 CPU 的节点
clu resources gzu --cpu-gpu-free --gpu-only        # CPU+GPU 都空闲，且只看 GPU 节点
clu resources gzu --fully-idle                     # CPU 与 GPU 全空闲
clu resources gzu --nodes                          # 附节点明细表
```

**抢占 / 交互式资源**

```bash
clu alloc gzu                                 # 默认 GPU 优先，无 GPU 空闲回退 CPU
clu alloc gzu --gpu --gpus 2 --cpus 16        # 指定卡数与核数
clu alloc gzu --cpu --cpus 8                  # 只抢 CPU
clu alloc gzu -p gpu-a100-8card --purpose train --wait 60
# 输出 granted 后：
clu attach gzu train        # 进入计算节点 shell（exit 即释放）
clu session list gzu        # 查看托管会话（--deep 全量扫描所有连接）
clu session release gzu train       # 释放单个
clu session release gzu --all       # 释放该连接全部
```

抢占选择逻辑：分区按**单节点可满足度**排名（GPU 数/CPU 核数都够才算），
提交被弹回时自动轮换候选分区；默认核数 = `min(节点总核÷节点总卡, 节点空闲核)`。

**批处理作业**

```bash
# 从文件提交（推荐）
clu submit gzu -f train.slurm --name train -p gpu-a100 --gpus 1 -c 16 -t 4:00:00

# 从标准输入提交
echo -e '#!/bin/bash\necho hi' | clu submit gzu --name echo-test -p cpu56c256g -c 1 -t 0:05:00

# 从字面量提交
clu submit gzu --script "#!/bin/bash
hostname
" --name probe

clu jobs gzu                  # 实时队列（当前用户全部作业）
clu jobs gzu --states RUNNING # 只看运行中
clu job list gzu              # 台账（脚本路径/资源/节点/状态/提交时间）
clu job list gzu --active     # 只看非终态
clu job log gzu 12345         # 读台账作业的 stdout/stderr（--tail N）
```

提交行为：
- 脚本与日志落在远端 `<家目录>/.clu/jobs/<连接>/<名称>-<时间戳>-<随机>.sh|-%j.out|-%j.err`
- 资源参数以 `#SBATCH` 指令插在 shebang 之后；脚本中已声明的同类指令不被覆盖
- 脚本内容、路径、资源、状态写入本地 `jobs` 台账；daemon 每 60s 核对状态
  （squeue 实时，已结束的用 sacct 补终态），`job list` 看到的就是最近核对结果
- 安全门同样作用于脚本内容（危险命令拒绝）

**端口转发**

```bash
clu forward add gzu 10.0.0.5 8888 --local-port 18888
clu forward list
clu forward remove fwd-1
```

## 6. 接入 Claude Code（MCP）

```bash
# 用户级（所有会话可用）
claude mcp add --scope user clu -- /path/to/clu-mcp     # 源码安装用 .venv/bin/clu-mcp

# 项目级（随项目共享）
claude mcp add clu -- clu-mcp
```

接入后 16 个工具（带 ○ 的只读，可在 Claude Code 权限设置里配免确认）：

| 工具 | 说明 |
|---|---|
| ○ `connection_list` | 连接与连接池健康状态 |
| `exec` | 远程执行单条命令（安全门在 server 端强制生效） |
| ○ `jobs` / ○ `status` | 作业列表 / 分区与队列概览（需 cluster 连接） |
| ○ `resources` | **资源余量**：每分区 CPU/GPU 的总量/已占/空闲与节点数（单次往返，数据源为节点 AllocTRES 权威值）。一开始就能限定范围：`filter=cpu_free`（有空闲 CPU）、`cpu_gpu_free`（CPU 和 GPU 都有空闲）、`fully_idle`（CPU+GPU 全空闲），`gpu_only=true` 只看有 GPU 的节点（可叠加）；带过滤自动附节点明细 |
| `submit` / `cancel` | 提交作业（可指定分区/节点/CPU/GPU/时限；脚本与 stdout/stderr 统一落远端 `~/.clu/jobs/<连接>/`，脚本内容与路径记入本地台账）/ 取消作业 |
| ○ `job_list` | 作业台账：提交历史、脚本路径与内容、申请资源、节点、日志路径、最近核对状态（只读） |
| ○ `log` | 作业 stdout（自动定位路径并 tail） |
| `alloc` | **抢占接口（自动选择，无需先查询）**：resource 缺省 = GPU 优先、无空闲 GPU 回退 CPU；`resource=gpu/cpu` 只抢该类；`gpus`/`cpus`/`partition` 可指定（GPU 抢占可与 cpus 组合）。分区按单节点余量（含 CPU 核数需求）排名、提交被弹回自动轮换候选。granted 后 `clu attach` 即入计算节点 shell。**CPU 核数默认策略**：GPU 抢占未指定时取 profile 的 `alloc_default_cpus`（未配置则集群 slurm 默认，通常 1 核）；CPU 抢占未指定默认 4 核 |
| ○ `session_list` / ○ `session_info` / `session_release` | 托管会话查看与释放 |
| `upload` / `download` | SFTP 传输（双侧父目录自动创建） |
| ○ `ledger_query` | 检索历史执行（跨会话记忆） |

## 7. 典型场景

**查队列 / 提交 / 看日志**（对 Claude Code 说，或自己跑）：
> "用 clu 看看 gzu 的队列和 GPU 空闲情况"
> "把这个训练脚本提交到 gzu 的 gpu 分区，2 张卡"
> "job 12345 的日志尾部 100 行"

**抢卡 / 抢资源**（一个接口，自动选择）：
> "用 clu 抢一张卡" → `alloc()`（默认 GPU 优先，无空闲 GPU 自动回退 CPU）
> "抢 2 张 A100" → `alloc(gpus=2)`；"抢 8 核 CPU" → `alloc(resource="cpu", cpus=8)`
> 分区自动选（单节点余量排名）；显式 `partition=` 则直用。
> granted 后 `clu attach cluster-r <purpose>` **直接落计算节点 shell**，exit 即释放；
> 用完 "释放 xxx 会话"（session_release）。忙分区提交被弹回时自动轮换候选分区重试。

**断线续传场景**：alloc 会话在远端 tmux 里，笔记本断网、换地方、
重启 Claude Code 都不影响；`clu session list` 随时可查。

**提交批处理作业**：
> "用 clu 把这个脚本提交到 gpu 分区，2 张卡"
> `submit(connection="cluster-r", script=..., name="train", partition="gpu-a100", gpus=1, cpus=16)`
> 或命令行：`clu submit cluster-r -f train.slurm -p gpu-a100 --gpus 1 -c 16`
>
> 脚本与日志落在远端 `~/.clu/jobs/<连接>/`；`clu job list` 看状态（daemon 每 60s 自动核对），
> `clu job log cluster-r <job_id>` 读 stdout/stderr。

**传文件**：
> "把 ./data.csv 上传到 gzu 的 ~/data/ 下" / "把 gzu:~/result.tar 下载到 ./out/"

**端口转发**（访问集群内 Web 服务）：
```bash
clu forward add gzu 10.0.0.5 8888 --local-port 18888   # 打开 http://127.0.0.1:18888
clu forward list && clu forward remove fwd-1
```

## 8. 安全模型

两层（DESIGN §8）：

1. **Claude Code 工具权限**（主）：只读工具与写工具分开声明，按需配免确认白名单
2. **server 端硬规则**（不可绕过，任何客户端生效）：
   - **危险黑名单**：递归删除根/系统目录/家目录、`mkfs`、`dd of=/dev/*`、
     `shutdown/reboot`、`scancel --all`、fork 炸弹等 → 直接拒绝
   - **登录节点重操作拦截**：在 `cluster` 连接上跑训练/编译/装环境
     （torchrun、`make -j`、`pip install` 等，profile 可增删）→ 拒绝并建议
     submit/alloc

认证仅公钥，复用 ssh-agent 与 `~/.ssh`；clu 不存储任何密钥。

## 9. 目录与配置

| 路径 | 内容 |
|---|---|
| `~/.config/clu/connections.toml` | 连接与 profile（可手改，保留注释） |
| `~/.config/clu/known_hosts` | 主机密钥（accept-new） |
| `~/.local/share/clu/ledger.sqlite3` | SQLite：执行留痕 + 会话登记 + 作业台账 |
| `$XDG_RUNTIME_DIR/clu/`（或 `/tmp/clu-$UID/`） | daemon socket 与日志 `daemon.log` |

环境变量 `CLU_CONFIG_DIR` / `CLU_DATA_DIR` / `CLU_RUNTIME_DIR` 可重定位（测试用）。

### 9.1 数据库表（`ledger.sqlite3`）

**`exec_records`** —— 执行流水（`ledger_query` 检索）

| 字段 | 说明 |
|---|---|
| `id` / `ts` | 自增主键 / UTC ISO8601 时间 |
| `connection` | 连接 label |
| `kind` | exec / submit / alloc / release / cancel / upload / download / forward |
| `command` | 命令或脚本摘要 |
| `exit_code` / `duration_ms` | 退出码 / 耗时 |
| `job_id` | 关联的 slurm 作业（可空） |
| `summary` | 一行摘要 |

**`sessions`** —— 托管会话登记 `(label, purpose)` 主键

| 字段 | 说明 |
|---|---|
| `label` / `purpose` | 连接 / 用途名（tmux 会话 `clu/<label>/<purpose>`） |
| `job_id` / `node` / `partition` | 分配到的作业与节点、分区 |
| `gpus` / `cpus` | 申请资源 |
| `state` | pending / granted（连接不可达时列表标注 stale） |
| `updated_ts` | 最近一次核对时间 |

**`jobs`** —— 批处理作业台账 `(connection, job_id)` 主键

| 字段 | 说明 |
|---|---|
| `name` | 作业名 |
| `script_path` / `script` | 远端脚本绝对路径 / 完整脚本内容 |
| `partition` / `requested_nodes` / `cpus` / `gpus` | 申请资源 |
| `node_list` | 实际分配节点（squeue/sacct 核对） |
| `stdout_path` / `stderr_path` | 日志绝对路径（`%j` 已解析为 job_id） |
| `state` | PENDING/RUNNING/COMPLETED/FAILED/CANCELLED/TIMEOUT/... |
| `exit_code` | sacct 退出码（如 `0:0`） |
| `submitted_ts` / `updated_ts` | 提交 / 最近核对时间 |

远端目录：`~/.clu/jobs/<连接>/`（脚本与日志）、`~/.clu/alloc/<连接>/<purpose>.log`（salloc 日志）。

## 10. 故障排查

| 现象 | 处理 |
|---|---|
| `daemon 未运行` | `clu server ensure`；看 `daemon.log` |
| 连接处于退避期（`retry_after`） | 刚失败过，等提示秒数；或 `clu server stop && clu server ensure` 重置连接池 |
| `Host key ... changed` | 主机密钥变了。确认是重装系统等正当原因后，删掉 `~/.config/clu/known_hosts` 对应行 |
| salloc 后 session 一直 pending | 正常排队；`clu session list` 看会话，或让 Claude Code 调 `session_info` 查详情；超时会话保留 |
| salloc 报 "--gres=gpu:N must be input" | 集群提交插件强制经典语法——clu 已内置 `--gres=gpu:N`（贵大实测） |
| attach 后 hostname 是登录节点 | 已修复：clu 用 `srun bash` 落计算节点（无 SallocDefaultCommand 的集群 salloc 默认本地执行） |
| attach 后无提示符/配置未生效 | 已修复：`srun --pty bash -l` 持真终端并走登录 shell；日志改用 tmux pipe-pane（tee 管道会让 srun 拒绝 --pty） |
| 转发不工作 | 转发绑定在连接上，连接断开后需重新 `clu forward add`（最小集限制） |
| `clu session list` 提示连接不可达 | 正常降级：该机器关机/网络不通，仅跳过其会话列举，其余照常（多连接并行探测，6s 上限） |
| squeue 输出解析异常 | 各校魔改 Slurm 差异大——把 `clu exec gzu "squeue -u you"` 原始输出发给作者 |
| 空闲 GPU 数量可疑 | `resources` 的已占取节点 AllocTRES（`scontrol show nodes -o` 里 `gres/gpu=N` 为总量）。先用 `clu exec gzu "scontrol show node <节点名> -o"` 肉眼对账；老集群无 AllocTRES 时返回值会注明是作业估算 |

## 11. 已知边界（v1）

- 文件传输为 SFTP 单文件；目录增量同步（rsync）在 v1.1
- 跳板仅单级（ProxyJump 一跳）；多级链在路线图
- 转发仅本地 `-L`；SOCKS/远程 `-R` 在路线图
- 平台 macOS + Linux；Windows 在路线图
- 密码认证的连接不支持（先 `ssh-copy-id` 配好公钥）
