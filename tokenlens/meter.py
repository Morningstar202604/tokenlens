"""计量核心：把一次调用组装成记录、计算成本、检查预算并告警。"""

from __future__ import annotations

import calendar
import hashlib
import json
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
            return key
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
        self.calc = CostCalculator(self.pricing, cfg.cached_discount, cfg.cached_discounts)

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
            cost = self.calc.compute(model, prompt, completion, cached,
                                     provider=kw.get("provider"))
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
            "ttft_ms": (round(float(kw["ttft_ms"]), 1)
                        if kw.get("ttft_ms") is not None else None),
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
                    msg = (f"[tokenlens] {scope} 预算告警：已花费 ${st['spent']:.4f} / "
                           f"${st['limit']:.2f}（{st['ratio']*100:.0f}%）")
                    self.store.alert_add(scope, st["spent"], st["limit"], msg)
                    print(msg, flush=True)
                    self._webhook(msg, st, scope)

    def enforce_check(self) -> Optional[str]:
        """预算硬拦截检查：任一范围达到拦截阈值则返回拒绝原因，否则 None。

        由代理在转发前调用；limit 为 0（不限）的范围自动跳过。
        """
        ratio = self.cfg.enforce_budget_ratio or 1.0
        for scope in ("daily", "monthly"):
            st = self.check_single(scope)
            if st and st["spent"] >= st["limit"] * ratio:
                pct = min(st["ratio"] * 100, 999)
                return (f"{scope} 预算已用 ${st['spent']:.4f} / ${st['limit']:.2f}"
                        f"（{pct:.0f}%），超限拦截")
        return None

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
            payload = webhook_payload(self.cfg.webhook_type, st, scope)
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5)
        except Exception as exc:
            print(f"[tokenlens] webhook 发送失败: {exc}")


def webhook_payload(typ: str, st: Dict[str, Any], scope: str) -> Dict[str, Any]:
    """按通道类型生成机器人卡片：generic / dingtalk / wecom / feishu。"""
    spent, limit, ratio = st["spent"], st["limit"], st["ratio"] * 100
    if typ == "dingtalk":
        text = (f"### TokenLens 预算告警（{scope}）\n\n"
                f"已花费 **${spent:.2f}** / ${limit:.2f}（{ratio:.0f}%）")
        return {"msgtype": "markdown",
                "markdown": {"title": f"TokenLens 预算告警（{scope}）", "text": text}}
    if typ == "wecom":
        text = (f"**TokenLens 预算告警（{scope}）**\n"
                f"> 已花费 **${spent:.2f}** / ${limit:.2f}（{ratio:.0f}%）")
        return {"msgtype": "markdown", "markdown": {"content": text}}
    if typ == "feishu":
        text = f"TokenLens 预算告警（{scope}）：已花费 ${spent:.2f} / ${limit:.2f}（{ratio:.0f}%）"
        return {"msg_type": "text", "content": {"text": text}}
    return {"text": f"[tokenlens] {scope} 预算告警：已花费 ${spent:.2f} / ${limit:.2f}（{ratio:.0f}%）",
            "scope": scope, "spent": spent, "limit": limit}
