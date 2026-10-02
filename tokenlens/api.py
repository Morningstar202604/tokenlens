"""仪表盘后端 API：为前端提供聚合数据。"""

from __future__ import annotations

import csv
import hmac
import io
import json
import time
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from starlette.responses import StreamingResponse
from pydantic import BaseModel, Field

from .meter import CostCalculator, Meter, day_bounds, month_bounds
from .pricing import PricingTable
from .store import Store

# 时间范围上限：365 天（8760 小时），配合 _fill_gaps 的桶数硬上限防内存放大
MAX_RANGE_HOURS = 8760
MAX_RANGE_DAYS = 365


def parse_range(rng: str) -> tuple:
    """解析时间范围；非法值抛 ValueError（API 层转 400，CLI 层友好提示）。"""
    now = time.time()
    if rng == "today":
        return day_bounds(now)
    if rng == "yesterday":
        s, e = day_bounds(now)
        return s - 86400, s
    if rng == "month":
        return month_bounds(now)
    if rng == "all":
        return None, None
    if rng.endswith("h"):
        try:
            hours = int(rng[:-1])
        except ValueError:
            raise ValueError(f"invalid range: {rng}")
        if hours < 1 or hours > MAX_RANGE_HOURS:
            raise ValueError(f"range 超界（1h ~ {MAX_RANGE_HOURS}h）: {rng}")
        return now - hours * 3600, now
    if rng.endswith("d"):
        try:
            days = int(rng[:-1])
        except ValueError:
            raise ValueError(f"invalid range: {rng}")
        if days < 1 or days > MAX_RANGE_DAYS:
            raise ValueError(f"range 超界（1d ~ {MAX_RANGE_DAYS}d）: {rng}")
        return now - days * 86400, now
    raise ValueError(f"invalid range: {rng}（可用: today/yesterday/month/all/Nh/Nd）")


def _range_or_400(rng: str) -> tuple:
    try:
        return parse_range(rng)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


class BudgetIn(BaseModel):
    scope: str
    limit: float = Field(ge=0)


class ConfigPatch(BaseModel):
    budget_daily: Optional[float] = Field(None, ge=0)
    budget_monthly: Optional[float] = Field(None, ge=0)
    usd_cny_rate: Optional[float] = Field(None, ge=0)
    webhook_url: Optional[str] = None
    webhook_type: Optional[str] = None
    enforce_budget: Optional[bool] = None
    enforce_budget_ratio: Optional[float] = Field(None, gt=0, le=1)
    default_upstream: Optional[str] = None
    upstreams: Optional[Dict[str, str]] = None
    pricing_overrides: Optional[Dict[str, Dict[str, float]]] = None
    allow_private_upstreams: Optional[bool] = None
    retention_days: Optional[int] = Field(None, ge=0)
    key_aliases: Optional[Dict[str, str]] = None


def _fill_gaps(rows, bucket: str, start, end, rng: str):
    """把没有请求的时间桶补成 0，让趋势图连续（符合人看图时对连续时间的预期）。"""
    if not rows:
        return rows
    if start is None or end is None:  # all 范围不补（跨度不定）
        return rows
    step = 3600 if bucket == "hour" else 86400
    fmt = "%Y-%m-%d %H:00" if bucket == "hour" else "%Y-%m-%d"
    # 桶数硬上限：超过说明范围异常放大，返回原始聚合（不补桶），防内存炸弹
    if (end - start) // step + 2 > 10000:
        return rows
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
        if auth.lower().startswith("bearer ") and hmac.compare_digest(auth[7:].strip(), tok):
            return None
        qtok = request.query_params.get("token")
        if qtok is not None and hmac.compare_digest(qtok, tok):
            return None
        raise HTTPException(401, "需要访问令牌（config.json 的 dashboard_token）")

    def require_write(request: Request):
        """写操作保护（两层）：
        1. 未设 token 时必须携带自定义头 x-tokenlens-write: 1 —— 浏览器跨域
           简单请求带不了自定义头，从机制上挡住「任意网页 fetch 清库」的 CSRF；
        2. 服务对外监听且未设 token 时直接禁止写（部署后必须先配 token）。
        """
        if not cfg.dashboard_token:
            if request.headers.get("x-tokenlens-write") != "1":
                raise HTTPException(
                    403, "写操作需要自定义头 x-tokenlens-write: 1（防网页 CSRF）")
            if cfg.host not in ("127.0.0.1", "localhost", "::1", "[::1]"):
                raise HTTPException(
                    401, "服务对外监听但未设置 dashboard_token，已禁止修改类操作；"
                         "请在 config.json 中配置访问令牌后重试")
        return None

    router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])

    @router.get("/stats")
    def stats(rng: str = "today", project: Optional[str] = None,
              model: Optional[str] = None, provider: Optional[str] = None,
              session: Optional[str] = None):
        s, e = _range_or_400(rng)
        data = store.summary(s, e, project, model, provider, session)
        data["range"] = rng
        data["generated_at"] = datetime.now().isoformat(timespec="seconds")
        return data

    @router.get("/timeseries")
    def timeseries(rng: str = "today", bucket: str = "hour", project: Optional[str] = None,
                   model: Optional[str] = None, provider: Optional[str] = None,
                   session: Optional[str] = None):
        s, e = _range_or_400(rng)
        if rng in ("30d", "90d", "all", "month"):
            bucket = "day"
        rows = store.timeseries(bucket, s, e, project, model, provider, session)
        return _fill_gaps(rows, bucket, s, e, rng)

    @router.get("/breakdown")
    def breakdown(field: str = "model", rng: str = "today", project: Optional[str] = None,
                  model: Optional[str] = None, provider: Optional[str] = None, limit: int = 20,
                  session: Optional[str] = None):
        if field not in {"model", "project", "provider", "endpoint", "day", "key_hash"}:
            raise HTTPException(400, "unsupported field")
        limit = min(max(limit, 1), 100)  # SQLite 负数 LIMIT = 无限制，必须钳位
        s, e = _range_or_400(rng)
        rows = store.breakdown(field, s, e, project, model, provider, limit, session)
        if field == "key_hash":
            # 密钥指纹 → 应用别名，让「谁在花钱」直接显示应用名
            aliases = cfg.key_aliases or {}
            rows = [{**r, "name": aliases.get(r["name"], r["name"])} for r in rows]
        return rows

    @router.get("/recent")
    def recent(limit: int = 50, rng: str = "today", project: Optional[str] = None,
               model: Optional[str] = None, provider: Optional[str] = None,
               session: Optional[str] = None):
        s, e = _range_or_400(rng)
        rows = store.recent(min(max(limit, 1), 500), s, e, project, model, provider, session)
        aliases = cfg.key_aliases or {}
        return [{**r, "app": aliases.get(r.get("key_hash") or "", "")} for r in rows]

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
            "keys": [k for k in store.distinct("key_hash") if k and k != "anonymous"][:100],
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
        window = min(max(window, 1), 86400)  # 客户端可控，钳位防全表扫描
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
        """对外可见的配置（不暴露 db_path / token 等敏感项）。
        webhook_url 常含机器人 access_token 凭据：对外监听且未鉴权时置空。"""
        mask_webhook = not cfg.dashboard_token and cfg.host not in ("127.0.0.1", "localhost", "::1")
        return {
            "budget_daily": cfg.budget_daily,
            "budget_monthly": cfg.budget_monthly,
            "usd_cny_rate": cfg.usd_cny_rate,
            "webhook_url": None if mask_webhook else cfg.webhook_url,
            "webhook_type": cfg.webhook_type,
            "enforce_budget": cfg.enforce_budget,
            "enforce_budget_ratio": cfg.enforce_budget_ratio,
            "default_upstream": cfg.default_upstream,
            "upstreams": cfg.upstreams,
            "pricing_overrides": cfg.pricing_overrides,
            "allow_private_upstreams": cfg.allow_private_upstreams,
            "retention_days": cfg.retention_days,
            "key_aliases": cfg.key_aliases,
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
        s, e = _range_or_400(rng)
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
        s, e = _range_or_400(rng)
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
        return store.alerts_list(min(max(limit, 1), 200))

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

    @router.get("/apps")
    def apps_status():
        """本机 AI 应用清单：onboard 探测状态 + 各本地扫描器已入库记录数。"""
        from .localscan import SCAN_KEY_HASHES
        from .onboard import detect
        rows = detect(cfg)
        for r in rows:
            metered = r["id"] in SCAN_KEY_HASHES
            r["metered"] = metered
            r["records"] = store.count_by_key(SCAN_KEY_HASHES[r["id"]]) if metered else 0
        return {"apps": rows}

    @router.post("/scan-local", dependencies=[Depends(require_write)])
    def scan_local():
        from .localscan import import_local
        result = import_local(cfg, store)
        return {"ok": True,
                "imported": sum(v["imported"] for v in result.values()),
                "skipped": sum(v["skipped"] for v in result.values()),
                "detail": result}

    return router
