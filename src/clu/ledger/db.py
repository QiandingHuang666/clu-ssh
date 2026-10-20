"""SQLite 留痕：建表与写入（exec_records 单表，DESIGN §7）。检索在 M2 的 query.py。"""

from datetime import UTC
from pathlib import Path

import aiosqlite

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
