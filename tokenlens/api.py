"""仪表盘后端 API：为前端提供聚合数据。"""

from __future__ import annotations

import csv
import io
import time
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel

from .meter import CostCalculator, Meter, day_bounds, month_bounds
from .pricing import PricingTable
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


class BudgetIn(BaseModel):
    scope: str
    limit: float


class ConfigPatch(BaseModel):
    budget_daily: Optional[float] = None
    budget_monthly: Optional[float] = None
    usd_cny_rate: Optional[float] = None
    webhook_url: Optional[str] = None
    webhook_type: Optional[str] = None
    enforce_budget: Optional[bool] = None
    enforce_budget_ratio: Optional[float] = None
    default_upstream: Optional[str] = None
    upstreams: Optional[Dict[str, str]] = None
    pricing_overrides: Optional[Dict[str, Dict[str, float]]] = None


def create_api(store: Store, meter: Meter) -> APIRouter:
    cfg = meter.cfg

    def require_token(request: Request):
        """仪表盘访问令牌：config 配了 dashboard_token 才生效，默认不鉴权。"""
        tok = cfg.dashboard_token
        if not tok:
            return None
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer ") and auth[7:].strip() == tok:
            return None
        if request.query_params.get("token") == tok:
            return None
        raise HTTPException(401, "需要访问令牌（config.json 的 dashboard_token）")

    router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])

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
        st = store.live_stats(now - window)
        n = st["requests"]
        return {
            "window": window,
            "requests": n,
            "rpm": round(n / (window / 60), 2),
            "tokens": int(st["tokens"] or 0),
            "cost": round(float(st["cost"] or 0), 6),
            "errors": int(st["errors"] or 0),
        }

    @router.get("/health")
    def health():
        return {"ok": True, "ts": time.time()}

    # ---------- 设置 ----------
    def _public_config(cfg) -> Dict[str, Any]:
        """对外可见的配置（不暴露 db_path / token 等敏感项）。"""
        return {
            "budget_daily": cfg.budget_daily,
            "budget_monthly": cfg.budget_monthly,
            "usd_cny_rate": cfg.usd_cny_rate,
            "webhook_url": cfg.webhook_url,
            "webhook_type": cfg.webhook_type,
            "enforce_budget": cfg.enforce_budget,
            "enforce_budget_ratio": cfg.enforce_budget_ratio,
            "default_upstream": cfg.default_upstream,
            "upstreams": cfg.upstreams,
            "pricing_overrides": cfg.pricing_overrides,
        }

    @router.get("/config")
    def get_config():
        return _public_config(meter.cfg)

    @router.put("/config")
    def put_config(body: ConfigPatch):
        patch = body.model_dump(exclude_none=True)
        cfg = meter.cfg
        for k, v in patch.items():
            setattr(cfg, k, v)
        cfg.save()
        # 价格覆盖变更后重建定价器，立即生效
        if "pricing_overrides" in patch:
            meter.pricing = PricingTable(cfg.pricing_overrides)
            meter.calc = CostCalculator(meter.pricing, cfg.cached_discount, cfg.cached_discounts)
        return _public_config(cfg)

    # ---------- 导出 ----------
    @router.get("/export")
    def export(rng: str = "30d", project: Optional[str] = None,
               model: Optional[str] = None, provider: Optional[str] = None):
        s, e = parse_range(rng)
        rows = store.recent(limit=100000, start=s, end=e,
                            project=project, model=model, provider=provider)
        if not rows:
            raise HTTPException(404, "所选范围暂无数据可导出")
        buf = io.StringIO()
        fieldnames = list(rows[0].keys())
        w = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            r = dict(r)
            r["ts"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"]))
            w.writerow(r)
        return Response(
            content=buf.getvalue(), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="tokenlens-usage.csv"'})

    # ---------- 告警历史 ----------
    @router.get("/alerts")
    def alerts(limit: int = 50):
        return store.alerts_list(min(limit, 200))

    # ---------- 数据管理 ----------
    @router.post("/reset")
    def reset():
        store.clear()
        return {"ok": True}

    return router
