"""本机应用清单 API 测试：/api/apps 的计量标记与记录数聚合。

    python scripts/apps_api_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["TOKENLENS_LOCAL_SCAN"] = "0"  # 隔离：lifespan 不灌真实本机数据

CHECK = []


def check(name, cond, detail=""):
    CHECK.append((name, cond, detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def main():
    from fastapi.testclient import TestClient

    from tokenlens.config import Config
    from tokenlens.server import create_app
    from tokenlens.store import Store

    tmp = Path(tempfile.mkdtemp())
    cfg = Config()
    cfg.db_path = str(tmp / "ledger.db")
    cfg.host = "127.0.0.1"
    store = Store(cfg.db_path)
    ts = time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(ts))
    hour = time.localtime(ts).tm_hour
    store.insert_many([
        {"ts": ts, "day": day, "hour": hour,
         "provider": "xiaomi-mimo", "model": "mimo-v2.6-flash", "key_hash": "opencode-local",
         "prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110, "project": "VPet"},
        {"ts": ts, "day": day, "hour": hour,
         "provider": "zhipu", "model": "GLM-5.3-Flash", "key_hash": "zcode-local",
         "prompt_tokens": 200, "completion_tokens": 20, "total_tokens": 220, "project": "zcode"},
    ])

    app = create_app(cfg)
    # base_url 用 127.0.0.1：回环部署的 Host 校验中间件会拒绝 testserver 默认 Host
    with TestClient(app, base_url="http://127.0.0.1") as client:
        r = client.get("/api/apps")
        check("GET /api/apps 200", r.status_code == 200, str(r.status_code))
        data = r.json()
        apps = {a["id"]: a for a in data.get("apps", [])}
        check("返回应用清单", len(apps) >= 5, f"got {len(apps)}")
        oc = apps.get("opencode") or {}
        check("opencode metered + 记录数", oc.get("metered") is True and oc.get("records") == 1, str(oc)[:120])
        zc = apps.get("zcode") or {}
        check("zcode metered + 记录数", zc.get("metered") is True and zc.get("records") == 1, str(zc)[:120])
        cc = apps.get("claude-code") or {}
        check("claude-code metered 零记录", cc.get("metered") is True and cc.get("records") == 0, str(cc)[:120])
        wb = next((a for a in data.get("apps", []) if a["id"] == "workbuddy"), None)
        check("不可计量应用不带 metered 或 records=0",
              wb is None or (wb.get("metered") in (False, None) and not wb.get("records")),
              str(wb)[:120] if wb else "本机未装 workbuddy 探测项（正常）")
        statuses = {a["id"]: a["status"] for a in data.get("apps", [])}
        check("状态字段存在", all(isinstance(v, str) and v for v in statuses.values()))

    passed = sum(1 for _, c, _ in CHECK if c)
    print(f"\n{passed}/{len(CHECK)} PASS")
    return 0 if passed == len(CHECK) else 1


if __name__ == "__main__":
    sys.exit(main())
