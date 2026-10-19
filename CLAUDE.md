# clu 开发约定

- 一切设计以 DESIGN.md 为准；改设计先改文档（§13 决策记录表追加一行），再写代码。
- 模块 docstring 标注所属里程碑（M1–M4），不提前实现后续里程碑内容。
- uv 管理环境。提交前四件套全绿：
  `uv run ruff format --check .` → `uv run ruff check .` → `uv run mypy` → `uv run pytest`
- mypy strict；ruff（E,F,W,I,UP,B,C4,SIM,RUF）；conventional commits。
- 测试三层：unit（纯逻辑）、integration（M1 起，Docker sshd 容器 + 假 slurm）、
  验收（真实集群 3 天日用，手动）。
- 文档/面向用户文案用中文；代码标识符用英文。
