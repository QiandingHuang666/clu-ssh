"""通用托管会话后端（v0.2）：按远端能力自动选择。

- tmux（集群/有 tmux 的服务器）：交互式会话，attach 可回，断线保活
- setsid（无 tmux 的服务器）：setsid nohup 脱离会话组保活，
  日志落 ~/.clu/sessions/<label>/，不可 attach（物理上无终端）

能力判别：connection.check 返回 kind 与 has_tmux。
"""

from __future__ import annotations

import shlex
from typing import Any

import clu.tmux as tmux

Runner = Any  # runner(command, *, timeout) -> ExecResult


async def has_tmux(runner: Runner) -> bool:
    return bool((await runner("command -v tmux")).exit_code == 0)


async def start(
    runner: Runner,
    label: str,
    purpose: str,
    *,
    backend: str,
    command: str | None = None,
    ptyd_bin: str | None = None,
) -> dict[str, Any]:
    """启动托管会话，返回登记信息（backend/session/pid/log/state）。"""
    name = tmux.session_name(label, purpose)
    if backend == "pty-daemon":
        if not ptyd_bin:
            raise ValueError("pty-daemon 后端需要 ptyd_bin")
        r = await runner(f"{ptyd_bin} create -name {shlex.quote(purpose)}")
        try:
            import json

            resp = json.loads(r.stdout.strip() or "{}")
        except ValueError:
            resp = {}
        if r.exit_code != 0 or not resp.get("ok"):
            return {
                "backend": "pty-daemon",
                "session": name,
                "state": "failed",
                "error": f"ptyd create 失败：{r.stderr.strip() or r.stdout.strip()[:200]}",
            }
        return {"backend": "pty-daemon", "session": name, "state": "running"}
    if backend == "tmux":
        inner = f"bash -lc {shlex.quote(command)}" if command else "bash -l"
        await tmux.create_session(runner, name, inner, log_file=tmux.log_path(label, purpose))
        return {"backend": "tmux", "session": name, "state": "running"}
    if not command:
        raise ValueError("setsid 后端必须提供 command")
    log = f"$HOME/.clu/sessions/{label}/{purpose}.log"
    await runner(f"mkdir -p $HOME/.clu/sessions/{shlex.quote(label)}")
    inner = f"exec bash -lc {shlex.quote(command)}"
    result = await runner(
        f"setsid nohup bash -c {shlex.quote(inner)} > {log} 2>&1 < /dev/null & echo $!"
    )
    pid = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else None
    if not pid or not pid.isdigit():
        return {"backend": "setsid", "session": name, "log": log, "state": "failed"}
    return {"backend": "setsid", "session": name, "pid": int(pid), "log": log, "state": "running"}


async def info_setsid(
    runner: Runner, pid: int, log: str | None, *, tail: int = 50
) -> dict[str, Any]:
    alive = bool((await runner(f"kill -0 {int(pid)}")).exit_code == 0)
    text = ""
    if log:
        text = (await runner(f"tail -n {int(tail)} {log} 2>/dev/null || true")).stdout
    return {"state": "running" if alive else "exited", "log_text": text}


async def release_setsid(runner: Runner, pid: int) -> bool:
    return bool((await runner(f"kill {int(pid)}")).exit_code == 0)


async def info_ptyd(
    runner: Runner, ptyd_bin: str, purpose: str, *, tail: int = 50
) -> dict[str, Any]:
    """pty-daemon 会话状态：进程存活 + 日志尾部。"""
    import json

    r = await runner(f"{ptyd_bin} list")
    state = "exited"
    if r.exit_code == 0:
        try:
            sessions = json.loads(r.stdout)
            state = "running" if any(s.get("name") == purpose for s in sessions) else "exited"
        except ValueError:
            state = "unknown"
    log = f"$HOME/.clu/ptyd/{purpose}.log"
    text = (await runner(f"tail -n {int(tail)} {log} 2>/dev/null || true")).stdout
    return {"state": state, "log_text": text}


async def release_ptyd(runner: Runner, ptyd_bin: str, purpose: str) -> bool:
    return bool((await runner(f"{ptyd_bin} kill -name {shlex.quote(purpose)}")).exit_code == 0)
