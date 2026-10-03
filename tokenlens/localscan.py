"""本地应用用量检测：只读扫描本机 AI 应用的会话记录，导入统一账本。

零侵入：不改应用配置、不需要 API key、外部库只读（file:...?mode=ro）。
    tokenlens scan-local            扫描全部支持的应用并导入（幂等，重复扫描不重复计费）
    tokenlens scan-local --app opencode

支持应用：
  opencode      ~/.local/share/opencode/opencode.db 的 session 表（每会话聚合 tokens）
  zcode         ~/.zcode/cli/rollout/model-io-sess_*.jsonl（每行一次模型调用，tokens 齐全）
  claude-code   ~/.claude/projects/**/*.jsonl 的 assistant 行（usage 字段，格式已实测验证）
  codex         ~/.codex/sessions/**/rollout-*.jsonl 的 token_count 事件（last_token_usage 增量）
  gemini-cli    ~/.gemini/tmp/*/chats/session-*.jsonl 的 gemini 消息 tokens 摘要
  qwen-code     ~/.qwen/tmp/*/chats/session-*.jsonl（同上，Gemini CLI 分支）
  iflow         ~/.iflow/tmp/*/chats/session-*.jsonl（同上）
  aider         ~、~/、~/*/ 两层内的 .aider.llm.history（LiteLLM 响应，自记 cost）
  cline         编辑器 globalStorage 的 tasks/*/ui_messages.json（api_req_started，自记 cost）
  roo-code      同 cline（RooVeterinaryInc.roo-cline）
Cursor 为闭源 SQLite 格式且随版本变动，不扫描（走手动接入代理）。

OpenCode session.model 是 JSON 串（{"id","providerID","variant"}），时间戳为毫秒；
ZCode model 是 {"modelId","providerId"}、时间为 ISO UTC；Claude Code 时间为 ISO UTC。
cost 取应用自记值；未记价时按牌价估算（cost_source 以 -est 标记），
-free 档位与价格表未收录的模型保持 0，如实记录不臆造价格。
"""

from __future__ import annotations

import hashlib
import json

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict

from .config import Config
from .pricing import CostCalculator, PricingTable
from .store import Store

AUTO_SCAN_INTERVAL = 600.0


def _opencode_db(home: Path) -> Path:
    return home / ".local" / "share" / "opencode" / "opencode.db"


def _parse_model(raw: str) -> tuple:
    """'{"id":"m","providerID":"p"}' → (m, p)；坏 JSON → (unknown, opencode)。"""
    try:
        d = json.loads(raw)
        if isinstance(d, dict):
            return str(d.get("id") or "unknown"), str(d.get("providerID") or "opencode")
    except Exception:  # aqg: top-level boundary 单行坏数据不阻断整库扫描
        pass
    return "unknown", "opencode"


def scan_opencode(db_path: Path) -> list:
    """只读扫描 OpenCode session 表；库不存在/被锁/无表 → 空列表。"""
    if not db_path.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, directory, model, cost, tokens_input, tokens_output, "
            "tokens_reasoning, tokens_cache_read, time_updated FROM session"
        ).fetchall()
        con.close()
    except Exception:  # aqg: top-level boundary 用户库可能正被 OpenCode 写入
        return []
    recs = []
    for r in rows:
        pin = r["tokens_input"] or 0
        pout = r["tokens_output"] or 0
        if pin <= 0 and pout <= 0:
            continue  # 空会话不产生账本行
        ts = (r["time_updated"] or 0) / 1000.0
        lt = time.localtime(ts)
        model, provider = _parse_model(r["model"] or "")
        recs.append({
            "request_id": f"opencode:{r['id']}",
            "ts": ts,
            "day": time.strftime("%Y-%m-%d", lt),
            "hour": lt.tm_hour,
            "provider": provider,
            "upstream": "",
            "model": model,
            "endpoint": "opencode-local",
            "project": Path(r["directory"] or "").name or "opencode",
            "key_hash": "opencode-local",
            "is_stream": 0,
            "prompt_tokens": pin,
            "completion_tokens": pout,
            "total_tokens": pin + pout,
            "cached_tokens": r["tokens_cache_read"] or 0,
            "reasoning_tokens": r["tokens_reasoning"] or 0,
            "cost": max(float(r["cost"] or 0.0), 0.0),
            "cost_source": "opencode-local",
            "latency_ms": 0,
            "status": 200,
            "session_id": r["id"],
        })
    return recs


SCANNERS = {"opencode": lambda home: scan_opencode(_opencode_db(home))}


def _iso_ts(s) -> float:
    """ISO 8601（含 Z 后缀）→ epoch 秒；解析失败返回 0（调用方跳过该行）。"""
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:  # aqg: top-level boundary 单行坏时间不阻断整库扫描
        return 0.0


def scan_zcode(rollout_dir: Path) -> list:
    """只读扫描 ZCode 模型 IO 日志：每行一次模型调用，含 tokens 与请求 UUID。"""
    if not rollout_dir.exists():
        return []
    recs = []
    for f in rollout_dir.glob("model-io-sess_*.jsonl"):
        try:
            fh = open(f, encoding="utf-8")
        except OSError:
            continue
        with fh:
            for ln in fh:
                try:
                    d = json.loads(ln)
                except Exception:  # aqg: top-level boundary 单行坏数据不阻断
                    continue
                resp = d.get("response") or {}
                u = resp.get("usage") or {}
                pin = int(u.get("inputTokens") or 0)
                pout = int(u.get("outputTokens") or 0)
                if pin <= 0 and pout <= 0:
                    continue
                ts = _iso_ts(d.get("completedAt") or d.get("startedAt"))
                if ts <= 0:
                    continue
                m = d.get("model")
                if isinstance(m, dict):
                    model = str(m.get("modelId") or "unknown")
                    pid = str(m.get("providerId") or "")
                else:
                    model = str(m or resp.get("modelId") or "unknown")
                    pid = ""
                provider = "zhipu" if "bigmodel" in pid else (pid.split(":")[-1] or "zcode")
                rid = d.get("requestId") or f"{d.get('sessionId')}:{d.get('startedAt')}"
                lt = time.localtime(ts)
                recs.append({
                    "request_id": f"zcode:{rid}",
                    "ts": ts,
                    "day": time.strftime("%Y-%m-%d", lt),
                    "hour": lt.tm_hour,
                    "provider": provider,
                    "upstream": "",
                    "model": model,
                    "endpoint": "zcode-local",
                    "project": "zcode",
                    "key_hash": "zcode-local",
                    "is_stream": 0,
                    "prompt_tokens": pin,
                    "completion_tokens": pout,
                    "total_tokens": pin + pout,
                    "cached_tokens": int(u.get("cacheReadTokens") or 0),
                    "reasoning_tokens": 0,
                    "cost": 0.0,
                    "cost_source": "zcode-local",
                    "latency_ms": int(d.get("durationMs") or 0),
                    "status": 200,
                    "session_id": d.get("sessionId"),
                })
    return recs


def scan_claude(projects_dir: Path) -> list:
    """只读扫描 Claude Code 本地会话：projects/**/*.jsonl 的 assistant 行。"""
    if not projects_dir.exists():
        return []
    recs = []
    for f in projects_dir.rglob("*.jsonl"):
        try:
            fh = open(f, encoding="utf-8")
        except OSError:
            continue
        with fh:
            for ln in fh:
                try:
                    d = json.loads(ln)
                except Exception:  # aqg: top-level boundary 单行坏数据不阻断
                    continue
                if d.get("type") != "assistant" or d.get("isApiErrorMessage"):
                    continue  # API 报错行 usage 是噪声（999/999 占位）
                m = d.get("message") or {}
                u = m.get("usage") or {}
                pin = int(u.get("input_tokens") or 0)
                pout = int(u.get("output_tokens") or 0)
                if pin <= 0 and pout <= 0:
                    continue
                ts = _iso_ts(d.get("timestamp"))
                if ts <= 0:
                    continue
                rid = d.get("uuid") or f"{d.get('sessionId')}:{d.get('timestamp')}"
                lt = time.localtime(ts)
                recs.append({
                    "request_id": f"claude:{rid}",
                    "ts": ts,
                    "day": time.strftime("%Y-%m-%d", lt),
                    "hour": lt.tm_hour,
                    "provider": "anthropic",
                    "upstream": "",
                    "model": str(m.get("model") or "unknown"),
                    "endpoint": "claude-local",
                    "project": Path(str(d.get("cwd") or "")).name or "claude-code",
                    "key_hash": "claude-local",
                    "is_stream": 0,
                    "prompt_tokens": pin,
                    "completion_tokens": pout,
                    "total_tokens": pin + pout,
                    "cached_tokens": int(u.get("cache_read_input_tokens") or 0),
                    "reasoning_tokens": 0,
                    "cost": 0.0,
                    "cost_source": "claude-local",
                    "latency_ms": 0,
                    "status": 200,
                    "session_id": d.get("sessionId"),
                })
    return recs


SCANNERS["zcode"] = lambda home: scan_zcode(home / ".zcode" / "cli" / "rollout")
SCANNERS["claude-code"] = lambda home: scan_claude(home / ".claude" / "projects")


def scan_codex(sessions_dir: Path) -> list:
    """只读扫描 OpenAI Codex CLI rollout：~/.codex/sessions/**/rollout-*.jsonl。
    token_count 事件的 last_token_usage 是自上次事件以来的增量（2025-09 起）。"""
    if not sessions_dir.exists():
        return []
    recs = []
    for f in sessions_dir.rglob("rollout-*.jsonl"):
        sid, model, cwd, seq = "", "unknown", "", 0
        try:
            fh = open(f, encoding="utf-8")
        except OSError:
            continue
        with fh:
            for ln in fh:
                try:
                    d = json.loads(ln)
                except Exception:  # aqg: top-level boundary 单行坏数据不阻断
                    continue
                t, p = d.get("type"), d.get("payload") or {}
                if t == "session_meta":
                    sid = str(p.get("id") or sid)
                    cwd = str(p.get("cwd") or "")
                elif t == "turn_context" and p.get("model"):
                    model = str(p["model"])
                elif t == "event_msg" and p.get("type") == "token_count":
                    u = (p.get("info") or {}).get("last_token_usage") or {}
                    pin = int(u.get("input_tokens") or 0)
                    pout = int(u.get("output_tokens") or 0)
                    if pin <= 0 and pout <= 0:
                        continue
                    seq += 1
                    ts = _iso_ts(d.get("timestamp"))
                    if ts <= 0:
                        continue
                    lt = time.localtime(ts)
                    recs.append({
                        "request_id": f"codex:{sid}:{seq}",
                        "ts": ts,
                        "day": time.strftime("%Y-%m-%d", lt),
                        "hour": lt.tm_hour,
                        "provider": "openai",
                        "upstream": "",
                        "model": model,
                        "endpoint": "codex-local",
                        "project": Path(cwd).name or "codex",
                        "key_hash": "codex-local",
                        "is_stream": 0,
                        "prompt_tokens": pin,
                        "completion_tokens": pout,
                        "total_tokens": pin + pout,
                        "cached_tokens": int(u.get("cached_input_tokens") or 0),
                        "reasoning_tokens": int(u.get("reasoning_output_tokens") or 0),
                        "cost": 0.0,
                        "cost_source": "codex-local",
                        "latency_ms": 0,
                        "status": 200,
                        "session_id": sid,
                    })
    return recs


def scan_gemini_like(home: Path, base_name: str, app: str) -> list:
    """只读扫描 Gemini CLI 及其分支（Gemini/Qwen Code/iFlow）会话：
    ~/<base>/tmp/<项目>/chats/session-*.jsonl，type=gemini 的消息行带 tokens 摘要
    （input=promptTokenCount, output=candidatesTokenCount, cached, thoughts）。"""
    base = home / base_name / "tmp"
    if not base.exists():
        return []
    recs = []
    for f in base.glob(f"*/chats/*.jsonl"):
        sid = ""
        try:
            fh = open(f, encoding="utf-8")
        except OSError:
            continue
        with fh:
            for ln in fh:
                try:
                    d = json.loads(ln)
                except Exception:  # aqg: top-level boundary 单行坏数据不阻断
                    continue
                if not isinstance(d, dict):
                    continue
                if d.get("kind") and d.get("sessionId"):
                    sid = str(d["sessionId"])  # 元数据行
                    continue
                if d.get("type") != "gemini":
                    continue
                tk = d.get("tokens") or {}
                pin, pout = int(tk.get("input") or 0), int(tk.get("output") or 0)
                if pin <= 0 and pout <= 0:
                    continue
                ts = _iso_ts(d.get("timestamp"))
                if ts <= 0:
                    continue
                lt = time.localtime(ts)
                recs.append({
                    "request_id": f"{app}:{sid or f.parent.parent.name}:{d.get('id')}",
                    "ts": ts,
                    "day": time.strftime("%Y-%m-%d", lt),
                    "hour": lt.tm_hour,
                    "provider": "google",
                    "upstream": "",
                    "model": str(d.get("model") or "unknown"),
                    "endpoint": f"{app}-local",
                    "project": app,
                    "key_hash": f"{app}-local",
                    "is_stream": 0,
                    "prompt_tokens": pin,
                    "completion_tokens": pout,
                    "total_tokens": int(tk.get("total") or pin + pout),
                    "cached_tokens": int(tk.get("cached") or 0),
                    "reasoning_tokens": int(tk.get("thoughts") or 0),
                    "cost": 0.0,
                    "cost_source": f"{app}-local",
                    "latency_ms": 0,
                    "status": 200,
                    "session_id": sid,
                })
    return recs


def scan_aider(home: Path) -> list:
    """只读扫描 aider 的 .aider.llm.history（LiteLLM 响应 JSONL，自记 cost）。
    零侵入约束下只搜 ~、~/、~/*/ 两层；更深的仓库请走代理接入。"""
    recs = []
    candidates = [home / ".aider.llm.history"]
    candidates += home.glob("*/.aider.llm.history")
    candidates += home.glob("*/*/.aider.llm.history")
    for f in candidates:
        try:
            fh = open(f, encoding="utf-8")
        except OSError:
            continue
        with fh:
            for i, ln in enumerate(fh):
                try:
                    d = json.loads(ln)
                except Exception:  # aqg: top-level boundary 单行坏数据不阻断
                    continue
                u = d.get("usage") or {}
                pin = int(u.get("prompt_tokens") or 0)
                pout = int(u.get("completion_tokens") or 0)
                if pin <= 0 and pout <= 0:
                    continue
                ts = float(d.get("created") or f.stat().st_mtime)
                lt = time.localtime(ts)
                recs.append({
                    "request_id": f"aider:{hashlib.md5(str(f).encode()).hexdigest()[:8]}:{i}",
                    "ts": ts,
                    "day": time.strftime("%Y-%m-%d", lt),
                    "hour": lt.tm_hour,
                    "provider": "aider",
                    "upstream": "",
                    "model": str(d.get("model") or "unknown"),
                    "endpoint": "aider-local",
                    "project": f.parent.name,
                    "key_hash": "aider-local",
                    "is_stream": 0,
                    "prompt_tokens": pin,
                    "completion_tokens": pout,
                    "total_tokens": int(u.get("total_tokens") or pin + pout),
                    "cached_tokens": 0,
                    "reasoning_tokens": int(u.get("completion_tokens_details", {}).get("reasoning_tokens", 0) or 0)
                    if isinstance(u.get("completion_tokens_details"), dict) else 0,
                    "cost": max(float(d.get("cost") or 0.0), 0.0),
                    "cost_source": "aider-local",
                    "latency_ms": 0,
                    "status": 200,
                    "session_id": str(d.get("id") or f.name),
                })
    return recs


# Cline/Roo Code 走 VS Code 系编辑器的 globalStorage（Windows 与 Linux/macOS 布局都查）
_EDITOR_ROOTS = ("Code", "Code - Insiders", "Cursor", "Windsurf", "Trae", "VSCodium")


def scan_cline(home: Path, ext_id: str, app: str) -> list:
    """只读扫描 Cline/Roo Code 任务记录：globalStorage/<扩展>/tasks/*/ui_messages.json，
    取 say=api_req_started 的 text JSON（tokensIn/tokensOut/cacheReads/cost，自记）。"""
    recs = []
    appdata = home / "AppData" / "Roaming"
    roots = [appdata / r / "User" / "globalStorage" for r in _EDITOR_ROOTS]
    roots += [home / ".config" / r / "User" / "globalStorage" for r in _EDITOR_ROOTS]
    for root in roots:
        tasks = root / ext_id / "tasks"
        if not tasks.exists():
            continue
        for tdir in tasks.iterdir():
            f = tdir / "ui_messages.json"
            if not f.is_file():
                continue
            try:
                msgs = json.loads(f.read_text(encoding="utf-8"))
            except Exception:  # aqg: top-level boundary 用户文件可能正被写入
                continue
            if not isinstance(msgs, list):
                continue
            for i, m in enumerate(msgs):
                if not isinstance(m, dict) or m.get("say") != "api_req_started":
                    continue
                try:
                    st = json.loads(str(m.get("text") or ""))
                except Exception:  # aqg: top-level boundary text 可能被截断
                    continue
                if not isinstance(st, dict):
                    continue
                pin, pout = int(st.get("tokensIn") or 0), int(st.get("tokensOut") or 0)
                if pin <= 0 and pout <= 0:
                    continue
                ts = float(m.get("ts") or 0) / 1000.0
                if ts <= 0:
                    continue
                model = "unknown"
                try:
                    rq = json.loads(str(st.get("request") or ""))
                    if isinstance(rq, dict) and rq.get("model"):
                        model = str(rq["model"])
                except Exception:  # aqg: top-level boundary request 可能被截断
                    pass
                lt = time.localtime(ts)
                recs.append({
                    "request_id": f"{app}:{tdir.name}:{i}",
                    "ts": ts,
                    "day": time.strftime("%Y-%m-%d", lt),
                    "hour": lt.tm_hour,
                    "provider": app,
                    "upstream": "",
                    "model": model,
                    "endpoint": f"{app}-local",
                    "project": app,
                    "key_hash": f"{app}-local",
                    "is_stream": 0,
                    "prompt_tokens": pin,
                    "completion_tokens": pout,
                    "total_tokens": pin + pout,
                    "cached_tokens": int(st.get("cacheReads") or 0),
                    "reasoning_tokens": 0,
                    "cost": max(float(st.get("cost") or 0.0), 0.0),
                    "cost_source": f"{app}-local",
                    "latency_ms": 0,
                    "status": 200,
                    "session_id": tdir.name,
                })
    return recs


SCANNERS["codex"] = lambda home: scan_codex(home / ".codex" / "sessions")
SCANNERS["gemini-cli"] = lambda home: scan_gemini_like(home, ".gemini", "gemini")
SCANNERS["qwen-code"] = lambda home: scan_gemini_like(home, ".qwen", "qwen")
SCANNERS["iflow"] = lambda home: scan_gemini_like(home, ".iflow", "iflow")
SCANNERS["aider"] = scan_aider
SCANNERS["cline"] = lambda home: scan_cline(home, "saoudrizwan.claude-dev", "cline")
SCANNERS["roo-code"] = lambda home: scan_cline(home, "RooVeterinaryInc.roo-cline", "roo-code")

# 应用 id → 该应用本地扫描入库用的 key_hash（/api/apps 计数与面板展示用）
SCAN_KEY_HASHES = {
    "opencode": "opencode-local",
    "zcode": "zcode-local",
    "claude-code": "claude-local",
    "codex": "codex-local",
    "gemini-cli": "gemini-local",
    "qwen-code": "qwen-local",
    "iflow": "iflow-local",
    "aider": "aider-local",
    "cline": "cline-local",
    "roo-code": "roo-code-local",
}


def _estimator(cfg: Config):
    """按牌价的成本估算器（USD）。-free 档位与价格表未收录的模型返回 None，调用方保持 0。"""
    calc = CostCalculator(PricingTable(cfg.pricing_overrides),
                          cfg.cached_discount, cfg.cached_discounts)

    def est(model, pin, pout, cached, provider):
        if str(model or "").lower().endswith("-free"):
            return None
        return calc.compute(model, pin, pout, cached, provider)

    return est


def _apply_estimate(recs: list, est) -> None:
    """就地为未记价的扫描行估算成本；应用自记值（>0）不动。"""
    for r in recs:
        if (r.get("cost") or 0) > 0:
            continue
        v = est(r.get("model"), r.get("prompt_tokens") or 0, r.get("completion_tokens") or 0,
                r.get("cached_tokens") or 0, r.get("provider"))
        if v and v > 0:
            r["cost"] = round(v, 8)
            r["cost_source"] = str(r.get("cost_source") or "").replace("-local", "-est")


def import_local(cfg: Config, store: Store, app: str = "", home: Any = None) -> Dict[str, Any]:
    """扫描（可按应用过滤）并幂等导入账本。返回 {app: {found, imported, skipped}}。
    home 仅测试注入用，默认真实用户目录。导入后按当前价格表回填存量本地行（幂等）。"""
    home = home or Path.home()
    est = _estimator(cfg)
    result: Dict[str, Any] = {}
    for name, scan in SCANNERS.items():
        if app and name != app:
            continue
        recs = scan(home)
        if not recs:
            result[name] = {"found": 0, "imported": 0, "skipped": 0}
            continue
        _apply_estimate(recs, est)
        stats = store.import_rows(recs)
        result[name] = {"found": len(recs), "imported": stats.get("imported", 0),
                        "skipped": stats.get("skipped", 0) + stats.get("duplicates", 0)}
    store.reprice(est)
    return result


def reprice_local(cfg: Config, store: Store) -> int:
    """按当前价格表重算存量本地扫描行成本（幂等），返回更新行数。"""
    return store.reprice(_estimator(cfg))


def start_autoscan(cfg: Config, store: Store, interval: float = AUTO_SCAN_INTERVAL) -> None:
    """启动即扫一次，之后每 interval 秒后台重扫（幂等，失败静默下轮重试）。
    TOKENLENS_LOCAL_SCAN=0 可关闭（隔离测试环境用）。"""
    import os
    if os.environ.get("TOKENLENS_LOCAL_SCAN", "1").strip().lower() in ("0", "false", "off"):
        return

    def _loop():
        while True:
            try:
                import_local(cfg, store)
            except Exception:  # aqg: top-level boundary 后台扫描失败不影响代理与仪表盘
                pass
            time.sleep(interval)

    threading.Thread(target=_loop, daemon=True, name="tokenlens-localscan").start()
