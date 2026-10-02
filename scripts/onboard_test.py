"""单元测试：onboard 应用扫描/接入/还原 + 内置厂商别名表。

    python scripts/onboard_test.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# 必须在导入 tokenlens 之前隔离配置目录：任何 cfg.save()（包括经
# TestClient 触发的 PUT /api/config）都只能写到临时目录，严禁污染真实配置
os.environ.setdefault("TOKENLENS_HOME", tempfile.mkdtemp(prefix="tl-home-"))

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tokenlens.config import Config

CHECK = []


def check(name, cond, detail=""):
    CHECK.append((name, cond, detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def make_home(settings=None):
    """构造一个带 .claude/settings.json 的假 HOME。"""
    tmp = tempfile.mkdtemp(prefix="tokenlens-onboard-")
    home = Path(tmp)
    if settings is not None:
        d = home / ".claude"
        d.mkdir(parents=True)
        (d / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    return home


def test_upstreams_catalog():
    u = Config().upstreams
    builtin = {"openai", "deepseek", "moonshot", "zhipu", "dashscope",
               "anthropic", "siliconflow"}
    cn_new = {"spark", "sensenova", "ai360", "hunyuan", "ark", "qianfan"}
    local = {"ollama", "lmstudio", "vllm", "llamacpp"}
    check("内置 7 家别名保留", builtin <= set(u), str(sorted(builtin - set(u))))
    check("国产补充厂商就位", cn_new <= set(u), str(sorted(cn_new - set(u))))
    check("本地推理运行时就位", local <= set(u), str(sorted(local - set(u))))
    check("厂商别名 >= 30（国内外主流）", len(u) >= 30, f"共 {len(u)} 条")
    bad = [k for k, v in u.items() if not str(v).startswith(("http://", "https://"))]
    check("所有上游地址以 http(s) 开头", not bad, str(bad))


def test_registry():
    from tokenlens.onboard import APPS
    ids = {a.id for a in APPS}
    want = {"claude-code", "opencode", "qwen-code", "iflow", "cline", "roo-code",
            "chatbox", "cherry-studio"}
    check("开源项目/应用注册表就位", want <= ids, str(sorted(want - ids)))
    check("注册表应用 >= 12", len(APPS) >= 12, f"共 {len(APPS)} 个")


def test_detect():
    from tokenlens.onboard import detect
    home = make_home(settings={})
    rows = {r["id"]: r for r in detect(home=home)}
    check("claude-code 检出为可接入", rows.get("claude-code", {}).get("status") == "可接入",
          str(rows.get("claude-code")))
    check("未安装的应用不报可接入", rows.get("chatbox", {}).get("status") != "可接入")
    empty = {r["id"]: r for r in detect(home=Path(tempfile.mkdtemp(prefix="tl-empty-")))}
    check("空 HOME 下 claude-code 为未安装", empty.get("claude-code", {}).get("status") == "未安装")


def test_wire_unwire():
    from tokenlens.onboard import wire, unwire, wired_base
    home = make_home(settings={"hooks": {}, "env": {"OTHER": "1"}})
    cfg = Config()
    msg = wire("claude-code", cfg, home=home)
    p = home / ".claude" / "settings.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    expect = f"{cfg.proxy_base}/anthropic"
    check("wire 写入 ANTHROPIC_BASE_URL", data.get("env", {}).get("ANTHROPIC_BASE_URL") == expect, msg)
    check("wire 保留既有配置键", data.get("env", {}).get("OTHER") == "1" and "hooks" in data)
    bak = p.with_suffix(p.suffix + ".tokenlens-bak")
    check("wire 生成备份文件", bak.exists())
    check("wire 后状态为已接入", wired_base("claude-code", cfg, home=home) == expect)
    msg = unwire("claude-code", cfg, home=home)
    data = json.loads(p.read_text(encoding="utf-8"))
    check("unwire 移除接入键", "ANTHROPIC_BASE_URL" not in (data.get("env") or {}), msg)
    check("unwire 保留其余键", data.get("env", {}).get("OTHER") == "1")


def test_wire_preserves_existing_base():
    from tokenlens.onboard import wire, unwire
    home = make_home(settings={"env": {"ANTHROPIC_BASE_URL": "https://k.example.com"}})
    cfg = Config()
    wire("claude-code", cfg, home=home)
    unwire("claude-code", cfg, home=home)
    data = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    check("unwire 还原用户原有 base_url",
          data.get("env", {}).get("ANTHROPIC_BASE_URL") == "https://k.example.com")


def test_provider_domains():
    from tokenlens.meter import provider_from_url
    cases = {
        "https://api.mistral.ai/v1": "mistral",
        "https://api.minimax.chat/v1": "minimax",
        "https://api.stepfun.com/v1": "stepfun",
        "https://api.baichuan-ai.com/v1": "baichuan",
        "https://api.lingyiwanwu.com/v1": "yi",
        "https://api-inference.modelscope.cn/v1": "modelscope",
        "https://qianfan.baidubce.com/v2": "qianfan",
        "https://token.sensenova.cn/v1": "sensenova",
        "https://spark-api-open.xf-yun.com/v1": "spark",
        "https://api.360.cn/v1": "ai360",
        "https://api.cerebras.ai/v1": "cerebras",
        "https://integrate.api.nvidia.com/v1": "nvidia",
        "https://api.deepinfra.com/v1/openai": "deepinfra",
        "https://api.cohere.ai/compatibility/v1": "cohere",
        "https://router.huggingface.co/v1": "huggingface",
        "http://127.0.0.1:11434/v1": "local",
    }
    bad = {u: provider_from_url(u) for u, w in cases.items() if provider_from_url(u) != w}
    check("新厂商域名识别与目录同步", not bad, str(bad))


def test_key_aliases():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from tokenlens.api import create_api
    from tokenlens.meter import Meter
    from tokenlens.store import Store
    tmp = tempfile.mkdtemp(prefix="tl-alias-")
    cfg = Config(db_path=str(Path(tmp) / "u.db"))
    cfg.key_aliases = {"abc123def456": "Claude Code"}
    store = Store(cfg.db_path)
    meter = Meter(store, cfg)
    meter.record(provider="anthropic", model="claude-3-5-haiku", key_hash="abc123def456",
                 prompt_tokens=100, completion_tokens=10, status=200)
    app = FastAPI()
    app.include_router(create_api(store, meter))
    c = TestClient(app)
    bk = c.get("/api/breakdown?field=key_hash&rng=all").json()
    check("构成维度显示应用别名", any(r["name"] == "Claude Code" for r in bk), str(bk[:1]))
    rc = c.get("/api/recent?limit=5").json()
    check("明细行带 app 字段", bool(rc) and rc[0].get("app") == "Claude Code",
          str((rc or [{}])[0].get("app")))
    cf = c.get("/api/config").json()
    check("config 暴露 key_aliases", cf.get("key_aliases") == {"abc123def456": "Claude Code"})
    r2 = c.put("/api/config", json={"key_aliases": {"abc123def456": "我的别名"}})
    ok = r2.status_code == 200 and r2.json().get("key_aliases", {}).get("abc123def456") == "我的别名"
    check("PUT 更新别名并生效", ok, f"status={r2.status_code}")


def main():
    print("onboard 单元测试")
    print("-" * 46)
    test_upstreams_catalog()
    test_registry()
    test_provider_domains()
    test_key_aliases()
    test_detect()
    test_wire_unwire()
    test_wire_preserves_existing_base()
    passed = sum(1 for _, ok, _ in CHECK if ok)
    print(f"\n{'='*52}\n结果: {passed}/{len(CHECK)} 通过")
    failed = [n for n, ok, _ in CHECK if not ok]
    if failed:
        print("失败项:", ", ".join(failed))
    return 0 if passed == len(CHECK) else 1


if __name__ == "__main__":
    sys.exit(main())
