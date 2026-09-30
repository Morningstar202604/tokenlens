"""仪表盘后端 API：为前端提供聚合数据。"""

from __future__ import annotations

import csv
import io
import json
import time
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from starlette.responses import StreamingResponse
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
        try:
            hours = int(rng[:-1])
        except ValueError:
            raise HTTPException(400, f"invalid range: {rng}")
        return now - hours * 3600, now
    if rng.endswith("d"):
        try:
            days = int(rng[:-1])
        except ValueError:
            raise HTTPException(400, f"invalid range: {rng}")
        return now - days * 86400, now
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
    allow_private_upstreams: Optional[bool] = None
    retention_days: Optional[int] = None


def _fill_gaps(rows, bucket: str, start, end, rng: str):
    """把没有请求的时间桶补成 0，让趋势图连续（符合人看图时对连续时间的预期）。"""
    if not rows:
        return rows
    if start is None or end is None:  # all 范围不补（跨度不定）
        return rows
    step = 3600 if bucket == "hour" else 86400
    fmt = "%Y-%m-%d %H:00" if bucket == "hour" else "%Y-%m-%d"
    got = {r["bucket"]: r for r in rows}
    filled = []
    t = start - (start % step)
    while t < end:
        key = datetime.fromtimestamp(t).strftime(fmt)
        r = got.get(key)
        if r is None:
            r = {"bucket": key, "requests": 0, "errors": 0, "prompt_tokens": 0,
                 "completion_tokens": 0, "total_tokens": 0, "cost": 0.0, "avg_latency": 0.0}
        filled.append(r)
        t += step
    return filled


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

    def require_write(request: Request):
        """写操作保护：对外监听且未设置访问令牌时，禁止修改类操作。

        本地默认（127.0.0.1）保持开箱即用；一旦监听非回环地址，
        未配 token 的部署不允许任何人改配置/预算/清数据。
        """
        if not cfg.dashboard_token and cfg.host not in ("127.0.0.1", "localhost", "::1", "[::1]"):
            raise HTTPException(
                401, "服务对外监听但未设置 dashboard_token，已禁止修改类操作；"
                     "请在 config.json 中配置访问令牌后重试")
        return None

    router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])

    @router.get("/stats")
    def stats(rng: str = "today", project: Optional[str] = None,
              model: Optional[str] = None, provider: Optional[str] = None,
              session: Optional[str] = None):
        s, e = parse_range(rng)
        data = store.summary(s, e, project, model, provider, session)
        data["range"] = rng
        data["generated_at"] = datetime.now().isoformat(timespec="seconds")
        return data

    @router.get("/timeseries")
    def timeseries(rng: str = "today", bucket: str = "hour", project: Optional[str] = None,
                   model: Optional[str] = None, provider: Optional[str] = None,
                   session: Optional[str] = None):
        s, e = parse_range(rng)
        if rng in ("30d", "90d", "all", "month"):
            bucket = "day"
        rows = store.timeseries(bucket, s, e, project, model, provider, session)
        return _fill_gaps(rows, bucket, s, e, rng)

    @router.get("/breakdown")
    def breakdown(field: str = "model", rng: str = "today", project: Optional[str] = None,
                  model: Optional[str] = None, provider: Optional[str] = None, limit: int = 20,
                  session: Optional[str] = None):
        if field not in {"model", "project", "provider", "endpoint", "day"}:
            raise HTTPException(400, "unsupported field")
        s, e = parse_range(rng)
        return store.breakdown(field, s, e, project, model, provider, limit, session)

    @router.get("/recent")
    def recent(limit: int = 50, rng: str = "today", project: Optional[str] = None,
               model: Optional[str] = None, provider: Optional[str] = None,
               session: Optional[str] = None):
        s, e = parse_range(rng)
        return store.recent(min(limit, 500), s, e, project, model, provider, session)

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
            "sessions": [s for s in store.distinct("session_id") if s][:100],
        }

    @router.post("/budget", dependencies=[Depends(require_write)])
    def set_budget(body: BudgetIn):
        if body.scope not in ("daily", "monthly"):
            raise HTTPException(400, "scope must be daily or monthly")
        if body.limit < 0:
            raise HTTPException(400, "limit must be >= 0")
        if body.scope == "daily":
            meter.cfg.budget_daily = body.limit
        else:
            meter.cfg.budget_monthly = body.limit
        meter.cfg.save()  # 预算持久化唯一入口：config.json
        return meter.budget_status()

    @router.get("/live")
    def live(window: int = 60):
        """最近 N 秒的速率，用于仪表盘顶部的实时感。"""
        if window <= 0:
            window = 1  # 客户端可控参数，非法值不抛 500
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
            "allow_private_upstreams": cfg.allow_private_upstreams,
            "retention_days": cfg.retention_days,
        }

    @router.get("/config")
    def get_config():
        return _public_config(meter.cfg)

    @router.put("/config", dependencies=[Depends(require_write)])
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
               model: Optional[str] = None, provider: Optional[str] = None,
               session: Optional[str] = None):
        s, e = parse_range(rng)
        first_page = store.export_page(1, 0, s, e, project, model, provider, session)
        if not first_page:
            raise HTTPException(404, "所选范围暂无数据可导出")
        first = first_page[0]

        def _sanitize(r: Dict[str, Any]) -> Dict[str, Any]:
            r = dict(r)
            r["ts"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"]))
            # CSV 公式注入防护：以 = + - @ 制表符/回车开头的字段前置单引号，
            # 避免 Excel/WPS 把用户可控字段当公式执行
            for k, v in r.items():
                if isinstance(v, str) and v and v[0] in ("=", "+", "-", "@", "\t", "\r"):
                    r[k] = "'" + v
            return r

        def gen():
            fieldnames = list(first.keys())
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
            w.writeheader()
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate()
            offset = 0
            while True:
                rows = store.export_page(5000, offset, s, e, project, model, provider, session)
                if not rows:
                    break
                offset += len(rows)
                for r in rows:
                    w.writerow(_sanitize(r))
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate()

        return StreamingResponse(
            gen(), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="tokenlens-usage.csv"'})

    @router.get("/export-jsonl")
    def export_jsonl(rng: str = "30d", project: Optional[str] = None,
                     model: Optional[str] = None, provider: Optional[str] = None,
                     session: Optional[str] = None):
        """JSONL 导出：每行一个 JSON 对象，天然规避 CSV 公式注入，适合程序化消费。"""
        s, e = parse_range(rng)
        if not store.export_page(1, 0, s, e, project, model, provider, session):
            raise HTTPException(404, "所选范围暂无数据可导出")

        def gen():
            offset = 0
            while True:
                rows = store.export_page(5000, offset, s, e, project, model, provider, session)
                if not rows:
                    break
                offset += len(rows)
                for r in rows:
                    r = dict(r)
                    r["ts"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"]))
                    yield json.dumps(r, ensure_ascii=False) + "\n"

        return StreamingResponse(
            gen(), media_type="application/x-ndjson",
            headers={"Content-Disposition": 'attachment; filename="tokenlens-usage.jsonl"'})

    # ---------- 告警历史 ----------
    @router.get("/alerts")
    def alerts(limit: int = 50):
        return store.alerts_list(min(limit, 200))

    # ---------- 数据管理 ----------
    @router.post("/reset", dependencies=[Depends(require_write)])
    def reset():
        store.clear()
        return {"ok": True}

    @router.post("/seed", dependencies=[Depends(require_write)])
    def seed_demo(n: int = Query(300, ge=1, le=5000)):
        from .demo import seed as seed_db
        count = seed_db(store, meter, n=n, days=7)
        return {"ok": True, "inserted": count}

    return router
