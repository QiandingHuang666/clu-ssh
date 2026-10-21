"""tmux 编排（DESIGN §5.2）：会话命名 clu/<connection>/<purpose>，全部经 SSH 命令操作。

daemon 不缓存句柄——tmux 列表即真相，daemon 重启后自动恢复；
alloc 日志落远端 ~/.clu/alloc/<label>/<purpose>.log（tee -a）。
"""

import shlex
from collections.abc import Awaitable, Callable

from clu.ssh.connection import ExecResult

Runner = Callable[..., Awaitable[ExecResult]]

PREFIX = "clu/"
LIST_FORMAT = "#{session_name}|#{session_created}|#{session_attached}"


def session_name(label: str, purpose: str) -> str:
    """合法会话名：clu/<label>/<purpose>（tmux 名禁冒号，label/purpose 禁斜杠冒号）。"""
    for part in (label, purpose):
        if "/" in part or ":" in part or not part:
            raise ValueError(f"非法会话名成分：{label}/{purpose}")
    return f"{PREFIX}{label}/{purpose}"


def parse_session_name(name: str) -> tuple[str, str] | None:
    if not name.startswith(PREFIX):
        return None
    label, sep, purpose = name[len(PREFIX) :].partition("/")
    if not sep or not label or not purpose:
        return None
    return label, purpose


def log_path(label: str, purpose: str) -> str:
    """远端日志路径。label/purpose 已校验为 [A-Za-z0-9_.-]，$HOME 需保留展开，
    因此此路径不得再整体加引号。"""
    return f"$HOME/.clu/alloc/{label}/{purpose}.log"


async def has_session(runner: Runner, name: str) -> bool:
    result = await runner(f"tmux has-session -t {shlex.quote(name)}")
    return result.exit_code == 0


async def kill_session(runner: Runner, name: str) -> bool:
    result = await runner(f"tmux kill-session -t {shlex.quote(name)}")
    return result.exit_code == 0


async def create_session(
    runner: Runner, name: str, command: str, log_file: str | None = None
) -> None:
    """创建后台会话，command 直接持有 tmux 的 pty（交互前提）。

    日志用 tmux pipe-pane 捕获而非 tee 管道——管道会让 srun --pty
    因 stdout 非 tty 而拒绝伪终端（贵大实测）。log_file 含需展开的 $HOME。
    """
    await runner(f"tmux new-session -d -s {shlex.quote(name)} {shlex.quote(command)}")
    if log_file is not None:
        await runner(f"tmux pipe-pane -o -t {shlex.quote(name)} 'cat >> {log_file}'")


async def list_sessions(runner: Runner, label: str) -> list[dict[str, object]]:
    """列出该连接名下的 clu 托管会话。"""
    result = await runner(f"tmux list-sessions -F '{LIST_FORMAT}' 2>/dev/null || true")
    sessions: list[dict[str, object]] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 3:
            continue
        parsed = parse_session_name(parts[0])
        if parsed is None or parsed[0] != label:
            continue
        sessions.append(
            {
                "connection": parsed[0],
                "purpose": parsed[1],
                "session": parts[0],
                "created": int(parts[1]) if parts[1].lstrip("-").isdigit() else 0,
                "attached": parts[2] != "0",
            }
        )
    return sessions


async def read_log(runner: Runner, label: str, purpose: str, *, tail: int = 50) -> str:
    log = log_path(label, purpose)
    result = await runner(f"tail -n {int(tail)} {log} 2>/dev/null || true")
    return result.stdout
