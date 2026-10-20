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


@server.tool(name="submit", annotations=_WRITE)
async def slurm_submit(connection: str, script: str) -> dict[str, Any]:
    """提交 Slurm 批处理作业：脚本文本经 base64 上传后 sbatch，返回 job_id。"""
    return await _call("slurm.submit", {"label": connection, "script": script})


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


@server.tool(name="connection_list", annotations=_READONLY)
async def connection_list() -> dict[str, Any]:
    """列出全部受管连接及连接池健康状态（label/kind/host/state）。"""
    return await _call("connection.list", {})


def main() -> None:
    """stdio 入口；daemon 缺席时自动拉起。"""
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
