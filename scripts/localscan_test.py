"""本地用量扫描测试：合成 OpenCode 库 fixture → 解析正确性 + 幂等导入。

    python scripts/localscan_test.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tokenlens.config import Config
from tokenlens.store import Store

CHECK = []


def check(name, cond, detail=""):
    CHECK.append((name, cond, detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def make_fake_opencode(db: Path) -> int:
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE session (
        id TEXT PRIMARY KEY, directory TEXT, model TEXT, cost REAL,
        tokens_input INTEGER, tokens_output INTEGER, tokens_reasoning INTEGER,
        tokens_cache_read INTEGER, tokens_cache_write INTEGER,
        time_created INTEGER, time_updated INTEGER, agent TEXT)""")
    ts_ms = int(time.time() * 1000)
    rows = [
        ("ses_ok", "C:/Users/X1882/Desktop/VPet",
         '{"id":"mimo-v2.6-flash","providerID":"xiaomi-mimo","variant":"default"}',
         0.0, 1000, 200, 5, 3000, 10, ts_ms - 5000, ts_ms, "build"),
        ("ses_zero", "C:/x", '{"id":"m","providerID":"p"}', 0.0,
         0, 0, 0, 0, 0, ts_ms - 9000, ts_ms - 8000, "build"),
        ("ses_badjson", "C:/y", "not-a-json", 1.5,
         10, 20, 0, 0, 0, ts_ms - 7000, ts_ms - 6000, "plan"),
    ]
    con.executemany("INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return ts_ms


def main():
    tmp = Path(tempfile.mkdtemp())
    fake_home = tmp / "home"
    db = fake_home / ".local" / "share" / "opencode" / "opencode.db"
    db.parent.mkdir(parents=True)
    ts_ms = make_fake_opencode(db)

    from tokenlens import localscan

    print("[scan_opencode] 解析")
    recs = localscan.scan_opencode(db)
    check("零用量行被跳过", len(recs) == 2, f"got {len(recs)}")
    ok = next(r for r in recs if r["request_id"] == "opencode:ses_ok")
    check("model JSON 解析", ok["model"] == "mimo-v2.6-flash", ok["model"])
    check("provider 取 providerID", ok["provider"] == "xiaomi-mimo")
    check("project 取目录末段", ok["project"] == "VPet")
    check("毫秒时间转秒", abs(ok["ts"] - ts_ms / 1000) < 2, f"ts={ok['ts']} expect~{ts_ms/1000}")
    check("day/hour 已派生", isinstance(ok["day"], str) and len(ok["day"]) == 10 and 0 <= ok["hour"] <= 23)
    check("缓存与推理 tokens", ok["cached_tokens"] == 3000 and ok["reasoning_tokens"] == 5)
    bad = next(r for r in recs if r["request_id"] == "opencode:ses_badjson")
    check("坏 model JSON → unknown 不抛", bad["model"] == "unknown" and bad["cost"] == 1.5)

    print("[import_local] 幂等")
    cfg = Config()
    cfg.db_path = str(tmp / "ledger.db")
    store = Store(cfg.db_path)
    r1 = localscan.import_local(cfg, store, home=fake_home)
    check("首次导入 imported=2", r1["opencode"]["imported"] == 2, str(r1))
    n = store._local.execute("select count(*) from requests").fetchone()[0]
    check("账本恰好 2 行", n == 2, f"rows={n}")
    r2 = localscan.import_local(cfg, store, home=fake_home)
    check("重复扫描 imported=0 skipped=2",
          r2["opencode"]["imported"] == 0 and r2["opencode"]["skipped"] == 2, str(r2))
    n2 = store._local.execute("select count(*) from requests").fetchone()[0]
    check("账本仍 2 行", n2 == 2)

    print("[不存在的库]")
    empty = localscan.scan_opencode(tmp / "nope.db")
    check("缺失库返回空列表不抛", empty == [])

    print("[scan_zcode] rollout JSONL")
    zdir = fake_home / ".zcode" / "cli" / "rollout"
    zdir.mkdir(parents=True)
    (zdir / "model-io-sess_x.jsonl").write_text("\n".join([
        json.dumps({"sessionId": "sess_x", "requestId": "rq-1", "startedAt": "2026-10-02T19:15:05.345Z",
                    "model": {"modelId": "GLM-5.3-Flash", "providerId": "account:bigmodel-start-plan"},
                    "response": {"usage": {"inputTokens": 48271, "outputTokens": 149,
                                           "totalTokens": 48420, "cacheReadTokens": 17728,
                                           "cacheWriteTokens": 0}}}),
        json.dumps({"sessionId": "sess_x", "requestId": "rq-2", "startedAt": "2026-10-02T19:16:00.000Z",
                    "model": "plain-model-name",
                    "response": {"usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}}}),
        "not-json-garbage",
    ]), encoding="utf-8")
    zrecs = localscan.scan_zcode(zdir)
    check("只有带用量的行入账", len(zrecs) == 1, f"got {len(zrecs)}")
    z = zrecs[0]
    check("zcode 模型/厂商解析", z["model"] == "GLM-5.3-Flash" and z["provider"] == "zhipu", f"{z['model']}/{z['provider']}")
    check("zcode request_id 幂等键", z["request_id"] == "zcode:rq-1")
    check("zcode ISO 时间转秒", abs(z["ts"] - 1790968505.345) < 1, f"ts={z['ts']}")
    check("zcode 缓存 tokens", z["cached_tokens"] == 17728 and z["prompt_tokens"] == 48271 and z["completion_tokens"] == 149)
    check("zcode 缺失库/目录 → 空", localscan.scan_zcode(tmp / "nope") == [])

    print("[scan_claude] projects JSONL")
    proj = fake_home / ".claude" / "projects" / "C--Users-X1882-Desktop-VPet"
    proj.mkdir(parents=True)
    (proj / "sess1.jsonl").write_text("\n".join([
        json.dumps({"type": "assistant", "uuid": "u-1", "sessionId": "s1", "cwd": "C:\\Users\\X1882\\Desktop\\VPet",
                    "timestamp": "2026-09-26T08:51:50.094Z",
                    "message": {"model": "claude-sonnet-4-5", "usage": {
                        "input_tokens": 120, "output_tokens": 45,
                        "cache_read_input_tokens": 900, "cache_creation_input_tokens": 30}}}),
        json.dumps({"type": "assistant", "uuid": "u-2", "sessionId": "s1",
                    "timestamp": "2026-09-26T08:52:00.000Z", "isApiErrorMessage": True,
                    "message": {"model": "claude-sonnet-4-5", "usage": {"input_tokens": 999, "output_tokens": 999}}}),
        json.dumps({"type": "user", "uuid": "u-3", "message": {"role": "user", "content": "hi"}}),
    ]), encoding="utf-8")
    crecs = localscan.scan_claude(fake_home / ".claude" / "projects")
    check("只有有效 assistant 行入账", len(crecs) == 1, f"got {len(crecs)}")
    c = crecs[0]
    check("claude 模型/厂商/项目", c["model"] == "claude-sonnet-4-5" and c["provider"] == "anthropic" and c["project"] == "VPet")
    check("claude 幂等键与缓存", c["request_id"] == "claude:u-1" and c["cached_tokens"] == 900)
    check("claude 空目录 → 空", localscan.scan_claude(tmp / "nope") == [])

    passed = sum(1 for _, c, _ in CHECK if c)
    print(f"\n{passed}/{len(CHECK)} PASS")
    return 0 if passed == len(CHECK) else 1


if __name__ == "__main__":
    sys.exit(main())
