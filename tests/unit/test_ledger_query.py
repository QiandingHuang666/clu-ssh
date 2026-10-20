"""留痕检索测试：过滤与排序。"""

from clu.ledger.db import Ledger
from clu.ledger.query import LedgerFilter, build_where


async def test_filters(tmp_path) -> None:
    ledger = Ledger(tmp_path / "l.sqlite3")
    await ledger.open()
    try:
        await ledger.record(kind="exec", connection="gzu", command="squeue -u me", exit_code=0)
        await ledger.record(kind="submit", connection="gzu", command="#!/bin/bash", job_id="42")
        await ledger.record(kind="exec", connection="aliyun", command="df -h", exit_code=0)
    finally:
        await ledger.close()

    import aiosqlite

    async with aiosqlite.connect(tmp_path / "l.sqlite3") as db:
        from clu.ledger.query import query_ledger

        rows = await query_ledger(db, LedgerFilter(connection="gzu"))
        assert len(rows) == 2
        rows = await query_ledger(db, LedgerFilter(kind="submit"))
        assert rows[0]["job_id"] == "42"
        rows = await query_ledger(db, LedgerFilter(keyword="squeue"))
        assert len(rows) == 1
        rows = await query_ledger(db, LedgerFilter(job_id="42"))
        assert rows[0]["kind"] == "submit"
        rows = await query_ledger(db, LedgerFilter(limit=1))
        assert len(rows) == 1  # 时间倒序：最后写入的在前
        assert rows[0]["connection"] == "aliyun"


def test_build_where_parameterized() -> None:
    where, params = build_where(LedgerFilter(connection="a", keyword="x"))
    assert "connection = ?" in where
    assert "(command LIKE ? OR summary LIKE ?)" in where
    assert params == ["a", "%x%", "%x%"]
