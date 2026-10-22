"""SQLite 留痕：建表与写入（exec_records 单表，DESIGN §7）。检索在 M2 的 query.py。"""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

import aiosqlite

from clu.ledger.query import LedgerFilter

SCHEMA = """
CREATE TABLE IF NOT EXISTS exec_records (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    connection  TEXT NOT NULL,
    kind        TEXT NOT NULL,
    command     TEXT NOT NULL,
    exit_code   INTEGER,
    duration_ms INTEGER,
    job_id      TEXT,
    summary     TEXT
);
CREATE INDEX IF NOT EXISTS idx_exec_ts ON exec_records(ts);
CREATE INDEX IF NOT EXISTS idx_exec_conn ON exec_records(connection);
CREATE TABLE IF NOT EXISTS jobs (
    connection   TEXT NOT NULL,
    job_id       TEXT NOT NULL,
    name         TEXT,
    script_path  TEXT,
    script       TEXT,
    partition    TEXT,
    requested_nodes TEXT,
    cpus         INTEGER,
    gpus         INTEGER,
    node_list    TEXT,
    stdout_path  TEXT,
    stderr_path  TEXT,
    state        TEXT NOT NULL,
    exit_code    TEXT,
    submitted_ts TEXT NOT NULL,
    updated_ts   TEXT NOT NULL,
    PRIMARY KEY (connection, job_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_jobs_ts ON jobs(submitted_ts);
CREATE TABLE IF NOT EXISTS sessions (
    label       TEXT NOT NULL,
    purpose     TEXT NOT NULL,
    job_id      TEXT,
    node        TEXT,
    partition   TEXT,
    gpus        INTEGER,
    cpus        INTEGER,
    state       TEXT NOT NULL,
    backend     TEXT,
    pid         INTEGER,
    updated_ts  TEXT NOT NULL,
    PRIMARY KEY (label, purpose)
);
"""


class Ledger:
    """执行留痕的异步写入器；daemon 启动时 open，退出时 close。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        try:
            # 逐条执行（executescript 的隐式 COMMIT 与迁移交错过一次事故）
            import contextlib
            import sqlite3

            for stmt in (x + ";" for x in SCHEMA.split(";") if x.strip()):
                await self._db.execute(stmt)
            # 旧库迁移：v0.1.x 的 sessions 表无 backend/pid 列（已存在则跳过）
            with contextlib.suppress(sqlite3.OperationalError):
                await self._db.execute("ALTER TABLE sessions ADD COLUMN backend TEXT")
            with contextlib.suppress(sqlite3.OperationalError):
                await self._db.execute("ALTER TABLE sessions ADD COLUMN pid INTEGER")
            await self._db.commit()
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection | None:
        return self._db

    async def query(self, f: LedgerFilter) -> list[dict[str, object]]:
        """结构化检索；db 未打开时返回空。"""
        from clu.ledger.query import query_ledger

        if self._db is None:
            return []
        return await query_ledger(self._db, f)

    # ---- 作业台账 ----

    _JOB_FIELDS = (
        "name",
        "script_path",
        "script",
        "partition",
        "requested_nodes",
        "cpus",
        "gpus",
        "node_list",
        "stdout_path",
        "stderr_path",
        "state",
        "exit_code",
    )

    async def upsert_job(
        self, *, connection: str, job_id: str, state: str, **fields: object
    ) -> None:
        """提交入库（新作业）；同名同连接则更新。"""
        if self._db is None:
            return
        from datetime import datetime

        now = datetime.now(UTC).isoformat(timespec="seconds")
        values = {f: fields.get(f) for f in self._JOB_FIELDS}
        values["state"] = state
        cols = ["connection", "job_id", *self._JOB_FIELDS, "submitted_ts", "updated_ts"]
        row = [connection, job_id, *[values[f] for f in self._JOB_FIELDS], now, now]
        updates = ", ".join(f"{c} = excluded.{c}" for c in [*self._JOB_FIELDS, "updated_ts"])
        await self._db.execute(
            f"INSERT INTO jobs ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
            f" ON CONFLICT(connection, job_id) DO UPDATE SET {updates}",
            row,
        )
        await self._db.commit()

    async def update_job(self, connection: str, job_id: str, **fields: object) -> None:
        """按字段更新作业（状态核对用）。"""
        if self._db is None:
            return
        from datetime import datetime

        updates = {k: v for k, v in fields.items() if k in self._JOB_FIELDS}
        updates["updated_ts"] = datetime.now(UTC).isoformat(timespec="seconds")
        assignments = ", ".join(f"{k} = ?" for k in updates)
        await self._db.execute(
            f"UPDATE jobs SET {assignments} WHERE connection = ? AND job_id = ?",
            [*updates.values(), connection, job_id],
        )
        await self._db.commit()

    async def list_jobs(
        self, connection: str | None = None, *, active_only: bool = False
    ) -> list[dict[str, object]]:
        if self._db is None:
            return []
        terminal = (
            "COMPLETED",
            "FAILED",
            "CANCELLED",
            "TIMEOUT",
            "OUT_OF_MEMORY",
            "NODE_FAIL",
            "PREEMPTED",
            "BOOT_FAIL",
            "DEADLINE",
        )
        clauses: list[str] = []
        params: list[object] = []
        if connection:
            clauses.append("connection = ?")
            params.append(connection)
        if active_only:
            placeholders = ", ".join("?" for _ in terminal)
            clauses.append(f"state NOT IN ({placeholders})")
            params.extend(terminal)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        cursor = await self._db.execute(
            "SELECT connection, job_id, name, script_path, script, partition, requested_nodes,"
            " cpus, gpus, node_list, stdout_path, stderr_path, state, exit_code,"
            f" submitted_ts, updated_ts FROM jobs{where} ORDER BY submitted_ts DESC",
            params,
        )
        rows = await cursor.fetchall()
        keys = [
            "connection",
            "job_id",
            "name",
            "script_path",
            "script",
            "partition",
            "requested_nodes",
            "cpus",
            "gpus",
            "node_list",
            "stdout_path",
            "stderr_path",
            "state",
            "exit_code",
            "submitted_ts",
            "updated_ts",
        ]
        return [dict(zip(keys, r, strict=True)) for r in rows]

    async def get_job(self, connection: str, job_id: str) -> dict[str, object] | None:
        for row in await self.list_jobs(connection):
            if str(row["job_id"]) == job_id:
                return row
        return None

    async def upsert_session(self, **fields: object) -> None:
        """会话登记（alloc 落库 / 状态刷新）；label 与 purpose 必填。"""
        if self._db is None or not fields.get("label") or not fields.get("purpose"):
            return
        from datetime import datetime

        cols = [
            "label",
            "purpose",
            "job_id",
            "node",
            "partition",
            "gpus",
            "cpus",
            "state",
            "backend",
            "pid",
        ]
        row = {c: fields.get(c) for c in cols}
        row["updated_ts"] = datetime.now(UTC).isoformat(timespec="seconds")
        all_cols = [*cols, "updated_ts"]
        updates = ", ".join(f"{c} = excluded.{c}" for c in [*cols[2:], "updated_ts"])
        placeholders = ", ".join("?" for _ in all_cols)
        await self._db.execute(
            f"INSERT INTO sessions ({', '.join(all_cols)})"
            f" VALUES ({placeholders})"
            f" ON CONFLICT(label, purpose) DO UPDATE SET {updates}",
            [row[c] for c in all_cols],
        )
        await self._db.commit()

    async def delete_session(self, label: str, purpose: str) -> bool:
        if self._db is None:
            return False
        cursor = await self._db.execute(
            "DELETE FROM sessions WHERE label = ? AND purpose = ?", (label, purpose)
        )
        await self._db.commit()
        return bool(cursor.rowcount)

    async def list_sessions(self) -> list[dict[str, object]]:
        if self._db is None:
            return []
        cursor = await self._db.execute(
            "SELECT label, purpose, job_id, node, partition, gpus, cpus, state,"
            " backend, pid, updated_ts FROM sessions ORDER BY updated_ts DESC"
        )
        rows = await cursor.fetchall()
        keys = [
            "label",
            "purpose",
            "job_id",
            "node",
            "partition",
            "gpus",
            "cpus",
            "state",
            "backend",
            "pid",
            "updated_ts",
        ]
        return [dict(zip(keys, r, strict=True)) for r in rows]

    async def count_recent_identical(
        self, connection: str, command: str, *, seconds: int = 120
    ) -> int:
        """同一连接同一命令在窗口内的执行次数（agent 循环熔断用）。"""
        if self._db is None:
            return 0
        from datetime import datetime, timedelta

        since = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat(timespec="seconds")
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM exec_records WHERE connection = ? AND command = ? AND ts >= ?",
            (connection, command, since),
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def record(
        self,
        *,
        kind: str,
        connection: str,
        command: str,
        exit_code: int | None = None,
        duration_ms: int | None = None,
        job_id: str | None = None,
        summary: str | None = None,
    ) -> int:
        """写入一条执行记录，返回 id。db 未打开时静默跳过（返回 -1）。"""
        if self._db is None:
            return -1
        from datetime import datetime

        ts = datetime.now(UTC).isoformat(timespec="seconds")
        cursor = await self._db.execute(
            "INSERT INTO exec_records (ts, connection, kind, command, exit_code,"
            " duration_ms, job_id, summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, connection, kind, command, exit_code, duration_ms, job_id, summary),
        )
        await self._db.commit()
        return int(cursor.lastrowid or -1)
