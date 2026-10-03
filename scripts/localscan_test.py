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

    print("[入库成本估算] 未记价行按牌价估算,应用自记值不动")
    r3 = localscan.import_local(cfg, store, home=fake_home)
    check("补导入 zcode+claude 各 1 行", r3["zcode"]["imported"] == 1 and r3["claude-code"]["imported"] == 1, str(r3))
    row = lambda rid: store._local.execute(
        "SELECT cost, cost_source FROM requests WHERE request_id=?", (rid,)).fetchone()
    zc = row("zcode:rq-1")
    check("zcode 行按牌价估算", zc["cost"] > 0 and zc["cost_source"] == "zcode-est",
          f"cost={zc['cost']} src={zc['cost_source']}")
    cc = row("claude:u-1")
    check("claude 行按牌价估算", cc["cost"] > 0 and cc["cost_source"] == "claude-est",
          f"cost={cc['cost']} src={cc['cost_source']}")
    oc = row("opencode:ses_ok")
    check("opencode 未记价行估算", oc["cost"] > 0 and oc["cost_source"] == "opencode-est",
          f"cost={oc['cost']} src={oc['cost_source']}")
    bd = row("opencode:ses_badjson")
    check("应用自记成本不覆盖", bd["cost"] == 1.5 and bd["cost_source"] == "opencode-local",
          f"cost={bd['cost']} src={bd['cost_source']}")
    r4 = localscan.import_local(cfg, store, home=fake_home)
    check("重扫不重复入账且估算幂等",
          r4["opencode"]["imported"] == 0 and row("zcode:rq-1")["cost_source"] == "zcode-est", str(r4))

    print("[免费与未知价] 0 即真实,不臆造")
    fake_home2 = tmp / "home2"
    db2path = fake_home2 / ".local" / "share" / "opencode" / "opencode.db"
    db2path.parent.mkdir(parents=True)
    con = sqlite3.connect(db2path)
    con.execute("""CREATE TABLE session (
        id TEXT PRIMARY KEY, directory TEXT, model TEXT, cost REAL,
        tokens_input INTEGER, tokens_output INTEGER, tokens_reasoning INTEGER,
        tokens_cache_read INTEGER, tokens_cache_write INTEGER,
        time_created INTEGER, time_updated INTEGER, agent TEXT)""")
    con.executemany("INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
        ("ses_free", "C:/f", '{"id":"mimo-v2.5-free","providerID":"xiaomi-mimo"}',
         0.0, 5000, 800, 0, 0, 0, ts_ms, ts_ms, "build"),
        ("ses_unknown", "C:/u", '{"id":"step-5-preview","providerID":"stepfun"}',
         0.0, 4000, 600, 0, 0, 0, ts_ms, ts_ms, "build"),
        ("ses_appcost", "C:/a", '{"id":"glm-4.6","providerID":"zhipu"}',
         0.02, 1000, 200, 0, 0, 0, ts_ms, ts_ms, "build"),
    ])
    con.commit()
    con.close()
    cfg2 = Config()
    cfg2.db_path = str(tmp / "ledger2.db")
    store2 = Store(cfg2.db_path)
    localscan.import_local(cfg2, store2, home=fake_home2)
    row2 = lambda rid: store2._local.execute(
        "SELECT model, cost, cost_source FROM requests WHERE request_id=?", (rid,)).fetchone()
    fr = row2("opencode:ses_free")
    check("-free 模型不按牌价高估", fr["cost"] == 0.0 and fr["cost_source"] == "opencode-local",
          f"cost={fr['cost']} src={fr['cost_source']}")
    ur = row2("opencode:ses_unknown")
    check("未知价模型保持 0 不臆造", ur["cost"] == 0.0 and ur["cost_source"] == "opencode-local",
          f"cost={ur['cost']} src={ur['cost_source']}")
    ar = row2("opencode:ses_appcost")
    check("应用记价原样保留", ar["cost"] == 0.02 and ar["cost_source"] == "opencode-local",
          f"cost={ar['cost']} src={ar['cost_source']}")

    print("[reprice 回填] 价格表更新后重算存量")
    cfg2.pricing_overrides = {"step-5-preview": {"in": 2.0, "out": 5.0}}
    n_upd = localscan.reprice_local(cfg2, store2)
    ur2 = row2("opencode:ses_unknown")
    check("存量 0 成本行被回填", n_upd == 1 and ur2["cost"] > 0 and ur2["cost_source"] == "opencode-est",
          f"upd={n_upd} cost={ur2['cost']} src={ur2['cost_source']}")
    n2 = localscan.reprice_local(cfg2, store2)
    check("reprice 幂等", n2 == 0 and row2("opencode:ses_unknown")["cost"] == ur2["cost"], f"upd={n2}")
    check("回填不碰 -free 与自记行",
          row2("opencode:ses_free")["cost"] == 0.0 and row2("opencode:ses_appcost")["cost"] == 0.02)

    print("[scan_codex] rollout JSONL token_count 增量")
    sdir = fake_home / ".codex" / "sessions" / "2026" / "10" / "03"
    sdir.mkdir(parents=True)
    (sdir / "rollout-x.jsonl").write_text("\n".join([
        json.dumps({"timestamp": "2026-10-03T02:00:00Z", "type": "session_meta",
                    "payload": {"id": "sess-abc", "cwd": "C:/work/proj"}}),
        json.dumps({"timestamp": "2026-10-03T02:00:05Z", "type": "turn_context",
                    "payload": {"model": "gpt-5.1-codex"}}),
        json.dumps({"timestamp": "2026-10-03T02:00:10Z", "type": "event_msg",
                    "payload": {"type": "token_count", "info": {
                        "total_token_usage": {"input_tokens": 1000, "cached_input_tokens": 400,
                                              "output_tokens": 100, "total_tokens": 1100},
                        "last_token_usage": {"input_tokens": 600, "cached_input_tokens": 400,
                                             "output_tokens": 100, "total_tokens": 700}}}}),
        "garbage-line",
        json.dumps({"timestamp": "2026-10-03T02:01:00Z", "type": "event_msg",
                    "payload": {"type": "other_event"}}),
    ]), encoding="utf-8")
    xrecs = localscan.scan_codex(fake_home / ".codex" / "sessions")
    check("codex 只取 token_count 事件", len(xrecs) == 1, f"got {len(xrecs)}")
    x = xrecs[0]
    check("codex last_token_usage 增量入账",
          x["request_id"] == "codex:sess-abc:1" and x["model"] == "gpt-5.1-codex"
          and x["prompt_tokens"] == 600 and x["cached_tokens"] == 400 and x["completion_tokens"] == 100,
          str(x)[80:200])
    check("codex 厂商/项目", x["provider"] == "openai" and x["project"] == "proj")
    check("codex 缺失目录 → 空", localscan.scan_codex(tmp / "nope") == [])

    print("[scan_gemini_like] chats JSONL tokens 摘要")
    gdir = fake_home / ".gemini" / "tmp" / "h1" / "chats"
    gdir.mkdir(parents=True)
    (gdir / "session-a.jsonl").write_text("\n".join([
        json.dumps({"sessionId": "s1", "projectHash": "h1", "startTime": "2026-10-03T01:00:00Z", "kind": "main"}),
        json.dumps({"id": "m1", "timestamp": "2026-10-03T01:00:01Z", "type": "user", "content": "hi"}),
        json.dumps({"id": "m2", "timestamp": "2026-10-03T01:00:05Z", "type": "gemini", "model": "gemini-2.5-pro",
                    "content": "ok", "tokens": {"input": 500, "output": 120, "cached": 100, "thoughts": 30, "total": 650}}),
        json.dumps({"id": "m3", "timestamp": "2026-10-03T01:00:06Z", "type": "gemini", "content": "no tokens"}),
        "garbage",
    ]), encoding="utf-8")
    grecs = localscan.scan_gemini_like(fake_home, ".gemini", "gemini")
    check("gemini 只取带 tokens 的 gemini 消息", len(grecs) == 1, f"got {len(grecs)}")
    g = grecs[0]
    check("gemini 行字段", g["request_id"] == "gemini:s1:m2" and g["model"] == "gemini-2.5-pro"
          and g["prompt_tokens"] == 500 and g["cached_tokens"] == 100 and g["completion_tokens"] == 120
          and g["reasoning_tokens"] == 30 and g["provider"] == "google", str(g)[80:220])
    check("gemini 缺失目录 → 空", localscan.scan_gemini_like(tmp, ".qwen", "qwen") == [])

    print("[scan_aider] .aider.llm.history 应用自记成本")
    proj = fake_home / "proj"
    proj.mkdir()
    (proj / ".aider.llm.history").write_text("\n".join([
        json.dumps({"id": "chatcmpl-1", "model": "gpt-5.6-sol", "created": 1790000000,
                    "usage": {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200},
                    "cost": 0.0032}),
        "garbage",
        json.dumps({"model": "m", "usage": {"prompt_tokens": 0, "completion_tokens": 0}}),
    ]), encoding="utf-8")
    arecs = localscan.scan_aider(fake_home)
    check("aider 只取带用量的行", len(arecs) == 1, f"got {len(arecs)}")
    a = arecs[0]
    check("aider 字段与自记成本", a["cost"] == 0.0032 and a["cost_source"] == "aider-local"
          and a["model"] == "gpt-5.6-sol" and a["prompt_tokens"] == 1000 and a["project"] == "proj",
          str(a)[80:200])
    check("aider request_id 稳定", arecs and localscan.scan_aider(fake_home)[0]["request_id"] == a["request_id"])

    print("[scan_cline] ui_messages api_req_started")
    tasks = fake_home / "AppData" / "Roaming" / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "tasks" / "task-1"
    tasks.mkdir(parents=True)
    started = {"request": json.dumps({"model": "claude-sonnet-4-5", "temperature": 0}),
               "tokensIn": 1000, "tokensOut": 200, "cacheWrites": 0, "cacheReads": 500, "cost": 0.0}
    (tasks / "ui_messages.json").write_text(json.dumps([
        {"type": "say", "say": "text", "ts": 1790000000000, "text": "hello"},
        {"type": "say", "say": "api_req_started", "ts": 1790000005000, "text": json.dumps(started)},
        {"type": "say", "say": "api_req_started", "ts": 1790000010000, "text": "not-json"},
    ]), encoding="utf-8")
    crecs2 = localscan.scan_cline(fake_home, "saoudrizwan.claude-dev", "cline")
    check("cline 只取可解析的 api_req_started", len(crecs2) == 1, f"got {len(crecs2)}")
    cl = crecs2[0]
    check("cline 字段", cl["request_id"] == "cline:task-1:1" and cl["model"] == "claude-sonnet-4-5"
          and cl["prompt_tokens"] == 1000 and cl["cached_tokens"] == 500
          and cl["completion_tokens"] == 200 and cl["cost"] == 0.0 and cl["provider"] == "cline",
          str(cl)[80:220])
    check("cline 缺失目录 → 空", localscan.scan_cline(tmp, "saoudrizwan.claude-dev", "cline") == [])
    check("scan_claude 空目录 → 空", localscan.scan_claude(tmp / "nope") == [])

    print("[import_local] 新扫描器接线")
    r5 = localscan.import_local(cfg, store, home=fake_home)
    check("codex/gemini/aider/cline 各入 1 行",
          r5["codex"]["imported"] == 1 and r5["gemini-cli"]["imported"] == 1
          and r5["aider"]["imported"] == 1 and r5["cline"]["imported"] == 1, str(r5))
    srcs = {r[0] for r in store._local.execute(
        "SELECT DISTINCT cost_source FROM requests WHERE cost_source LIKE '%-local' OR cost_source LIKE '%-est'")}
    check("新应用已按牌价估算", {"codex-est", "gemini-est", "cline-est"} <= srcs, str(sorted(srcs)))
    check("aider 自记成本保留", "aider-local" in srcs)

    passed = sum(1 for _, c, _ in CHECK if c)
    print(f"\n{passed}/{len(CHECK)} PASS")
    return 0 if passed == len(CHECK) else 1


if __name__ == "__main__":
    sys.exit(main())
