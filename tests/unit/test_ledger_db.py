"""留痕库测试：建表与写入。"""

import aiosqlite

from clu.ledger.db import Ledger


async def test_record_and_read_back(tmp_path: object) -> None:
    path = tmp_path / "ledger.sqlite3"  # type: ignore[arg-type]
    ledger = Ledger(path)
    await ledger.open()
    try:
        row_id = await ledger.record(
            kind="exec", connection="gzu", command="true", exit_code=0, duration_ms=12
        )
        assert row_id > 0
    finally:
        await ledger.close()
    async with aiosqlite.connect(path) as db:  # type: ignore[arg-type]
        cursor = await db.execute("SELECT connection, kind, exit_code FROM exec_records")
        row = await cursor.fetchone()
    assert row == ("gzu", "exec", 0)


async def test_record_without_open_returns_minus_one(tmp_path: object) -> None:
    ledger = Ledger(tmp_path / "x.sqlite3")  # type: ignore[arg-type]
    assert await ledger.record(kind="exec", connection="a", command="c") == -1
