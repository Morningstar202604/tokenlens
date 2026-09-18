"""把演示库里的真实查询结果注入 index.html 的 DEMO_DATA，
使仪表盘在离线（未启动服务）直接打开时也能展示完整图表。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tokenlens.api import parse_range  # noqa: E402
from tokenlens.config import Config  # noqa: E402
from tokenlens.meter import Meter  # noqa: E402
from tokenlens.store import Store  # noqa: E402

HTML = ROOT / "tokenlens" / "web" / "index.html"
MARK = "//__DEMO_DATA__"
RANGES = ["24h", "today", "7d", "30d", "all"]


def build(cfg: Config) -> dict:
    store = Store(cfg.db_path)
    meter = Meter(store, cfg)
    out = {}
    for rng in RANGES:
        s, e = parse_range(rng)
        bucket = "hour" if rng in ("24h", "today") else "day"
        out[rng] = {
            "stats": store.summary(s, e),
            "ts": store.timeseries(bucket, s, e),
            "rank": {f: store.breakdown(f, s, e, limit=8) for f in ("model", "project", "provider")},
            "recent": store.recent(100, s, e),
            "budget": meter.budget_status(),
            "filters": {"models": store.distinct("model"),
                        "projects": store.distinct("project"),
                        "providers": store.distinct("provider"),
                        "endpoints": store.distinct("endpoint")},
            "live": {"window": 60, "requests": 12, "rpm": 12.0,
                     "tokens": sum(r["total_tokens"] for r in store.recent(20)),
                     "cost": round(sum(r["cost"] for r in store.recent(20)), 6),
                     "errors": 0},
        }
    return out


def main():
    cfg = Config.load()
    data = build(cfg)
    text = HTML.read_text(encoding="utf-8")
    lines = text.split("\n")
    hit = -1
    for i, ln in enumerate(lines):
        if MARK in ln:
            hit = i
            break
    if hit < 0:
        print("未找到注入标记，已跳过")
        return 1
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    lines[hit] = f"const DEMO_DATA = {payload};"
    HTML.write_text("\n".join(lines), encoding="utf-8")
    size = len(payload) / 1024
    print(f"已注入演示数据 {size:.0f} KB -> {HTML}")
    print(f"  范围: {', '.join(RANGES)}")
    print(f"  今日: {data['today']['stats']['requests']} 请求 / "
          f"{data['today']['stats']['total_tokens']:,} tokens / ${data['today']['stats']['cost']:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
