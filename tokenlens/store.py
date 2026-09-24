"""SQLite 存储与聚合查询。

单文件、零外部依赖，写入走 WAL + 独立连接，读多写少场景下足够用。
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                REAL    NOT NULL,
    day               TEXT    NOT NULL,
    hour              INTEGER NOT NULL,
    provider          TEXT,
    upstream          TEXT,
    model             TEXT,
    endpoint          TEXT,
    project           TEXT,
    key_hash          TEXT,
    is_stream         INTEGER DEFAULT 0,
    prompt_tokens     INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens      INTEGER DEFAULT 0,
    cached_tokens     INTEGER DEFAULT 0,
    reasoning_tokens  INTEGER DEFAULT 0,
    cost              REAL    DEFAULT 0,
    cost_source       TEXT,
    latency_ms        REAL    DEFAULT 0,
    ttft_ms           REAL,
    status            INTEGER DEFAULT 200,
    error             TEXT,
    req_bytes         INTEGER DEFAULT 0,
    resp_bytes        INTEGER DEFAULT 0,
    request_id        TEXT
);
CREATE INDEX IF NOT EXISTS idx_req_day    ON requests(day);
CREATE INDEX IF NOT EXISTS idx_req_ts     ON requests(ts);
CREATE INDEX IF NOT EXISTS idx_req_model  ON requests(model);
CREATE INDEX IF NOT EXISTS idx_req_proj   ON requests(project);

CREATE TABLE IF NOT EXISTS budgets (
    scope TEXT PRIMARY KEY,
    limit_usd REAL NOT NULL,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    scope TEXT,
    spent REAL,
    limit_usd REAL,
    message TEXT
);
"""

FIELDS = [
    "ts", "day", "hour", "provider", "upstream", "model", "endpoint", "project",
    "key_hash", "is_stream", "prompt_tokens", "completion_tokens", "total_tokens",
    "cached_tokens", "reasoning_tokens", "cost", "cost_source", "latency_ms",
    "ttft_ms", "status", "error", "req_bytes", "resp_bytes", "request_id",
]


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._local = sqlite3.connect(db_path, check_same_thread=False)
        self._local.row_factory = sqlite3.Row
        self._local.execute("PRAGMA journal_mode=WAL")
        self._init()

    def _init(self):
        self._local.executescript(SCHEMA)
        self._local.commit()

    @contextmanager
    def _write(self):
        """写操作走独立短连接 + busy_timeout，避免与长期读连接争用。"""
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000")
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        finally:
            cur.close()
            conn.close()

    # ---------------- 写入 ----------------
    def insert(self, rec: Dict[str, Any]) -> int:
        data = {k: rec.get(k) for k in FIELDS if k != "id"}
        cols = ",".join(data.keys())
        marks = ",".join("?" for _ in data)
        with self._write() as cur:
            cur.execute(f"INSERT INTO requests ({cols}) VALUES ({marks})", list(data.values()))
            return cur.lastrowid or 0

    def insert_many(self, recs: Iterable[Dict[str, Any]]) -> int:
        recs = list(recs)
        if not recs:
            return 0
        data_list = [{k: r.get(k) for k in FIELDS if k != "id"} for r in recs]
        cols = ",".join(data_list[0].keys())
        marks = ",".join("?" for _ in data_list[0])
        with self._write() as cur:
            cur.executemany(
                f"INSERT INTO requests ({cols}) VALUES ({marks})",
                [list(d.values()) for d in data_list],
            )
            return len(data_list)

    # ---------------- 查询 ----------------
    def _where(self, start: Optional[float], end: Optional[float],
               project: Optional[str], model: Optional[str], provider: Optional[str]) -> tuple:
        clauses, args = [], []
        if start is not None:
            clauses.append("ts >= ?")
            args.append(start)
        if end is not None:
            clauses.append("ts <= ?")
            args.append(end)
        if project:
            clauses.append("project = ?")
            args.append(project)
        if model:
            clauses.append("model = ?")
            args.append(model)
        if provider:
            clauses.append("provider = ?")
            args.append(provider)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", args

    def summary(self, start=None, end=None, project=None, model=None, provider=None) -> Dict[str, Any]:
        w, args = self._where(start, end, project, model, provider)
        row = self._local.execute(f"""
            SELECT
              COUNT(*)                                   AS requests,
              COALESCE(SUM(status >= 400), 0)            AS errors,
              COALESCE(SUM(prompt_tokens), 0)            AS prompt_tokens,
              COALESCE(SUM(completion_tokens), 0)        AS completion_tokens,
              COALESCE(SUM(total_tokens), 0)             AS total_tokens,
              COALESCE(SUM(cached_tokens), 0)            AS cached_tokens,
              COALESCE(SUM(reasoning_tokens), 0)         AS reasoning_tokens,
              COALESCE(SUM(cost), 0)                     AS cost,
              COALESCE(AVG(latency_ms), 0)               AS avg_latency,
              COALESCE(AVG(CASE WHEN is_stream=1 THEN ttft_ms END), 0) AS avg_ttft,
              COALESCE(SUM(is_stream), 0)                AS streamed
            FROM requests{w}
        """, args).fetchone()
        d = dict(row)
        # p95 延迟
        rows = self._local.execute(
            f"SELECT latency_ms FROM requests{w} AND latency_ms > 0 ORDER BY latency_ms", args
        ).fetchall() if w else self._local.execute(
            "SELECT latency_ms FROM requests WHERE latency_ms > 0 ORDER BY latency_ms").fetchall()
        vals = [r[0] for r in rows]
        d["p95_latency"] = vals[int(len(vals) * 0.95) - 1] if len(vals) > 1 else (vals[0] if vals else 0)
        d["avg_latency"] = round(d["avg_latency"] or 0, 1)
        d["avg_ttft"] = round(d["avg_ttft"] or 0, 1)
        d["p95_latency"] = round(d["p95_latency"] or 0, 1)
        d["cost"] = round(d["cost"] or 0, 6)
        d["error_rate"] = round((d["errors"] or 0) / d["requests"], 4) if d["requests"] else 0
        return d

    def timeseries(self, bucket: str = "hour", start=None, end=None,
                   project=None, model=None, provider=None) -> List[Dict[str, Any]]:
        w, args = self._where(start, end, project, model, provider)
        if bucket == "day":
            expr, fmt = "date(ts, 'unixepoch', 'localtime')", "%Y-%m-%d"
        else:
            expr, fmt = "strftime('%Y-%m-%d %H:00', ts, 'unixepoch', 'localtime')", "%Y-%m-%d %H:00"
        rows = self._local.execute(f"""
            SELECT {expr} AS bucket,
                   COUNT(*)                            AS requests,
                   COALESCE(SUM(status >= 400),0)      AS errors,
                   COALESCE(SUM(prompt_tokens),0)      AS prompt_tokens,
                   COALESCE(SUM(completion_tokens),0)  AS completion_tokens,
                   COALESCE(SUM(total_tokens),0)       AS total_tokens,
                   COALESCE(SUM(cost),0)               AS cost,
                   COALESCE(AVG(latency_ms),0)         AS avg_latency
            FROM requests{w}
            GROUP BY bucket ORDER BY bucket
        """, args).fetchall()
        return [dict(r) for r in rows]

    def breakdown(self, field: str, start=None, end=None, project=None, model=None,
                  provider=None, limit: int = 20) -> List[Dict[str, Any]]:
        allowed = {"model", "project", "provider", "endpoint", "day"}
        if field not in allowed:
            field = "model"
        w, args = self._where(start, end, project, model, provider)
        rows = self._local.execute(f"""
            SELECT COALESCE({field}, 'unknown') AS name,
                   COUNT(*)                           AS requests,
                   COALESCE(SUM(status >= 400),0)    AS errors,
                   COALESCE(SUM(prompt_tokens),0)    AS prompt_tokens,
                   COALESCE(SUM(completion_tokens),0) AS completion_tokens,
                   COALESCE(SUM(total_tokens),0)     AS total_tokens,
                   COALESCE(SUM(cost),0)             AS cost,
                   COALESCE(AVG(latency_ms),0)       AS avg_latency
            FROM requests{w}
            GROUP BY {field}
            ORDER BY cost DESC
            LIMIT ?
        """, args + [limit]).fetchall()
        return [dict(r) for r in rows]

    def recent(self, limit: int = 50, start=None, end=None, project=None,
               model=None, provider=None) -> List[Dict[str, Any]]:
        w, args = self._where(start, end, project, model, provider)
        rows = self._local.execute(
            f"SELECT * FROM requests{w} ORDER BY id DESC LIMIT ?", args + [limit]
        ).fetchall()
        return [dict(r) for r in rows]

    def cost_in_range(self, start: float, end: float) -> float:
        row = self._local.execute(
            "SELECT COALESCE(SUM(cost),0) AS c FROM requests WHERE ts >= ? AND ts <= ?",
            (start, end)).fetchone()
        return float(row["c"]) if row else 0.0

    def live_stats(self, since: float) -> Dict[str, float]:
        """最近 since 秒的实时聚合（修复：不再拉明细到内存过滤）。"""
        row = self._local.execute(
            "SELECT COUNT(*) AS requests, "
            "       COALESCE(SUM(status >= 400),0) AS errors, "
            "       COALESCE(SUM(total_tokens),0)  AS tokens, "
            "       COALESCE(SUM(cost),0)          AS cost "
            "FROM requests WHERE ts >= ?",
            (since,)).fetchone()
        return dict(row) if row else {"requests": 0, "errors": 0, "tokens": 0, "cost": 0}

    def distinct(self, field: str) -> List[str]:
        if field not in {"model", "project", "provider", "endpoint"}:
            return []
        rows = self._local.execute(
            f"SELECT DISTINCT {field} FROM requests WHERE {field} IS NOT NULL ORDER BY {field}"
        ).fetchall()
        return [r[0] for r in rows if r[0]]

    def budget_get(self, scope: str) -> Optional[float]:
        row = self._local.execute("SELECT limit_usd FROM budgets WHERE scope = ?", (scope,)).fetchone()
        return row["limit_usd"] if row else None

    def budget_set(self, scope: str, limit_usd: float):
        with self._write() as cur:
            cur.execute(
                "INSERT INTO budgets(scope, limit_usd, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(scope) DO UPDATE SET limit_usd=excluded.limit_usd, updated_at=excluded.updated_at",
                (scope, limit_usd, time.time()))

    def alert_recent(self, scope: str, since: float) -> bool:
        row = self._local.execute(
            "SELECT COUNT(*) AS c FROM alerts WHERE scope = ? AND ts >= ?", (scope, since)).fetchone()
        return (row["c"] if row else 0) > 0

    def alerts_list(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._local.execute(
            "SELECT * FROM alerts ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def alert_add(self, scope: str, spent: float, limit_usd: float, message: str):
        with self._write() as cur:
            cur.execute(
                "INSERT INTO alerts(ts, scope, spent, limit_usd, message) VALUES(?,?,?,?,?)",
                (time.time(), scope, spent, limit_usd, message))

    def clear(self):
        with self._write() as cur:
            cur.execute("DELETE FROM requests")

    def close(self):
        try:
            self._local.close()
        except Exception:
            pass
