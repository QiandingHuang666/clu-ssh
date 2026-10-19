# clu 设计文档

> 共识来源：2026-10-19 设计评审（grilling）会话。本文档是唯一权威设计来源；
> 改设计先改本文档并在 §13 决策记录表追加一行，再写代码。

## 1. 定位与设计原则

面向高校师生的**单用户**开源工具，两层价值合一：

1. **AI 能力层**：Claude Code（MCP）为第一消费方；其他 agent 未来经 MCP 接入
2. **SSH 会话管理层**：人类经 `clu` CLI 直接使用同一套连接池与托管会话

不自建 agent 循环、不做事务/计划模型——那是 agent 层（Claude Code）的事。
本项目只做被所有消费方共享的**能力层**。

前作教训（connector v1/v2、slurm-bot-executor-redesign、Rust slurm-client 脚手架）：
设计过度、工程实现不规范，均未达到日用状态。对策：

- **能力层保持薄**：留痕是执行副产物，不预建模；不重建审批流
- **工程规范保持硬**：mypy strict、ruff、pytest 全绿才合并；容器化集成测试

## 2. 命名与发行

| 项 | 值 | 备注 |
|---|---|---|
| PyPI 包名 | `clu-ssh` | 已核查可用（`clu`、`clu-cli`、`clink` 被占用） |
| CLI 命令 | `clu` | `clu status` / `clu attach` |
| 配置目录 | `~/.config/clu/` | 遵循 XDG |
| 数据目录 | `~/.local/share/clu/` | 留痕 SQLite |
| 运行时目录 | `$XDG_RUNTIME_DIR/clu/` | unix socket |
| License | MIT | |

发行：`uv tool install clu-ssh`。平台 v1 仅 macOS + Linux；Windows 进路线图。

## 3. 总体架构

```
┌─────────────┐        ┌──────────┐
│ Claude Code │        │   人类    │
│ (MCP stdio) │        │          │
└──────┬──────┘        └────┬─────┘
       │ MCP                │ CLI（clu）
┌──────▼────────────────────▼──────┐
│ mcp_server.py        cli.py      │  薄客户端（无常驻状态）
└───────────────┬─────────────────┘
                │ unix socket（JSON 消息，见 src/clu/protocol.py）
┌───────────────▼─────────────────┐
│           clu daemon            │
│  ┌──────────┐ ┌──────┐ ┌─────┐  │
│  │ SSH 连接池│ │ tmux │ │policy│  │
│  └────┬─────┘ └──┬───┘ └──▲──┘  │
│  ┌────▼─────┐ ┌──▼──────┐  │     │
│  │  ledger  │ │ slurm   │──┘     │
│  │ (sqlite) │ │ commands│        │
│  └──────────┘ └─────────┘        │
└────────┬───────────────┬─────────┘
         │ asyncssh      │ ssh（tmux/命令通道）
         ▼               ▼
   集群 / 服务器（贵大 HPC、aliyun、4090、WSL……）
```

进程模型：

- **daemon**：唯一常驻进程，持有连接池、tmux 会话句柄、SQLite 留痕。
  缺席时由任一客户端自动拉起（`clu server ensure` 模式）
- **MCP server**：Claude Code 经 stdio 启动的短生命周期进程，只做协议转换
- **CLI**：一次性进程，经 unix socket 调 daemon

MCP stdio 进程随时会被 Claude Code 杀掉，因此一切状态必须归 daemon。

## 4. 连接与配置管理

`~/.config/clu/connections.toml`（tomlkit 读写，保留用户注释）：

```toml
[connections.gzu]
kind = "cluster"          # cluster | instance | local
host = "hpc.example.edu.cn"
port = 22
user = "you"
proxy = "gzu-relay"       # 可选：单级跳板，值为另一连接的 label
profile = "gzu"           # 可选：定制 profile

[profiles.gzu]
public_dirs = ["/public/home", "/public/soft"]
heavy_guard = true        # 登录节点重操作拦截（默认全局开启，profile 可加名单）
preempt_partition = "..." # 抢占模式使用的分区
```

- **导入语义**：`clu connection import` 从 `~/.ssh/config` **一次性导入副本**，
  不持续跟踪源文件变化；**仅导入公钥认证可达的条目**；解析 `HostName/Port/
  User/ProxyJump`；跳板引用不存在的条目时导入告警
- **认证**：仅公钥；复用 ssh-agent 与 `~/.ssh` 中的 key 路径，不自建凭据库
- **kind 语义**：`cluster`（Slurm 集群，启用 slurm 工具面）/ `instance`
  （租用 GPU 机等普通服务器）/ `local`（局域网/本机转发目标）
- **profile**：一切学校/集群定制（公共资源目录、重操作名单、抢占分区）进
  profile 配置，代码零硬编码；仓库内置贵大 profile 作为示例

## 5. SSH 能力层

### 5.1 连接池（M1）

- 按 label 管理长连 asyncssh 连接；懒建立，首次使用时连
- 心跳：asyncssh keepalive，间隔 30s（可配）
- 断线：自动重连，指数退避 1s → 上限 30s；重连期间请求排队或快速失败（M1 定）
- 单级跳板：经 `proxy` 指向的连接建立 tunnel（asyncssh 原生）；多级链进路线图
- 通道：命令执行、SFTP、tmux 控制共用同一连接，每连接通道数上限（默认 10，可配）

### 5.2 tmux 编排保活（M3）

- 所有交互式资源（salloc）与长任务在远端 tmux 会话内运行，
  命名规范 `clu/<connection_label>/<purpose>`
- daemon 持有"tmux 会话 → 意图/关联 job/资源"映射表（重启后可从 tmux 列表恢复）
- `clu attach <label>/<purpose>` 人类直接接入
- saloc 获得分配后 `sleep infinity` 占住资源（继承旧 skill 抢占语义）

### 5.3 端口转发（M4，最小集）

- 本地转发（`-L` 等价）：`clu forward add/list/remove`，经连接池连接建立
- 动态转发（SOCKS）与远程转发（`-R`）进路线图

## 6. MCP 工具面（M2）

专用结构化工具优先，泛用 `exec` 兜底。全部经 daemon 执行并写留痕。

| 工具 | 关键参数 | 输出 | 里程碑 |
|---|---|---|---|
| `exec` | connection, cmd, timeout | stdout/stderr/exit_code/时长 | M2 |
| `jobs` | connection, state? | 结构化作业列表（squeue） | M2 |
| `status` | connection | 分区/节点/GPU 概览（sinfo+squeue 聚合） | M2 |
| `submit` | connection, script（文本或远端路径） | job_id | M2 |
| `log` | connection, job_id, tail? | 日志文本 | M2 |
| `cancel` | connection, job_id | 结果 | M2 |
| `ledger_query` | 时间范围/连接/关键词/job_id 过滤 | 留痕记录列表（结构化过滤，不暴露裸 SQL） | M2 |
| `alloc` | connection, partition, gpu?, preempt? | 分配与 tmux 会话信息 | M3 |
| `session_list` / `session_info` | connection? | tmux 托管会话列表/详情 | M3 |
| `upload` / `download` | connection, local_path, remote_path | 传输结果 | M4 |
| `connection_list` | — | 连接与池状态（含 health） | M2 |

**alloc 语义（继承旧 skill，不可削弱）**：

- 申请资源必须**直接执行**，禁止只返回 salloc 命令让用户手动跑；
  仅当用户明确说"先看命令"时才返回命令文本
- "抢占 A100"等表述 → `preempt=true`：tmux 内 salloc + `sleep infinity` 保活
- 只读工具（jobs/status/log/ledger_query/connection_list）与写工具在 MCP 层
  分开声明，便于 Claude Code 权限配置只读免确认

## 7. 留痕（SQLite 最小集）

执行的自然副产物，单表起步：

```sql
CREATE TABLE exec_records (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,     -- UTC ISO8601
    connection  TEXT NOT NULL,     -- 连接 label
    kind        TEXT NOT NULL,     -- exec | submit | alloc | cancel | upload | download | forward
    command     TEXT NOT NULL,
    exit_code   INTEGER,
    duration_ms INTEGER,
    job_id      TEXT,              -- 可空：关联 slurm 作业
    summary     TEXT               -- 可空：一行摘要，供检索
);
CREATE INDEX idx_exec_ts ON exec_records(ts);
CREATE INDEX idx_exec_conn ON exec_records(connection);
```

不做事务/计划模型。检索 = `ledger_query` 结构化过滤。

## 8. 安全模型（双层）

**第一层：Claude Code 工具权限**（主）。只读/写工具分开声明，用户在 Claude
Code 侧配置免确认白名单。

**第二层：server 端硬规则**（极薄，仅两条）：

1. **登录节点重操作拦截**：在 `kind=cluster` 的登录节点上命中重操作模式 →
   拒绝并建议 alloc/submit。初始名单（profile 可增）：
   `torchrun`、`deepspeed`、`accelerate launch`、`python -m torch.distributed`、
   `make -j`、`cmake --build`、`conda create/install`、`pip install`（大包场景）
2. **危险模式黑名单**（无条件拒绝）：递归删除根/家目录的 `rm -rf`、`mkfs`、
   `dd of=/dev/*`、`shutdown/reboot`、`scancel --all`、fork 炸弹、
   `chmod -R 777 /`

不重建审批流；其余信任第一层。

## 9. 文件传输

- **M4：SFTP**——走连接池内连接，单文件与少量文件；`upload/download` 记留痕
- **v1.1：系统 rsync**——目录增量同步，spawn rsync 子进程（用系统 ssh，绕开
  连接池但传输仍记留痕）
- "先查公共资源目录再下载"等学校规则进 profile 提示，不进传输层

## 10. 测试策略

三层，**不依赖真实集群**：

1. **单元测试**：连接池状态机、重连退避、策略门匹配、配置读写——纯逻辑，
   快速全量跑
2. **集成测试**：**Docker 容器当集群/服务器**——sshd 容器（注入测试 key）+
   容器内假 slurm CLI（脚本输出固定 squeue/sacct/sinfo JSON）。不碰物理机、
   不碰真集群，开源贡献者本地与 CI 都能跑
3. **验收测试**：3 天真实日用（见 §12），手动执行

## 11. 工程规范

- uv 管理环境与依赖；Python ≥ 3.11
- ruff（E,F,W,I,UP,B,C4,SIM,RUF）+ ruff format
- mypy **strict**
- pytest + pytest-asyncio；CI = GitHub Actions，macOS + Linux 矩阵
- conventional commits
- 提交前四件套全绿：`ruff format --check` → `ruff check` → `mypy` → `pytest`
- 模块 docstring 标注所属里程碑（M1–M4）；不提前实现后续里程碑内容

## 12. 里程碑与验收

| 里程碑 | 内容 | 完成定义 |
|---|---|---|
| M0 | 骨架 + 本文档 | 目录/工具链/CI 就位，四件套全绿，CLI `--help` 可用 |
| M1 | daemon + 连接池 | socket 通信、ensure 拉起、ssh config 导入、心跳重连、单级跳板；容器集成测试覆盖连接建立/断线重连 |
| M2 | MCP 工具面 + 安全门 | Claude Code 经 MCP 完成 exec/jobs/status/submit/log/cancel + ledger_query；两条硬规则生效 |
| M3 | alloc + tmux 保活 | 抢卡→alloc 直接执行→tmux 保活→`clu attach` 全链路 |
| M4 | SFTP + 转发管理 | 上传/下载可用；`-L` 按需管理 |
| **验收** | **3 天日用** | 连续 3 天，贵大集群全部日常操作（查卡/提交/看日志/抢卡/传文件）100% 经 Claude Code 或 `clu`，零手打 ssh |

**v1.1 路线图**：系统 rsync 同步、多级跳板链、SOCKS/远程转发、Windows 支持。

## 13. 决策记录

| 日期 | 决策 | 理由 |
|---|---|---|
| 2026-10-19 | 双栖能力层（MCP+CLI），不自建 agent 循环 | 与 Claude Code 重复造轮子；MCP 天然服务其他 agent |
| 2026-10-19 | Python + asyncssh，弃 Rust/包装系统 ssh | 最快到达日用；ControlMaster 与保活需求冲突 |
| 2026-10-19 | daemon + 薄客户端（unix socket，ensure 拉起） | MCP stdio 进程随时被杀，状态必须归 daemon |
| 2026-10-19 | 连接配置独立 toml + ssh config 一次性导入（含 ProxyJump、仅公钥） | 可管理、可预测；高校密码场景引导配 key |
| 2026-10-19 | SQLite 最小留痕，不做事务模型 | 留痕是副产物；事务归 agent 层 |
| 2026-10-19 | 安全双层：Claude Code 权限为主 + server 两条硬规则 | 不重建审批流；真踩过的坑用硬规则兜底 |
| 2026-10-19 | tmux 编排保活，弃 mosh/ET | 集群无 root 装不了；托管资源会话才是痛点 |
| 2026-10-19 | SFTP 先行，rsync v1.1 | 先跑起来；SFTP 覆盖 80% 场景 |
| 2026-10-19 | 单级跳板进 v1，多级链路线图 | asyncssh 原生支持成本低；导入即全可用 |
| 2026-10-19 | macOS + Linux 优先 | HPC 用户工作机以 Mac/Linux 为主 |
| 2026-10-19 | 集成测试用容器，不用进程内假服务器/物理机 | 环境更真实；贡献者无真集群也能跑 |
| 2026-10-19 | 包名 clu-ssh（clu 被占用） | PyPI 已核查 |
