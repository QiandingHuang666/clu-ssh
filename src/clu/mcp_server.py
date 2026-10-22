"""MCP stdio server：Claude Code 等 agent 的工具面（DESIGN §6，mcp 2.x）。

薄客户端：所有工具经 unix socket 转发给 daemon 执行（安全门/留痕都在 daemon）。
只读工具（jobs/status/log/ledger_query/connection_list）带 read_only_hint，
便于在 Claude Code 侧配置只读免确认。
"""

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from clu.client import CluClient
from clu.daemon.server import ensure_daemon

server = MCPServer(
    name="clu",
    title="clu",
    description="SSH 会话保持/管理 + Slurm 集群与服务器 agent 能力层",
)

_READONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)
_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False)


async def _call(method: str, params: dict[str, Any]) -> dict[str, Any]:
    async with CluClient(timeout=120.0) as client:
        return dict(await client.request(method, params))


@server.tool(name="exec", annotations=_WRITE)
async def exec_command(
    connection: str, command: str, timeout: float | None = None
) -> dict[str, Any]:
    """在远程连接上执行单条 shell 命令。危险命令与集群登录节点重操作会被服务端拒绝。

    Args:
        connection: 连接 label（见 connection_list）
        command: 要执行的 shell 命令
        timeout: 超时秒数（默认 60）
    """
    return await _call("exec", {"label": connection, "command": command, "timeout": timeout})


@server.tool(name="jobs", annotations=_READONLY)
async def slurm_jobs(connection: str, states: list[str] | None = None) -> dict[str, Any]:
    """列出 Slurm 集群上当前用户的作业（job_id/状态/分区/耗时/资源，结构化）。"""
    return await _call("slurm.jobs", {"label": connection, "states": states})


@server.tool(name="status", annotations=_READONLY)
async def slurm_status(connection: str) -> dict[str, Any]:
    """Slurm 集群概览：分区（节点/CPU/内存/GPU）与队列状态计数。"""
    return await _call("slurm.status", {"label": connection})


@server.tool(name="resources", annotations=_READONLY)
async def resources(
    connection: str,
    partition: str | None = None,
    flt: str | None = None,
    gpu_only: bool = False,
    include_nodes: bool = False,
) -> dict[str, Any]:
    """查询集群资源余量：每分区 CPU/GPU 的总量/已占/空闲与节点数。

    一开始就限定查看范围（agent 与人类同款口径）：
    flt=cpu_free（有空闲 CPU 的节点）、flt=cpu_gpu_free（CPU 和 GPU 都有空闲）、
    flt=fully_idle（CPU 与 GPU 全空闲）；gpu_only=true 只看有 GPU 的节点（可叠加）。
    partition 限定分区。默认紧凑（分区聚合）；带任一过滤或 include_nodes
    才返回节点明细。不要自己拼 sinfo/scontrol。

    Args:
        connection: cluster 连接 label
        partition: 只看该分区
        flt: cpu_free | cpu_gpu_free | fully_idle
        gpu_only: 只看有 GPU 的节点（与其他条件叠加）
        include_nodes: 返回全部节点明细（大集群建议配合过滤）
    """
    return await _call(
        "slurm.resources",
        {
            "label": connection,
            "partition": partition,
            "filter": flt,
            "gpu_only": gpu_only,
            "include_nodes": include_nodes,
        },
    )


@server.tool(name="submit", annotations=_WRITE)
async def slurm_submit(
    connection: str,
    script: str,
    name: str | None = None,
    partition: str | None = None,
    nodes: int | None = None,
    cpus: int | None = None,
    gpus: int | None = None,
    time_limit: str | None = None,
) -> dict[str, Any]:
    """提交 Slurm 批处理作业：脚本写入 ~/.clu/jobs/<连接>/ 后 sbatch。

    stdout/stderr 默认重定向到同目录 <name>-<时间>-%j.out/.err（脚本自带
    #SBATCH -o/-e 时尊重脚本）；脚本内容、路径、资源与状态记入本地 jobs 台账。

    Args:
        connection: cluster 连接 label
        script: sbatch 脚本内容（含 shebang）
        name: 作业名（默认 job）
        partition: 分区（脚本未声明时注入）
        nodes: 节点数（脚本未声明时注入）
        cpus: 每任务 CPU 核数
        gpus: GPU 卡数（注入 --gres=gpu:N）
        time_limit: 时限（如 4:00:00）
    """
    return await _call(
        "slurm.submit",
        {
            "label": connection,
            "script": script,
            "name": name,
            "partition": partition,
            "nodes": nodes,
            "cpus": cpus,
            "gpus": gpus,
            "time_limit": time_limit,
        },
    )


@server.tool(name="job_list", annotations=_READONLY)
async def job_list(connection: str | None = None, active_only: bool = False) -> dict[str, Any]:
    """查询本地作业台账：脚本路径与内容、申请资源、节点、stdout/stderr 路径、
    最近一次核对的状态（daemon 定期刷新）。"""
    return await _call("job.list", {"label": connection, "active_only": active_only})


@server.tool(name="log", annotations=_READONLY)
async def slurm_log(connection: str, job_id: str, tail: int = 100) -> dict[str, Any]:
    """读取作业 stdout（自动定位 StdOut 路径并 tail）。"""
    return await _call("slurm.log", {"label": connection, "job_id": job_id, "tail": tail})


@server.tool(name="cancel", annotations=_WRITE)
async def slurm_cancel(connection: str, job_id: str) -> dict[str, Any]:
    """取消指定作业（scancel 单个作业）。scancel --all 被安全门禁止。"""
    return await _call("slurm.cancel", {"label": connection, "job_id": job_id})


@server.tool(name="ledger_query", annotations=_READONLY)
async def ledger_query(
    connection: str | None = None,
    kind: str | None = None,
    keyword: str | None = None,
    job_id: str | None = None,
    since: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """检索执行留痕（跨会话记忆）：按连接/类型/关键词/作业号过滤，时间倒序。"""
    return await _call(
        "ledger.query",
        {
            "connection": connection,
            "kind": kind,
            "keyword": keyword,
            "job_id": job_id,
            "since": since,
            "limit": limit,
        },
    )


@server.tool(name="alloc", annotations=_WRITE)
async def slurm_alloc(
    connection: str,
    purpose: str = "alloc",
    resource: str | None = None,
    gpus: int = 1,
    cpus: int | None = None,
    partition: str | None = None,
    wait: float = 120.0,
) -> dict[str, Any]:
    """抢占资源并直接执行（绝不只返回命令）：tmux 内 salloc + srun bash，
    分配后 attach 即入计算节点 shell，断线不丢资源。同名 purpose 已有活会话时返回
    state="exists"（附 job 与接入/释放指引）——不会静默复用旧会话。

    抢占选择自动完成（无需先查询）：resource 缺省 = GPU 优先、无空闲 GPU 回退 CPU；
    resource=gpu / cpu 只抢该类（无则失败并附当前余量摘要）；
    partition 显式指定则直接使用。返回 granted/pending/failed 与 job_id/node。

    Args:
        connection: cluster 连接 label
        purpose: 会话用途名（如 train-a100），用于 tmux 命名与检索
        resource: gpu | cpu；缺省为自动（GPU 优先回退 CPU）
        gpus: GPU 卡数（默认 1；resource 含 gpu 时生效）
        cpus: CPU 核数（cpu 资源必给则默认 4；也可与 GPU 同时指定）
        partition: 显式分区（跳过自动选择）
        wait: 等待授予秒数（超时会话保留，session_info 随时查）
    """
    return await _call(
        "slurm.alloc",
        {
            "label": connection,
            "purpose": purpose,
            "resource": resource,
            "gpus": gpus,
            "cpus": cpus,
            "partition": partition,
            "wait": wait,
        },
    )


@server.tool(name="session_list", annotations=_READONLY)
async def session_list(connection: str | None = None) -> dict[str, Any]:
    """列出 tmux 托管会话（仅 alloc 抢占产生的资源会话；普通查询任务请用 exec）。

    要求远端装有 tmux；机器上没有 tmux 时列表为空或 alloc 会明确报错。
    """
    return await _call("session.list", {"label": connection})


@server.tool(name="session_info", annotations=_READONLY)
async def session_info(connection: str, purpose: str) -> dict[str, Any]:
    """查看某个托管会话详情：分配状态、job_id、节点、日志尾部。"""
    return await _call("session.info", {"label": connection, "purpose": purpose})


@server.tool(name="session_release", annotations=_WRITE)
async def session_release(connection: str, purpose: str) -> dict[str, Any]:
    """释放托管会话：杀 tmux → salloc 退出 → 资源归还集群。"""
    return await _call("session.release", {"label": connection, "purpose": purpose})


@server.tool(name="upload", annotations=_WRITE)
async def upload(connection: str, local_path: str, remote_path: str) -> dict[str, Any]:
    """上传本地文件到远端（SFTP，远端父目录自动创建）。"""
    return await _call(
        "transfer.upload",
        {"label": connection, "local_path": local_path, "remote_path": remote_path},
    )


@server.tool(name="download", annotations=_WRITE)
async def download(connection: str, remote_path: str, local_path: str) -> dict[str, Any]:
    """下载远端文件到本地（SFTP，本地父目录自动创建）。"""
    return await _call(
        "transfer.download",
        {"label": connection, "remote_path": remote_path, "local_path": local_path},
    )


@server.tool(name="connection_list", annotations=_READONLY)
async def connection_list() -> dict[str, Any]:
    """列出全部受管连接及连接池健康状态（label/kind/host/state）。"""
    return await _call("connection.list", {})


def main() -> None:
    """stdio 入口；daemon 缺席时自动拉起。"""
    import sys

    if sys.stdin.isatty():
        # 被人手工在终端里跑：给提示而不是挂在 stdio 上等握手
        print(
            "clu-mcp 是 MCP stdio 服务，需由 MCP 客户端拉起（不是直接运行）。\n"
            "接入 Claude Code：claude mcp add clu -- clu-mcp",
            file=sys.stderr,
        )
        raise SystemExit(2)

    import asyncio

    from clu.client import CluError

    async def _ensure() -> None:
        try:
            async with CluClient() as client:
                await client.request("ping")
        except CluError:
            ensure_daemon()

    asyncio.run(_ensure())
    server.run("stdio")


if __name__ == "__main__":
    main()
