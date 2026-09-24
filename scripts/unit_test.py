"""单元测试：价格表、成本计算、存储聚合、webhook 卡片、并发写入。

    python scripts/unit_test.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from fastapi import FastAPI
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tokenlens.config import Config
from tokenlens.meter import Meter, webhook_payload
from tokenlens.pricing import CostCalculator, PricingTable
from tokenlens.store import Store

CHECK = []


def check(name, cond, detail=""):
    CHECK.append((name, cond, detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def rec(model="gpt-4o", **kw):
    r = {"ts": kw.pop("ts", time.time()), "day": kw.pop("day", "2026-01-01"),
         "hour": kw.pop("hour", 0), "provider": kw.pop("provider", "openai"),
         "upstream": kw.pop("upstream", "https://api.openai.com/v1"), "model": model,
         "endpoint": kw.pop("endpoint", "/chat/completions"), "project": kw.pop("project", "unit"),
         "key_hash": kw.pop("key_hash", "abc"), "is_stream": kw.pop("is_stream", 0),
         "prompt_tokens": kw.pop("prompt_tokens", 1000), "completion_tokens": kw.pop("completion_tokens", 500),
         "total_tokens": kw.pop("total_tokens", 1500), "cached_tokens": kw.pop("cached_tokens", 0),
         "reasoning_tokens": kw.pop("reasoning_tokens", 0), "cost": kw.pop("cost", 0.001),
         "cost_source": kw.pop("cost_source", "reported"), "latency_ms": kw.pop("latency_ms", 500),
         "ttft_ms": kw.pop("ttft_ms", 100), "status": kw.pop("status", 200), "error": kw.pop("error", ""),
         "req_bytes": kw.pop("req_bytes", 100), "resp_bytes": kw.pop("resp_bytes", 100),
         "request_id": kw.pop("request_id", None)}
    r.update(kw)
    return r


def test_pricing():
    print("[1] 价格表")
    t = PricingTable()
    n_models = len(t.table)
    check("价格表 >= 2000 个模型", n_models >= 2000, f"{n_models} 个")
    check("gpt-4o 命中", t.price("gpt-4o")[0] > 0)
    check("gpt-4o-mini 不被 gpt-4o 抢", t.price("gpt-4o-mini")[0] < t.price("gpt-4o")[0],
          f"{t.price('gpt-4o-mini')} vs {t.price('gpt-4o')}")
    check("豆包内置兜底", t.price("doubao-seed-1.6")[0] > 0, str(t.price("doubao-seed-1.6")))
    check("未知模型计 0", t.price("no-such-model-xyz") == (0.0, 0.0))
    t2 = PricingTable(overrides={"gpt-4o": {"in": 9.9, "out": 19.9}})
    check("overrides 覆盖内置", t2.price("gpt-4o") == (9.9, 19.9))
    t3 = PricingTable(data_path="/tmp/not-exists.json")
    check("数据缺失回退内置", t3.price("gpt-4o")[0] > 0)

    print("[2] 成本计算（含缓存折扣）")
    calc = CostCalculator(PricingTable(), cached_discount=0.1, cached_discounts={"openai": 0.25})
    c1 = calc.compute("gpt-4o", prompt_tokens=1_000_000, completion_tokens=1_000_000)
    check("1M in + 1M out = $12.5", abs(c1 - 12.5) < 1e-6, f"${c1}")
    c2 = calc.compute("gpt-4o", prompt_tokens=1_000_000, completion_tokens=0,
                      cached_tokens=1_000_000, provider="openai")
    check("openai 缓存 0.25 折", abs(c2 - 0.625) < 1e-6, f"${c2}")
    c3 = calc.compute("gpt-4o", prompt_tokens=1_000_000, completion_tokens=0,
                      cached_tokens=1_000_000, provider="deepseek")
    check("deepseek 用默认 0.1 折", abs(c3 - 0.25) < 1e-6, f"${c3}")


def test_store():
    print("[3] store 聚合")
    db = "/tmp/tl-unit.db"
    if Path(db).exists():
        os.remove(db)
    st = Store(db)
    now = time.time()
    for i in range(10):
        st.insert(rec(cost=0.001 + i * 0.0001))
    st.insert(rec(status=500, error="boom", prompt_tokens=100, completion_tokens=0,
                  total_tokens=100, cost=0.0))
    s = st.summary(now - 60, now + 60)
    check("请求数 11", s["requests"] == 11, str(s["requests"]))
    check("错误 1", s["errors"] == 1)
    check("token 合计", s["total_tokens"] == 15100, str(s["total_tokens"]))
    ls = st.live_stats(now - 60)
    check("live_stats 请求数", ls["requests"] == 11)
    check("live_stats 错误数", ls["errors"] == 1)
    check("live_stats 成本>0", ls["cost"] > 0, f"${ls['cost']}")
    bd = st.breakdown("model", now - 60, now + 60, limit=5)
    check("breakdown 聚合", bd and bd[0]["requests"] == 11 and bd[0]["name"] == "gpt-4o",
          json.dumps(bd, ensure_ascii=False)[:120])
    ts = st.timeseries("hour", now - 3600, now + 3600)
    check("timeseries 有桶", len(ts) >= 1 and ts[0]["requests"] == 11,
          json.dumps(ts, ensure_ascii=False)[:120])
    st.close()

    print("[4] 并发写入（busy_timeout / 独立写连接）")
    db2 = "/tmp/tl-unit-conc.db"
    if Path(db2).exists():
        os.remove(db2)
    st2 = Store(db2)
    errs = []
    def w(i):
        try:
            for j in range(20):
                st2.insert(rec(model="gpt-4o-mini", project=f"p{i}", prompt_tokens=10,
                               completion_tokens=5, total_tokens=15, cost=0.00001,
                               latency_ms=1, ttft_ms=None))
        except Exception as e:
            errs.append(str(e))
    threads = [threading.Thread(target=w, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    check("并发 160 次写入零异常", not errs, "; ".join(errs[:2]))
    check("并发后计数正确", st2.summary(None, None)["requests"] == 160,
          str(st2.summary(None, None)["requests"]))
    st2.close()


def test_enforce():
    print("[5] 预算硬拦截检查")
    from tokenlens.meter import Meter
    db = "/tmp/tl-unit-enf.db"
    if Path(db).exists():
        os.remove(db)
    cfg = Config()
    cfg.db_path = db
    st = Store(db)
    meter = Meter(st, cfg)
    # 不限预算 → 不拦
    cfg.budget_daily = 0
    cfg.budget_monthly = 0
    check("不限预算不拦", meter.enforce_check() is None)
    # 预算 10，未超 → 不拦
    cfg.budget_daily = 10
    cfg.budget_monthly = 0
    now = time.time()
    for i in range(3):
        st.insert(rec(cost=1.0))
    check("未超限不拦", meter.enforce_check() is None)
    # 超支 → 拦截原因含 scope
    cfg.budget_daily = 2.0
    reason = meter.enforce_check()
    check("超支触发拦截", reason is not None and "daily" in reason, str(reason)[:80])
    # 阈值 0.8：用到 80% 就拦（spent=3.0 ≥ 3.5*0.8=2.8）
    cfg.budget_daily = 3.5
    cfg.enforce_budget_ratio = 0.8
    reason2 = meter.enforce_check()
    check("80% 阈值触发拦截", reason2 is not None, str(reason2)[:80])
    # 阈值 1.5：超支 150% 才拦 → 当前 30% 不拦
    cfg.budget_daily = 10.0
    cfg.enforce_budget_ratio = 1.5
    check("150% 阈值不拦当前", meter.enforce_check() is None)
    st.close()


def test_auth():
    print("[6] 仪表盘访问令牌")
    from fastapi.testclient import TestClient
    from tokenlens.api import create_api
    db = "/tmp/tl-unit-auth.db"
    if Path(db).exists():
        os.remove(db)
    st = Store(db)
    cfg2 = Config()
    cfg2.db_path = db
    m = Meter(st, cfg2)
    app = FastAPI()
    app.include_router(create_api(st, m))
    c = TestClient(app)
    # 默认不鉴权
    check("默认不鉴权", c.get("/api/stats").status_code == 200)
    # 配置 token 后：无 token 401，正确 token 200
    cfg2.dashboard_token = "sekret"
    check("未带 token 401", c.get("/api/stats").status_code == 401)
    check("Bearer token 通过", c.get("/api/stats", headers={"Authorization": "Bearer sekret"}).status_code == 200)
    check("query token 通过", c.get("/api/stats?token=sekret").status_code == 200)
    check("错误 token 401", c.get("/api/stats?token=wrong").status_code == 401)
    st.close()


def test_webhook():
    print("[5] webhook 卡片格式")
    st = {"spent": 8.5, "limit": 10.0, "ratio": 0.85}
    d = webhook_payload("dingtalk", st, "daily")
    check("钉钉 markdown", d["msgtype"] == "markdown" and "预算告警" in d["markdown"]["text"])
    w = webhook_payload("wecom", st, "daily")
    check("企业微信 markdown", w["msgtype"] == "markdown" and ">" in w["markdown"]["content"])
    f = webhook_payload("feishu", st, "daily")
    check("飞书 text", f["msg_type"] == "text" and "8.50" in f["content"]["text"])
    g = webhook_payload("generic", st, "daily")
    check("通用 JSON 含数字", g["text"] and g["spent"] == 8.5)


def main():
    test_pricing()
    test_store()
    test_enforce()
    test_auth()
    test_webhook()
    passed = sum(1 for _, ok, _ in CHECK if ok)
    print(f"\n{'='*52}\n结果: {passed}/{len(CHECK)} 通过")
    failed = [n for n, ok, _ in CHECK if not ok]
    if failed:
        print("失败项:", ", ".join(failed))
    return 0 if passed == len(CHECK) else 1


if __name__ == "__main__":
    sys.exit(main())
