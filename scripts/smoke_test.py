"""端到端冒烟测试：启动 mock 上游 + TokenLens 代理，跑通各类场景并校验统计结果。

    python scripts/smoke_test.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MOCK_PORT = 8901
PROXY_PORT = 8799
DB = "/tmp/tokenlens-smoke.db"
BASE = f"http://127.0.0.1:{PROXY_PORT}/v1"
CHECK = []


def check(name: str, cond: bool, detail: str = ""):
    CHECK.append((name, cond, detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def wait_up(url: str, timeout: int = 40):
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = httpx.get(url, timeout=2)
            if r.status_code < 500:
                return True
        except Exception:
            time.sleep(0.3)
    return False


def main():
    if Path(DB).exists():
        os.remove(DB)
    env = dict(os.environ, TOKENLENS_HOME="/tmp/tokenlens-smoke")
    Path("/tmp/tokenlens-smoke").mkdir(exist_ok=True)

    mock = subprocess.Popen([sys.executable, str(ROOT / "examples/mock_upstream.py"),
                             "--port", str(MOCK_PORT)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proxy = subprocess.Popen(
        [sys.executable, "-m", "tokenlens", "start", "--port", str(PROXY_PORT),
         "--upstream", f"http://127.0.0.1:{MOCK_PORT}/v1", "--daily", "1000"],
        env=env, cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        print("等待服务启动...")
        assert wait_up(f"http://127.0.0.1:{MOCK_PORT}/v1/models"), "mock 上游未启动"
        assert wait_up(f"http://127.0.0.1:{PROXY_PORT}/api/health"), "TokenLens 未启动"
        print("服务就绪\n")

        c = httpx.Client(timeout=30, headers={"Authorization": "Bearer sk-test-key",
                                              "X-TokenLens-Project": "smoke"})

        print("[1] 非流式请求")
        r = c.post(f"{BASE}/chat/completions", json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "写一首关于监控系统的短诗"}],
        })
        check("非流式 200", r.status_code == 200, str(r.status_code))
        data = r.json()
        check("响应含 usage", "usage" in data)

        print("[2] 流式请求（上游回传 usage）")
        chunks = 0
        with c.stream("POST", f"{BASE}/chat/completions", json={
            "model": "gpt-4o-mini", "stream": True,
            "messages": [{"role": "user", "content": "流式测试"}],
        }) as resp:
            check("流式 200", resp.status_code == 200)
            for line in resp.iter_lines():
                if line.startswith("data:"):
                    chunks += 1
        check("流式收到多个 chunk", chunks > 5, f"{chunks} chunks")

        print("[3] 多上游别名路由")
        r = c.post(f"{BASE}/deepseek/chat/completions", json={
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": "别名路由测试"}],
        })
        check("别名路径转发成功（真实上游无 key 会 401 / 沙箱无外网会 502）",
              r.status_code in (200, 401, 502), str(r.status_code))

        print("[4] Embeddings")
        r = c.post(f"{BASE}/embeddings", json={"model": "text-embedding-3-small",
                                               "input": "hello tokenlens"})
        check("embeddings 200", r.status_code == 200, str(r.status_code))

        print("[5] 上游错误")
        r = c.post(f"{BASE}/chat/completions", json={
            "model": "gpt-4o-mini", "__force_error": True,
            "messages": [{"role": "user", "content": "触发错误"}],
        })
        check("错误被透传 400", r.status_code == 400, str(r.status_code))

        print("[6] 上游不可达")
        r = c.post(f"{BASE}/chat/completions",
                   headers={"X-TokenLens-Upstream": "http://127.0.0.1:9/v1"},
                   json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "x"}]})
        check("不可达上游返回 502", r.status_code == 502, str(r.status_code))

        time.sleep(0.6)
        print("\n[7] 校验统计接口")
        stats = httpx.get(f"http://127.0.0.1:{PROXY_PORT}/api/stats?rng=all").json()
        print("   ", json.dumps(stats, ensure_ascii=False))
        check("记录了请求", stats["requests"] >= 5, f"{stats['requests']} 条")
        check("统计到 token", stats["total_tokens"] > 0, f"{stats['total_tokens']} tokens")
        check("成本 > 0", stats["cost"] > 0, f"${stats['cost']}")
        check("错误被记录", stats["errors"] >= 2, f"{stats['errors']} 条错误")

        recent = httpx.get(f"http://127.0.0.1:{PROXY_PORT}/api/recent?limit=20&rng=all").json()
        streamed = [x for x in recent if x["is_stream"]]
        check("流式被标记", len(streamed) >= 1)
        check("流式记录首字延迟", any(x["ttft_ms"] for x in streamed))
        check("项目维度生效", all(x["project"] == "smoke" for x in recent if x["project"]))

        bd = httpx.get(f"http://127.0.0.1:{PROXY_PORT}/api/breakdown?field=model&rng=all").json()
        check("模型维度聚合", len(bd) >= 1, json.dumps(bd, ensure_ascii=False)[:120])

        ts = httpx.get(f"http://127.0.0.1:{PROXY_PORT}/api/timeseries?rng=all").json()
        check("时序有数据", len(ts) >= 1)

        print("\n[8] 仪表盘页面")
        r = httpx.get(f"http://127.0.0.1:{PROXY_PORT}/")
        check("首页可访问", r.status_code == 200 and "TokenLens" in r.text)

        print("[11] SSE 事件被切碎到跨 chunk（行缓冲解析）")
        with c.stream("POST", f"{BASE}/chat/completions", json={
            "model": "gpt-4o-mini", "stream": True,
            "stream_options": {"include_usage": True},
            "__chunk_size": 40,
            "messages": [{"role": "user", "content": "chunk-split 测试"}],
        }) as resp:
            check("切碎流 200", resp.status_code == 200, str(resp.status_code))
            n = 0
            for _ in resp.iter_lines():
                n += 1
            check("切碎流仍能完整读完", n > 5, f"{n} 行")
        time.sleep(0.5)
        recent = httpx.get(f"http://127.0.0.1:{PROXY_PORT}/api/recent?limit=5&rng=all").json()
        latest = next((x for x in recent if x.get("is_stream") and x.get("completion_tokens")), None)
        check("跨 chunk 后 usage 未丢失", latest is not None,
              f"completion={latest and latest['completion_tokens']}")
        check("usage 来自上游非估算", latest and not latest.get("cost_source") == "estimated")

        print("\n[12] 并发写入（SQLite busy_timeout / 独立写连接）")
        import threading
        ok_reqs = []
        lock = threading.Lock()
        def worker(i):
            for j in range(5):
                try:
                    r = httpx.post(f"{BASE}/chat/completions", headers={
                        "Authorization": "Bearer sk-test-key", "X-TokenLens-Project": "conc"},
                        json={"model": "gpt-4o-mini",
                              "messages": [{"role": "user", "content": f"并发 {i}-{j}"}]},
                        timeout=30)
                    with lock:
                        ok_reqs.append(r.status_code)
                except Exception as e:
                    with lock:
                        ok_reqs.append(999)
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        check("并发 50 请求全 200", ok_reqs.count(200) == 50,
              f"{ok_reqs.count(200)}/50 成功, 异常 {ok_reqs.count(999)}")
        stats = httpx.get(f"http://127.0.0.1:{PROXY_PORT}/api/stats?rng=all").json()
        check("并发记录全部入库", stats["requests"] >= 57, f"{stats['requests']} 条")

        print("\n[13] 预算直改即时生效 + 告警写入")
        r = httpx.post(f"http://127.0.0.1:{PROXY_PORT}/api/budget",
                       json={"scope": "daily", "limit": 0.001})
        check("POST budget 生效", r.status_code == 200 and r.json()["daily"]["limit"] == 0.001,
              json.dumps(r.json(), ensure_ascii=False)[:120])
        httpx.post(f"{BASE}/chat/completions", headers={"Authorization": "Bearer sk-test-key"},
                   json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "触发告警"}]})
        time.sleep(0.4)
        alerts = httpx.get(f"http://127.0.0.1:{PROXY_PORT}/api/alerts?limit=10").json()
        daily_alert = [a for a in alerts if a["scope"] == "daily"]
        check("告警写入历史", len(daily_alert) >= 1, json.dumps(alerts, ensure_ascii=False)[:160])
        httpx.post(f"http://127.0.0.1:{PROXY_PORT}/api/budget",
                   json={"scope": "daily", "limit": 1000})  # 恢复预算
        check("恢复预算", httpx.get(f"http://127.0.0.1:{PROXY_PORT}/api/budget").json()["daily"]["limit"] == 1000)

        print("\n[14] 设置接口")
        r = httpx.put(f"http://127.0.0.1:{PROXY_PORT}/api/config",
                      json={"webhook_type": "dingtalk", "budget_daily": 1000})
        check("PUT config 生效", r.status_code == 200 and r.json()["webhook_type"] == "dingtalk")

        print("\n[9] SDK 埋点（不经代理）")
        sys.path.insert(0, str(ROOT))
        from tokenlens import TokenLens
        from tokenlens.config import Config
        cfg = Config.load()
        lens = TokenLens(cfg, project="sdk-test")
        rec = lens.record_response(
            {"model": "gpt-4o", "usage": {"prompt_tokens": 1000, "completion_tokens": 250},
             "choices": [{"message": {"content": "hi"}}]},
            request_payload={"messages": [{"role": "user", "content": "hi"}]},
            provider="openai", latency_ms=800)
        check("SDK 记录成功", rec["total_tokens"] == 1250 and rec["cost"] > 0,
              f"tokens={rec['total_tokens']} cost=${rec['cost']:.6f}")

        print("\n[10] CLI")
        out = subprocess.run([sys.executable, "-m", "tokenlens", "stats", "--range", "all"],
                             env=env, cwd=str(ROOT), capture_output=True, text=True)
        check("CLI stats 正常", out.returncode == 0 and "总 tokens" in out.stdout,
              out.stdout.strip().splitlines()[0] if out.stdout else out.stderr[:120])
        out = subprocess.run([sys.executable, "-m", "tokenlens", "top", "--field", "model",
                              "--range", "all"], env=env, cwd=str(ROOT),
                             capture_output=True, text=True)
        check("CLI top 正常", out.returncode == 0 and "成本USD" in out.stdout)
        out = subprocess.run([sys.executable, "-m", "tokenlens", "export", "--out",
                              "/tmp/tokenlens-smoke.csv", "--range", "all"],
                             env=env, cwd=str(ROOT), capture_output=True, text=True)
        csv_ok = Path("/tmp/tokenlens-smoke.csv").exists()
        check("CLI export 正常", out.returncode == 0 and csv_ok)

    finally:
        proxy.terminate()
        mock.terminate()
        try:
            proxy.wait(timeout=8); mock.wait(timeout=8)
        except Exception:
            proxy.kill(); mock.kill()

    passed = sum(1 for _, ok, _ in CHECK if ok)
    print(f"\n{'='*52}\n结果: {passed}/{len(CHECK)} 通过")
    failed = [n for n, ok, _ in CHECK if not ok]
    if failed:
        print("失败项:", ", ".join(failed))
    return 0 if passed == len(CHECK) else 1


if __name__ == "__main__":
    sys.exit(main())
