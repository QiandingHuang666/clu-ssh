"""MCP 端到端：真实 stdio 握手（Claude Code 同款路径）。"""

import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from clu.config.store import ConfigStore, ConnectionConfig
from clu.constants import connections_path
from clu.daemon.server import ensure_daemon

pytestmark = pytest.mark.integration


def _payload(result) -> dict:
    """兼容 structured content 与 text content 两种返回形态。"""
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    text = result.content[0].text if result.content else ""
    return dict(json.loads(text)) if text else {}


async def test_mcp_stdio_tools(sshd: dict, tmp_path, monkeypatch) -> None:
    rt = Path(f"/tmp/clu-it3-rt-{os.getpid()}")
    monkeypatch.setenv("CLU_RUNTIME_DIR", str(rt))
    monkeypatch.setenv("CLU_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CLU_DATA_DIR", str(tmp_path / "data"))

    store = ConfigStore(connections_path())
    store.add_connection(
        ConnectionConfig(
            label="cl",
            kind="cluster",
            host=str(sshd["host"]),
            port=int(sshd["port"]),
            user="tester",
            identity=str(sshd["identity"]),
        )
    )
    ensure_daemon()
    # mcp stdio client 默认净化子进程环境，必须显式带上 CLU_* 隔离变量
    env = {
        k: os.environ[k]
        for k in ("PATH", "HOME", "CLU_CONFIG_DIR", "CLU_DATA_DIR", "CLU_RUNTIME_DIR")
        if k in os.environ
    }
    params = StdioServerParameters(command=sys.executable, args=["-m", "clu.mcp_server"], env=env)
    try:
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            listed = await session.list_tools()
            names = {t.name for t in listed.tools}
            assert {
                "exec",
                "jobs",
                "status",
                "submit",
                "log",
                "cancel",
                "ledger_query",
                "connection_list",
                "alloc",
                "session_list",
                "session_info",
                "session_release",
            } <= names
            by_name = {t.name: t for t in listed.tools}
            assert by_name["jobs"].annotations.read_only_hint is True
            assert by_name["submit"].annotations.read_only_hint is False

            result = await session.call_tool(
                "exec", {"connection": "cl", "command": "echo via-mcp"}
            )
            payload = _payload(result)
            assert payload["exit_code"] == 0
            assert "via-mcp" in payload["stdout"]

            jobs = _payload(await session.call_tool("jobs", {"connection": "cl"}))
            assert len(jobs["jobs"]) == 2

            denied = await session.call_tool(
                "exec", {"connection": "cl", "command": "mkfs.ext4 /dev/sda"}
            )
            assert denied.is_error
    finally:
        shutil.rmtree(rt, ignore_errors=True)
