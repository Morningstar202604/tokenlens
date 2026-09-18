"""仪表盘后端 API：为前端提供聚合数据。"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from .meter import Meter, day_bounds, month_bounds
from .store import Store


def parse_range(rng: str) -> tuple:
    now = time.time()
    if rng == "today":
        return day_bounds(now)
    if rng == "yesterday":
        s, e = day_bounds(now)
        return s - 86400, s
    if rng.endswith("h"):
        return now - int(rng[:-1]) * 3600, now
    if rng.endswith("d"):
        return now - int(rng[:-1]) * 86400, now
    if rng == "month":
        return month_bounds(now)
    if rng == "all":
        return None, None
    return day_bounds(now)


def create_api(store: Store, meter: Meter) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/stats")
    def stats(rng: str = "today", project: Optional[str] = None,
              model: Optional[str] = None, provider: Optional[str] = None):
        s, e = parse_range(rng)
        data = store.summary(s, e, project, model, provider)
        data["range"] = rng
        data["generated_at"] = datetime.now().isoformat(timespec="seconds")
        return data

    @router.get("/timeseries")
    def timeseries(rng: str = "today", bucket: str = "hour", project: Optional[str] = None,
                   model: Optional[str] = None, provider: Optional[str] = None):
        s, e = parse_range(rng)
        if rng in ("30d", "90d", "all", "month"):
            bucket = "day"
        return store.timeseries(bucket, s, e, project, model, provider)

    @router.get("/breakdown")
    def breakdown(field: str = "model", rng: str = "today", project: Optional[str] = None,
                  model: Optional[str] = None, provider: Optional[str] = None, limit: int = 20):
        if field not in {"model", "project", "provider", "endpoint", "day"}:
            raise HTTPException(400, "unsupported field")
        s, e = parse_range(rng)
        return store.breakdown(field, s, e, project, model, provider, limit)

    @router.get("/recent")
    def recent(limit: int = 50, rng: str = "today", project: Optional[str] = None,
               model: Optional[str] = None, provider: Optional[str] = None):
        s, e = parse_range(rng)
        return store.recent(min(limit, 500), s, e, project, model, provider)

    @router.get("/budget")
    def budget():
        return meter.budget_status()

    @router.get("/filters")
    def filters():
        return {
            "models": store.distinct("model"),
            "projects": store.distinct("project"),
            "providers": store.distinct("provider"),
            "endpoints": store.distinct("endpoint"),
        }

    class BudgetIn(BaseModel):
        scope: str
        limit: float

    @router.post("/budget")
    def set_budget(body: BudgetIn):
        if body.scope not in ("daily", "monthly"):
            raise HTTPException(400, "scope must be daily or monthly")
        store.budget_set(body.scope, body.limit)
        if body.scope == "daily":
            meter.cfg.budget_daily = body.limit
        else:
            meter.cfg.budget_monthly = body.limit
        return meter.budget_status()

    @router.get("/live")
    def live(window: int = 60):
        """最近 N 秒的速率，用于仪表盘顶部的实时感。"""
        now = time.time()
        rows = store.recent(200)
        win = [r for r in rows if r["ts"] >= now - window]
        tokens = sum(r["total_tokens"] for r in win)
        cost = sum(r["cost"] for r in win)
        return {
            "window": window,
            "requests": len(win),
            "rpm": round(len(win) / (window / 60), 2),
            "tokens": tokens,
            "cost": round(cost, 6),
            "errors": sum(1 for r in win if r["status"] >= 400),
        }

    @router.get("/health")
    def health():
        return {"ok": True, "ts": time.time()}

    return router
