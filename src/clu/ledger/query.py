"""留痕结构化检索（DESIGN §6：ledger_query 不暴露裸 SQL）。"""

from dataclasses import dataclass

import aiosqlite


@dataclass(frozen=True)
class LedgerFilter:
    since: str | None = None  # UTC ISO8601 下界（含）
    until: str | None = None  # UTC ISO8601 上界（含）
    connection: str | None = None
    kind: str | None = None
    keyword: str | None = None  # command/summary 模糊匹配
    job_id: str | None = None
    limit: int = 100


def build_where(f: LedgerFilter) -> tuple[str, list[object]]:
    """构造 WHERE 子句与参数（全部参数化，无注入面）。"""
    clauses: list[str] = []
    params: list[object] = []
    if f.since:
        clauses.append("ts >= ?")
        params.append(f.since)
    if f.until:
        clauses.append("ts <= ?")
        params.append(f.until)
    if f.connection:
        clauses.append("connection = ?")
        params.append(f.connection)
    if f.kind:
        clauses.append("kind = ?")
        params.append(f.kind)
    if f.keyword:
        clauses.append("(command LIKE ? OR summary LIKE ?)")
        like = f"%{f.keyword}%"
        params.extend([like, like])
    if f.job_id:
        clauses.append("job_id = ?")
        params.append(f.job_id)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


async def query_ledger(db: aiosqlite.Connection, f: LedgerFilter) -> list[dict[str, object]]:
    where, params = build_where(f)
    sql = (
        "SELECT id, ts, connection, kind, command, exit_code, duration_ms, job_id, summary"
        f" FROM exec_records{where} ORDER BY id DESC LIMIT ?"
    )
    cursor = await db.execute(sql, [*params, f.limit])
    rows = await cursor.fetchall()
    keys = [
        "id",
        "ts",
        "connection",
        "kind",
        "command",
        "exit_code",
        "duration_ms",
        "job_id",
        "summary",
    ]
    return [dict(zip(keys, row, strict=True)) for row in rows]
