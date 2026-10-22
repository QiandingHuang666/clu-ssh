# clu

SSH 会话保持与管理 + Slurm 集群 / 普通服务器的 AI agent 能力层。

一个常驻 daemon 持有 SSH 连接池、tmux 托管会话与执行留痕；Claude Code（MCP）
与人类（`clu` CLI）作为两个薄客户端共享同一套能力。

> 当前版本：v0.1.4。设计共识与里程碑见
> [DESIGN.md](https://github.com/QiandingHuang666/clu-ssh/blob/main/DESIGN.md)，
> 完整用法见 [使用说明](https://github.com/QiandingHuang666/clu-ssh/blob/main/docs/usage.md)。

## 能做什么

- 从 `~/.ssh/config` 导入连接，常驻连接池（心跳、断线自动重连、单级跳板）
- 查看集群资源余量与实时队列（可过滤“有空闲 CPU / CPU 与 GPU 都空闲 / 完全空闲 / 只看 GPU 节点”）
- 抢占交互式资源（默认 GPU 优先、无则 CPU），分配后 `clu attach` 直接进入计算节点 shell
- 提交批处理作业（可指定分区/节点数/CPU/GPU/时限），脚本与 stdout/stderr 统一落
  `~/.clu/jobs/`，本地台账定期核对状态
- SFTP 上传下载、本地端口转发
- 执行流水与作业/会话台账可检索；危险命令与登录节点重操作由服务端安全门拦截
- Claude Code 经 MCP 使用同一套能力（16 个工具，只读工具带 read-only hint）

## 安装

```bash
uv tool install clu-ssh      # 或 pipx install clu-ssh
```

要求：Python ≥ 3.11；macOS（Intel / Apple Silicon）、Linux、Windows。发布的是纯
Python wheel（`py3-none-any`），不依赖本机编译工具链；`clu attach` 需要 OpenSSH 客户端。

## 接入 Claude Code（MCP）

```bash
# 项目级
claude mcp add clu -- clu-mcp

# 或用户级
claude mcp add --scope user clu -- clu-mcp
```

工具面（○ 为只读，可在权限设置中免确认）：

| 工具 | 说明 |
|---|---|
| `exec` | 远程执行单条命令（受安全门约束） |
| ○ `jobs` / ○ `status` / ○ `resources` | 实时队列 / 集群概览 / CPU·GPU 余量 |
| `submit` / `cancel` | 提交作业（脚本与日志入 `~/.clu/jobs/`）/ 取消作业 |
| ○ `job_list` / ○ `log` | 作业台账 / 作业日志 |
| `alloc` | 抢占交互式资源（自动选择，GPU 优先回退 CPU） |
| ○ `session_list` / ○ `session_info` / `session_release` | 托管会话查看与释放 |
| `upload` / `download` | SFTP 传输 |
| ○ `ledger_query` / ○ `connection_list` | 执行留痕检索 / 连接与连接池状态 |

## 开发

```bash
uv sync                        # 安装依赖（含 dev 组）
uv run clu --help              # CLI
uv run pytest                  # 单元 + 容器化集成测试
uv run ruff format .           # 格式化
uv run ruff check .            # 静态检查
uv run mypy                    # 类型检查（strict）
```

CI（GitHub Actions）在 macOS、Linux、Windows 矩阵上运行同一套检查（集成测试在无
Docker 的平台自动跳过）；tag 推送触发 PyPI 发布与 GitHub Release。

## License

MIT
