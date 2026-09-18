"""计量核心：把一次调用组装成记录、计算成本、检查预算并告警。"""

from __future__ import annotations

import calendar
import hashlib
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from .config import Config
from .pricing import CostCalculator, PricingTable
from .store import Store


def provider_from_url(url: str) -> str:
    if not url:
        return "unknown"
    host = url.lower()
    for key in ("openai", "anthropic", "deepseek", "moonshot", "bigmodel", "dashscope",
                "volces", "volcengine", "ark", "siliconflow", "googleapis", "x.ai",
                "groq", "together", "ollama", "localhost", "127.0.0.1", "openrouter"):
        if key in host:
            return "openai" if key == "openrouter" and False else key
    return "custom"


def key_hash(api_key: Optional[str]) -> str:
    if not api_key:
        return "anonymous"
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]


def day_bounds(now: Optional[float] = None) -> tuple:
    now = now or time.time()
    d = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    return d.timestamp(), d.timestamp() + 86400


def month_bounds(now: Optional[float] = None) -> tuple:
    now = now or time.time()
    d = datetime.fromtimestamp(now)
    start = d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    _, last = calendar.monthrange(d.year, d.month)
    end = start + timedelta(days=last)
    return start.timestamp(), end.timestamp()


class Meter:
    """所有用量记录的入口：代理与 SDK 都通过它落库。"""

    def __init__(self, store: Store, cfg: Config, pricing: Optional[PricingTable] = None):
        self.store = store
        self.cfg = cfg
        self.pricing = pricing or PricingTable(cfg.pricing_overrides)
        self.calc = CostCalculator(self.pricing, cfg.cached_discount)

    # ---------- 记录 ----------
    def record(self, **kw) -> Dict[str, Any]:
        ts = kw.get("ts") or time.time()
        dt = datetime.fromtimestamp(ts)
        prompt = int(kw.get("prompt_tokens") or 0)
        completion = int(kw.get("completion_tokens") or 0)
        cached = int(kw.get("cached_tokens") or 0)
        model = kw.get("model")
        cost = kw.get("cost")
        if cost is None:
            cost = self.calc.compute(model, prompt, completion, cached)
            cost_source = "estimated" if kw.get("estimated") else "reported"
        else:
            cost_source = kw.get("cost_source") or "reported"
        rec = {
            "ts": ts,
            "day": dt.strftime("%Y-%m-%d"),
            "hour": dt.hour,
            "provider": kw.get("provider") or "custom",
            "upstream": kw.get("upstream") or "",
            "model": model or "unknown",
            "endpoint": kw.get("endpoint") or "",
            "project": kw.get("project") or "default",
            "key_hash": kw.get("key_hash") or "anonymous",
            "is_stream": 1 if kw.get("is_stream") else 0,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": int(kw.get("total_tokens") or (prompt + completion)),
            "cached_tokens": cached,
            "reasoning_tokens": int(kw.get("reasoning_tokens") or 0),
            "cost": round(float(cost), 8),
            "cost_source": cost_source,
            "latency_ms": round(float(kw.get("latency_ms") or 0), 1),
            "ttft_ms": round(float(kw["ttft_ms"]), 1) if kw.get("ttft_ms") else None,
            "status": int(kw.get("status") or 200),
            "error": (kw.get("error") or "")[:500],
            "req_bytes": int(kw.get("req_bytes") or 0),
            "resp_bytes": int(kw.get("resp_bytes") or 0),
            "request_id": kw.get("request_id") or "",
        }
        try:
            self.store.insert(rec)
        except Exception as exc:
            print(f"[tokenlens] 写入失败: {exc}")
        self.check_budget()
        return rec

    # ---------- 预算 ----------
    def budget_status(self) -> Dict[str, Any]:
        out = {}
        for scope, limit, bounds in (
            ("daily", self.cfg.budget_daily, day_bounds()),
            ("monthly", self.cfg.budget_monthly, month_bounds()),
        ):
            spent = self.store.cost_in_range(*bounds)
            out[scope] = {
                "limit": limit,
                "spent": round(spent, 4),
                "ratio": round(spent / limit, 4) if limit else 0,
                "remaining": round(max(limit - spent, 0), 4) if limit else None,
                "exceeded": bool(limit and spent >= limit),
                "alert": bool(limit and spent >= limit * self.cfg.alert_ratio),
            }
        return out

    def check_budget(self):
        """达到阈值时触发一次告警（同一 scope 24 小时内只报一次，避免刷屏）。"""
        for scope in ("daily", "monthly"):
            st = self.check_single(scope)
            if st and st["alert"]:
                since = time.time() - (3600 if scope == "daily" else 24 * 3600)
                if not self.store.alert_recent(scope, since):
                    msg = (f"[tokenlens] {scope} 预算告警：已花费 ${st['spent']:.2f} / "
                           f"${st['limit']:.2f}（{st['ratio']*100:.0f}%）")
                    self.store.alert_add(scope, st["spent"], st["limit"], msg)
                    print(msg, flush=True)
                    self._webhook(msg, st, scope)

    def check_single(self, scope: str):
        limit = self.cfg.budget_daily if scope == "daily" else self.cfg.budget_monthly
        if not limit:
            return None
        start, end = day_bounds() if scope == "daily" else month_bounds()
        spent = self.store.cost_in_range(start, end)
        return {
            "scope": scope, "limit": limit, "spent": spent,
            "ratio": spent / limit, "exceeded": spent >= limit,
            "alert": spent >= limit * self.cfg.alert_ratio,
        }

    def _webhook(self, msg: str, st: Dict[str, Any], scope: str):
        url = self.cfg.webhook_url
        if not url:
            return
        try:
            import urllib.request
            payload = {"text": msg, "scope": scope,
                       "spent": st["spent"], "limit": st["limit"]}
            req = urllib.request.Request(
                url, data=__import__("json").dumps(payload).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5)
        except Exception as exc:
            print(f"[tokenlens] webhook 发送失败: {exc}")
