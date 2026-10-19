# clu

SSH 会话保持与管理 + Slurm 集群 / 普通服务器的 AI agent 能力层。

一个常驻 daemon 持有 SSH 连接池、tmux 托管会话与执行留痕；Claude Code（MCP）
与人类（`clu` CLI）作为两个薄客户端共享同一套能力。

> 状态：M0 骨架阶段，尚未发布。设计共识与里程碑见 [DESIGN.md](DESIGN.md)。

总体架构图（交互式，明暗主题）：[docs/architecture.html](docs/architecture.html)

## 安装（计划）

```bash
uv tool install clu-ssh
```

## 开发

```bash
uv sync                        # 安装依赖（含 dev 组）
uv run clu --help              # CLI
uv run pytest                  # 单元测试
uv run ruff format .           # 格式化
uv run ruff check .            # 静态检查
uv run mypy                    # 类型检查（strict）
```

CI（GitHub Actions）在 macOS 与 Linux 矩阵上运行同一套检查。

## License

MIT
