"""本地应用用量检测：只读扫描本机 AI 应用的会话记录，导入统一账本。

零侵入：不改应用配置、不需要 API key、外部库只读（file:...?mode=ro）。
    tokenlens scan-local            扫描全部支持的应用并导入（幂等，重复扫描不重复计费）
    tokenlens scan-local --app opencode

支持应用：
  opencode      ~/.local/share/opencode/opencode.db 的 session 表（每会话聚合 tokens）
  zcode         ~/.zcode/cli/rollout/model-io-sess_*.jsonl（每行一次模型调用，tokens 齐全）
  claude-code   ~/.claude/projects/**/*.jsonl 的 assistant 行（usage 字段，格式已实测验证）
Cursor 为闭源 SQLite 格式且随版本变动，不扫描（走手动接入代理）。

OpenCode session.model 是 JSON 串（{"id","providerID","variant"}），时间戳为毫秒；
ZCode model 是 {"modelId","providerId"}、时间为 ISO UTC；Claude Code 时间为 ISO UTC。
cost 一律取应用自记值，缺省 0（订阅/免费额度），如实记录不臆造价格。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict

from .config import Config
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

# 应用 id → 该应用本地扫描入库用的 key_hash（/api/apps 计数与面板展示用）
SCAN_KEY_HASHES = {
    "opencode": "opencode-local",
    "zcode": "zcode-local",
    "claude-code": "claude-local",
}


def import_local(cfg: Config, store: Store, app: str = "", home: Any = None) -> Dict[str, Any]:
    """扫描（可按应用过滤）并幂等导入账本。返回 {app: {found, imported, skipped}}。
    home 仅测试注入用，默认真实用户目录。"""
    home = home or Path.home()
    result: Dict[str, Any] = {}
    for name, scan in SCANNERS.items():
        if app and name != app:
            continue
        recs = scan(home)
        if not recs:
            result[name] = {"found": 0, "imported": 0, "skipped": 0}
            continue
        stats = store.import_rows(recs)
        result[name] = {"found": len(recs), "imported": stats.get("imported", 0),
                        "skipped": stats.get("skipped", 0) + stats.get("duplicates", 0)}
    return result


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
