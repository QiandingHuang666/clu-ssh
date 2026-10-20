"""SFTP 上传/下载（DESIGN §9）：走连接池内连接，单文件传输，写留痕。rsync 目录同步在 v1.1。"""

import contextlib
import posixpath
from pathlib import Path

import asyncssh

from clu.ssh.connection import RealSSHConn, SSHConn


class TransferError(Exception):
    """传输失败（本地文件不存在 / 远端错误等）。"""


def _real(conn: SSHConn) -> RealSSHConn:
    if not isinstance(conn, RealSSHConn):
        raise TransferError("SFTP 需要真实 SSH 连接（测试 fake 连接不支持）")
    return conn


async def _ensure_remote_dir(sftp: asyncssh.SFTPClient, remote_path: str) -> None:
    rdir = posixpath.dirname(remote_path)
    if rdir in ("", "/", "."):
        return
    # 已存在或不可建时静默——交给 put/get 报准确错误
    with contextlib.suppress(asyncssh.SFTPError):
        await sftp.makedirs(rdir, exist_ok=True)


async def upload(conn: SSHConn, local_path: str, remote_path: str) -> dict[str, object]:
    """上传单个本地文件（父目录自动创建）。"""
    real = _real(conn)
    source = Path(local_path).expanduser()
    if not source.is_file():
        raise TransferError(f"本地文件不存在：{source}")
    size = source.stat().st_size
    async with real.raw.start_sftp_client() as sftp:
        await _ensure_remote_dir(sftp, remote_path)
        await sftp.put(str(source), remote_path)
    return {"local_path": str(source), "remote_path": remote_path, "bytes": size}


async def download(conn: SSHConn, remote_path: str, local_path: str) -> dict[str, object]:
    """下载单个远端文件（本地父目录自动创建）。"""
    real = _real(conn)
    target = Path(local_path).expanduser()
    async with real.raw.start_sftp_client() as sftp:
        try:
            attrs = await sftp.stat(remote_path)
        except asyncssh.SFTPError as exc:
            raise TransferError(f"远端文件不可达：{remote_path}（{exc.reason}）") from exc
        size = int(attrs.size or 0)
        target.parent.mkdir(parents=True, exist_ok=True)
        await sftp.get(remote_path, str(target))
    return {"remote_path": remote_path, "local_path": str(target), "bytes": size}
