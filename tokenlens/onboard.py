"""应用自动接入：扫描本机主流 AI 应用，把 base_url 指向 TokenLens 代理。

    tokenlens onboard                     扫描：显示各应用接入状态与手动步骤
    tokenlens onboard --auto              对支持安全改写的应用直接写入（先备份，可还原）
    tokenlens onboard --app claude-code   只处理指定应用
    tokenlens onboard --unwire [应用id]   还原自动写入的改动

自动写入只做「文档明确、可无损还原」的配置（当前仅 Claude Code 的 settings.json）；
GUI 应用配置格式各异且可能被运行中的进程回写，只给精确步骤不代改。
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .config import Config

_BAK_SUFFIX = ".tokenlens-bak"


def _appdata() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))


def _has_vscode_ext(home: Path, ext_id: str) -> bool:
    """VS Code 扩展是否安装：扩展目录 glob + 用户数据目录（Win/macOS/Linux 常见位置）。"""
    ext = home / ".vscode" / "extensions"
    if ext.is_dir() and any(ext.glob(ext_id + "-*")):
        return True
    candidates = [
        Path(os.environ.get("APPDATA", "")) / "Code" / "User" / "globalStorage" / ext_id,
        home / ".config" / "Code" / "User" / "globalStorage" / ext_id,
    ]
    return any(p.exists() for p in candidates)


def _claude_settings(home: Path) -> Path:
    return home / ".claude" / "settings.json"


def _claude_base(cfg: Config) -> str:
    """Claude Code 的 ANTHROPIC_BASE_URL：proxy_base 已含 /v1，
    加 anthropic 别名段后，Claude Code 追加 /v1/messages 即落到原生转发路由。"""
    return f"{cfg.proxy_base}/anthropic"


@dataclass
class App:
    id: str
    name: str
    installed: Callable[[Path], bool]
    steps: Callable[[Config], List[str]]
    primary: Callable[[Path], Path]
    note: str = ""


APPS: List[App] = [
    App(
        id="claude-code", name="Claude Code",
        installed=lambda home: _claude_settings(home).parent.exists(),
        primary=_claude_settings,
        steps=lambda cfg: [
            f"自动写入: tokenlens onboard --app claude-code（写入 {_claude_base(cfg)}）",
            f"或手动: ~/.claude/settings.json 的 env 加 ANTHROPIC_BASE_URL={_claude_base(cfg)}",
        ],
    ),
    App(
        id="codex", name="Codex CLI",
        installed=lambda home: (home / ".codex").exists(),
        primary=lambda home: home / ".codex" / "config.toml",
        steps=lambda cfg: [
            "~/.codex/config.toml 添加:",
            '  [model_providers.tokenlens]',
            f'  base_url = "{cfg.proxy_base}"',
            '  wire_api = "chat"',
            '  并在所用 profile 里设 model_provider = "tokenlens"',
        ],
    ),
    App(
        id="gemini-cli", name="Gemini CLI",
        installed=lambda home: (home / ".gemini").exists(),
        primary=lambda home: home / ".gemini",
        note="走 Google 私有协议，暂不支持透明计量",
        steps=lambda cfg: [
            f"可改用 OpenAI 兼容客户端接入 {cfg.proxy_base}（Gemini 官方兼容端点亦可作上游）",
        ],
    ),
    App(
        id="cursor", name="Cursor",
        installed=lambda home: (home / ".cursor").exists(),
        primary=lambda home: home / ".cursor",
        steps=lambda cfg: [
            f"设置 → Models → 勾选 OpenAI API Key → Override Base URL 填 {cfg.proxy_base}",
            "添加自定义模型名（需上游厂商 API Key）",
        ],
    ),
    App(
        id="continue", name="Continue (VS Code/JetBrains)",
        installed=lambda home: (home / ".continue").exists(),
        primary=lambda home: home / ".continue",
        steps=lambda cfg: [
            f"配置里 models 添加: provider: openai / apiBase: {cfg.proxy_base}",
        ],
    ),
    App(
        id="aider", name="aider",
        installed=lambda home: (home / ".aider.conf.yml").exists() or (home / ".aider").exists(),
        primary=lambda home: home / ".aider.conf.yml",
        steps=lambda cfg: [
            f"aider --openai-api-base {cfg.proxy_base}",
            f"或环境变量 OPENAI_API_BASE={cfg.proxy_base}",
        ],
    ),
    App(
        id="chatbox", name="ChatBox",
        installed=lambda home: any(p.exists() for p in (
            _appdata() / "ChatBox", home / ".config" / "ChatBox")),
        primary=lambda home: _appdata() / "ChatBox",
        steps=lambda cfg: [
            f"设置 → 模型 → 添加自定义提供方 → API 域名填 {cfg.proxy_base}",
            "API Key 填上游厂商的 key",
        ],
    ),
    App(
        id="cherry-studio", name="Cherry Studio",
        installed=lambda home: any(p.exists() for p in (
            _appdata() / "CherryStudio", _appdata() / "cherry-studio",
            home / ".config" / "cherry-studio")),
        primary=lambda home: _appdata() / "CherryStudio",
        steps=lambda cfg: [
            f"设置 → 模型服务 → 添加提供商 → API 地址填 {cfg.proxy_base}",
            "选 OpenAI 兼容类型并填入 API Key",
        ],
    ),
    App(
        id="opencode", name="OpenCode",
        installed=lambda home: any(p.exists() for p in (
            home / ".config" / "opencode", _appdata() / "opencode", home / ".opencode")),
        primary=lambda home: home / ".config" / "opencode" / "opencode.json",
        steps=lambda cfg: [
            "项目或全局 opencode.json 的 provider 块添加:",
            '  "tokenlens": {',
            '    "npm": "@ai-sdk/openai-compatible",',
            f'    "options": {{ "baseURL": "{cfg.proxy_base}", "apiKey": "上游key" }},',
            '    "models": { "模型名": {} }',
            "  }",
            "重启后在 /models 里选择",
        ],
    ),
    App(
        id="qwen-code", name="Qwen Code",
        installed=lambda home: (home / ".qwen").exists(),
        primary=lambda home: home / ".qwen",
        steps=lambda cfg: [
            f"export OPENAI_BASE_URL={cfg.proxy_base}",
            "export OPENAI_API_KEY=<上游key>   export OPENAI_MODEL=<模型名>",
            "进入 Qwen Code 后用 /auth 切到 OpenAI 兼容模式",
        ],
    ),
    App(
        id="iflow", name="iFlow CLI",
        installed=lambda home: (home / ".iflow").exists(),
        primary=lambda home: home / ".iflow" / "settings.json",
        steps=lambda cfg: [
            f"~/.iflow/settings.json 配置自定义 OpenAI 兼容端点（API 地址 {cfg.proxy_base}），"
            "或用 IFLOW_ 前缀环境变量",
        ],
    ),
    App(
        id="cline", name="Cline (VS Code)",
        installed=lambda home: _has_vscode_ext(home, "saoudrizwan.claude-dev"),
        primary=lambda home: home / ".vscode" / "extensions",
        steps=lambda cfg: [
            "Cline 侧边栏设置 → API Provider 选 OpenAI Compatible",
            f"Base URL 填 {cfg.proxy_base}，再填上游 API Key 与模型名",
        ],
    ),
    App(
        id="roo-code", name="Roo Code (VS Code)",
        installed=lambda home: _has_vscode_ext(home, "RooVeterinaryInc.roo-cline"),
        primary=lambda home: home / ".vscode" / "extensions",
        steps=lambda cfg: [
            "Roo Code 设置 → API Provider 选 OpenAI Compatible",
            f"Base URL 填 {cfg.proxy_base}，再填上游 API Key 与模型名",
        ],
    ),
    App(
        id="deepchat", name="DeepChat",
        installed=lambda home: any(p.exists() for p in (
            _appdata() / "DeepChat", home / ".config" / "DeepChat")),
        primary=lambda home: _appdata() / "DeepChat",
        steps=lambda cfg: [
            f"设置 → 模型服务商 → 添加 OpenAI 兼容提供商，API 地址填 {cfg.proxy_base}",
        ],
    ),
]


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _atomic_write_text(path: Path, text: str):
    """temp + rename 原子写：进程被杀/断电也不会留下截断的坏 JSON。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _marker_path(p: Path) -> Path:
    return p.with_suffix(p.suffix + ".tokenlens-wired")


def _wire_claude(cfg: Config, home: Path) -> str:
    p = _claude_settings(home)
    data = _read_json(p) if p.exists() else {}
    if data is None:
        return f"[claude-code] {p} 不是有效 JSON，已跳过自动写入（不覆盖可疑配置）"
    current = (data.get("env") or {}).get("ANTHROPIC_BASE_URL")
    target = _claude_base(cfg)
    if current == target:
        return f"[claude-code] 已是接入状态（{target}）"
    p.parent.mkdir(parents=True, exist_ok=True)
    bak = p.with_suffix(p.suffix + _BAK_SUFFIX)
    if p.exists() and not bak.exists():
        shutil.copy2(p, bak)  # 只备份首次接入前的原始状态，重复接入不覆盖
    env = data.setdefault("env", {})
    if not isinstance(env, dict):
        return f"[claude-code] {p} 的 env 不是对象，已跳过自动写入"
    env["ANTHROPIC_BASE_URL"] = target
    _atomic_write_text(p, json.dumps(data, ensure_ascii=False, indent=2))
    # 记录本次写入的地址：之后端口改了 unwire 也认得（否则只比对当前端口会拒还原）
    _atomic_write_text(_marker_path(p), json.dumps({"base_url": target}, ensure_ascii=False))
    return f"[claude-code] 已写入 ANTHROPIC_BASE_URL={target}" + (f"（备份: {bak}）" if bak.exists() else "")


def _unwire_claude(cfg: Config, home: Path) -> str:
    p = _claude_settings(home)
    if not p.exists():
        return "[claude-code] 未找到 settings.json，无需还原"
    data = _read_json(p)
    bak = p.with_suffix(p.suffix + _BAK_SUFFIX)
    if data is None:
        # 仅当文件损坏时才用备份整文件兜底；正常路径绝不覆盖用户文件
        if bak.exists():
            shutil.copy2(bak, p)
            _marker_path(p).unlink(missing_ok=True)
            return f"[claude-code] {p} 已损坏，已从备份整体还原"
        return f"[claude-code] {p} 不是有效 JSON，不做还原"
    env = data.get("env")
    if not isinstance(env, dict):
        return f"[claude-code] {p} 的 env 不是对象，不做还原"
    current = env.get("ANTHROPIC_BASE_URL")
    if current is None:
        _marker_path(p).unlink(missing_ok=True)
        return "[claude-code] 未接入（无 ANTHROPIC_BASE_URL），无需还原"
    # 认得的地址：当前配置的代理地址 + wire 当时写入的地址（端口可能已改）
    allowed = {_claude_base(cfg)}
    try:
        marker = json.loads(_marker_path(p).read_text(encoding="utf-8"))
        if isinstance(marker, dict) and marker.get("base_url"):
            allowed.add(marker["base_url"])
    # aqg: top-level boundary
    except Exception:  # aqg: top-level boundary 标记缺失只影响还原判定范围，不阻断
        pass
    if current not in allowed:
        return f"[claude-code] 当前 base_url 非本代理写入（{current}），未改动"
    # 只还原这一个键：备份里有用户原始 base_url 就恢复它，否则删掉键；
    # 文件其余内容（wire 之后用户/Claude Code 写入的 hooks 等）原样保留
    original = None
    bdata = _read_json(bak) if bak.exists() else None
    if isinstance(bdata, dict) and isinstance(bdata.get("env"), dict):
        original = bdata["env"].get("ANTHROPIC_BASE_URL")
    if original:
        env["ANTHROPIC_BASE_URL"] = original
    else:
        env.pop("ANTHROPIC_BASE_URL", None)
    _atomic_write_text(p, json.dumps(data, ensure_ascii=False, indent=2))
    _marker_path(p).unlink(missing_ok=True)
    return f"[claude-code] 已还原 ANTHROPIC_BASE_URL（其余内容未动，备份保留: {bak}）"


_AUTO = {"claude-code": (_wire_claude, _unwire_claude)}


def wired_base(app_id: str, cfg: Config, home: Optional[Path] = None) -> Optional[str]:
    """返回应用当前生效的接入地址；未接入（或指向第三方中转）返回 None。"""
    home = home or Path.home()
    if app_id == "claude-code":
        p = _claude_settings(home)
        data = _read_json(p) if p.exists() else None
        if isinstance(data, dict):
            v = (data.get("env") or {}).get("ANTHROPIC_BASE_URL")
            # 只有指向本代理才算已接入；指向第三方中转不算（避免 detect 误报）
            return v if v and v == _claude_base(cfg) else None
    return None


def detect(cfg: Optional[Config] = None, home: Optional[Path] = None) -> List[dict]:
    """扫描本机已安装的 AI 应用，返回状态清单。"""
    cfg = cfg or Config()
    home = home or Path.home()
    rows: List[dict] = []
    for app in APPS:
        if not app.installed(home):
            status = "未安装"
        elif app.id in _AUTO and wired_base(app.id, cfg, home=home):
            status = "已接入"
        elif app.id in _AUTO:
            status = "可接入"
        else:
            status = "可接入(手动)"
        rows.append({
            "id": app.id, "name": app.name, "status": status,
            "path": str(app.primary(home)), "note": app.note,
            "auto": app.id in _AUTO,
        })
    return rows


def wire(app_id: str, cfg: Config, home: Optional[Path] = None) -> str:
    """把指定应用接入代理（仅限支持安全改写的应用）。"""
    home = home or Path.home()
    fn = _AUTO.get(app_id)
    if not fn:
        return f"[{app_id}] 不支持自动写入，运行 tokenlens onboard 查看手动步骤"
    app = next((a for a in APPS if a.id == app_id), None)
    if app and not app.installed(home):
        return f"[{app_id}] 未检测到安装"
    return fn[0](cfg, home)


def unwire(app_id: str, cfg: Config, home: Optional[Path] = None) -> str:
    """还原指定应用的自动接入改动。"""
    home = home or Path.home()
    fn = _AUTO.get(app_id)
    if not fn:
        return f"[{app_id}] 没有可还原的自动改动"
    return fn[1](cfg, home)
