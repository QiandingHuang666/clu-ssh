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
CREATE TABLE IF NOT EXISTS sessions (
    label       TEXT NOT NULL,
    purpose     TEXT NOT NULL,
    job_id      TEXT,
    node        TEXT,
    partition   TEXT,
    gpus        INTEGER,
    cpus        INTEGER,
    state       TEXT NOT NULL,
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
            await self._db.executescript(SCHEMA)  # 多语句必须 executescript
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

    async def upsert_session(self, **fields: object) -> None:
        """会话登记（alloc 落库 / 状态刷新）；label 与 purpose 必填。"""
        if self._db is None or not fields.get("label") or not fields.get("purpose"):
            return
        from datetime import datetime

        cols = ["label", "purpose", "job_id", "node", "partition", "gpus", "cpus", "state"]
        row = {c: fields.get(c) for c in cols}
        row["updated_ts"] = datetime.now(UTC).isoformat(timespec="seconds")
        updates = ", ".join(f"{c} = ?" for c in [*cols[2:], "updated_ts"])
        await self._db.execute(
            f"INSERT INTO sessions ({', '.join(cols)}, updated_ts)"
            f" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            f" ON CONFLICT(label, purpose) DO UPDATE SET {updates}",
            [
                row["label"],
                row["purpose"],
                row["job_id"],
                row["node"],
                row["partition"],
                row["gpus"],
                row["cpus"],
                row["state"],
                row["updated_ts"],
                row["job_id"],
                row["node"],
                row["partition"],
                row["gpus"],
                row["cpus"],
                row["state"],
                row["updated_ts"],
            ],
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
            "SELECT label, purpose, job_id, node, partition, gpus, cpus, state, updated_ts"
            " FROM sessions ORDER BY updated_ts DESC"
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
            "updated_ts",
        ]
        return [dict(zip(keys, r, strict=True)) for r in rows]

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
