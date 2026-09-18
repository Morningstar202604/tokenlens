"""演示数据生成器：往本地库里灌一批有真实感的调用记录，用来验证仪表盘。"""

from __future__ import annotations

import random
import time
from datetime import datetime, timedelta

from .meter import Meter, key_hash
from .pricing import PricingTable
from .store import Store

SCENARIOS = [
    # (project, provider, model, 占比, 平均输入, 平均输出, 流式概率, 错误率)
    ("chatbot-prod", "openai", "gpt-4o-mini", 30, 900, 320, 0.75, 0.01),
    ("chatbot-prod", "openai", "gpt-4o", 8, 2400, 640, 0.6, 0.02),
    ("rag-search", "deepseek", "deepseek-chat", 18, 5200, 700, 0.5, 0.015),
    ("code-agent", "anthropic", "claude-sonnet-4.5", 12, 12000, 1500, 0.85, 0.02),
    ("code-agent", "anthropic", "claude-haiku-4.5", 10, 3000, 900, 0.7, 0.01),
    ("doc-summary", "dashscope", "qwen-plus", 9, 6800, 800, 0.4, 0.01),
    ("doc-summary", "moonshot", "kimi-k2", 6, 7500, 1100, 0.6, 0.02),
    ("embedding", "openai", "text-embedding-3-small", 7, 1500, 0, 0.0, 0.005),
]

ENDPOINTS = {
    "embedding": "/embeddings",
}


def seed(store: Store, meter: Meter, n: int = 600, days: int = 7, seed_val: int = 42):
    rng = random.Random(seed_val)
    pricing = PricingTable()
    now = time.time()
    recs = []
    pool = []
    for s in SCENARIOS:
        pool.extend([s] * s[3])

    for _ in range(n):
        project, provider, model, _, pin, pout, pstream, perr = rng.choice(pool)
        # 时间分布：最近更密集（偏向当前时间）
        ago = (rng.random() ** 1.7) * days * 86400
        ts = now - ago
        hour = datetime.fromtimestamp(ts).hour
        if rng.random() > (0.35 + 0.65 * (1 - abs(hour - 14) / 14)):
            ts -= 3600 * rng.randint(1, 5)
        dt = datetime.fromtimestamp(ts)

        mult = rng.choice([0.4, 0.7, 1.0, 1.0, 1.4, 2.2, 4.0])
        prompt = max(1, int(pin * mult * rng.uniform(0.75, 1.3)))
        completion = max(0, int(pout * mult * rng.uniform(0.7, 1.4))) if pout else 0
        cached = int(prompt * rng.uniform(0.1, 0.45)) if provider == "openai" and rng.random() < 0.6 else 0
        is_stream = rng.random() < pstream
        failed = rng.random() < perr
        endpoint = ENDPOINTS.get(project, "/chat/completions")
        latency = rng.uniform(320, 2600) * (1 + prompt / 12000)
        if model.startswith("claude"):
            latency *= 1.15

        rec = {
            "ts": ts,
            "day": dt.strftime("%Y-%m-%d"),
            "hour": dt.hour,
            "provider": provider,
            "upstream": f"https://api.{provider}.com/v1",
            "model": model,
            "endpoint": endpoint,
            "project": project,
            "key_hash": key_hash(f"sk-demo-{project}"),
            "is_stream": 1 if is_stream else 0,
            "prompt_tokens": prompt,
            "completion_tokens": 0 if failed else completion,
            "total_tokens": prompt + (0 if failed else completion),
            "cached_tokens": cached,
            "reasoning_tokens": int(completion * 0.5) if "deepseek-reasoner" in model else 0,
            "cost_source": "reported",
            "latency_ms": round(latency, 1),
            "ttft_ms": round(rng.uniform(180, 900), 1) if is_stream and not failed else None,
            "status": rng.choice([429, 500, 400]) if failed else 200,
            "error": ("rate_limit_exceeded" if failed and rng.random() < 0.5 else "") if failed else "",
            "req_bytes": prompt * 4,
            "resp_bytes": completion * 4,
        }
        rec["cost"] = meter.calc.compute(model, rec["prompt_tokens"],
                                         rec["completion_tokens"], cached)
        recs.append(rec)

    recs.sort(key=lambda r: r["ts"])
    store.insert_many(recs)
    return len(recs)


def seed_cli(args):
    from .config import Config
    cfg = Config.load()
    store = Store(cfg.db_path)
    meter = Meter(store, cfg)
    n = seed(store, meter, n=args.n, days=args.days)
    s = store.summary()
    print(f"已写入 {n} 条演示记录 -> {cfg.db_path}")
    print(f"  请求 {s['requests']} · tokens {s['total_tokens']:,} · 成本 ${s['cost']:.4f}")
