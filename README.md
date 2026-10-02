<div align="center">

<img src="docs/media/brand-wide.png" alt="TokenLens" width="720">

# TokenLens

**Every AI call, metered — tokens and spend in a ledger that stays on your machine.**

Zero-intrusion transparent proxy · Local-first · 37+ upstreams · 5-minute setup

[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](pyproject.toml)
[![Tests](https://img.shields.io/badge/tests-137%20passing-brightgreen)](scripts/smoke_test.py)
[![Version](https://img.shields.io/badge/version-1.3.0-orange)](CHANGELOG.md)

[Website](https://x33834.github.io/tokenlens/) ｜ [中文](README.zh-CN.md) ｜ [日本語](README.ja.md) ｜ [Changelog](CHANGELOG.md)

</div>

---

<table>
<tr><td width="50%">

**The problem you know too well**

- The bill arrives before you notice the burn
- Multiple projects and apps share one key; nobody can say where the money went
- Usage caps mean babysitting a dashboard
- Cloud monitoring wants your prompts and your keys

</td><td width="50%">

**How TokenLens handles it**

- Every call is metered in real time: tokens, cost, latency, success/failure
- Split by project / app / model / provider — see exactly who is spending
- Threshold alerts, and over-budget calls get a 402 before reaching the provider
- Local-first: metadata only, prompts/responses never stored, keys reduced to fingerprints

</td></tr>
</table>

## Download

> **Windows**: grab the single-file `TokenLens.exe` from [Releases](https://github.com/x33834/tokenlens/releases/latest) — double-click, proxy + dashboard come up on :8788.
> **Star the repo** if it saves you money — it genuinely helps other people find it: [github.com/x33834/tokenlens](https://github.com/x33834/tokenlens/stargazers)

## Up and running in 60 seconds

```bash
# 1. Install and start (default port 8787)
pip install . && python -m tokenlens start

# 2. Point any OpenAI-compatible client at it
export OPENAI_BASE_URL=http://127.0.0.1:8787/v1

# 3. Open the spend ledger
http://127.0.0.1:8787
```

That's the whole integration. No business code changes, no account, no cloud. No real API key needed to try it: `python -m tokenlens seed-demo` loads demo data, `python examples/mock_upstream.py` runs a mock provider.

### Other ways to run it

| Method | Good for | Command |
|---|---|---|
| pip / source | daily development | the three lines above |
| Docker | servers / NAS | `docker compose up -d` ([Dockerfile](Dockerfile)) |
| Windows, no install | desktop users | single-file PyInstaller build `dist/TokenLens.exe` — double-click, proxy + dashboard on :8788 |
| Multi-machine | work split across boxes | export CSV on each → `tokenlens import-csv` merges idempotently |

## What it looks like

<video src="docs/media/tokenlens-promo.mp4" controls width="720" poster="docs/media/desktop.png"></video>

*22s real capture: skeleton loading → animated KPI counters → chart dimensions → row expansion → settings drawer → alert timeline. There's also a [9-second quick cut](docs/media/tokenlens-demo-live.webm).*

| Desktop | Mobile |
|---|---|
| <img src="docs/media/desktop.png" alt="Desktop dashboard" width="100%"> | <img src="docs/media/mobile.png" alt="Mobile layout" width="72%"> |

| Settings drawer | Alert timeline |
|---|---|
| <img src="docs/media/drawer.png" alt="Settings drawer" width="100%"> | <img src="docs/media/alerts.png" alt="Alert timeline" width="100%"> |

## Architecture

<img src="docs/media/architecture.svg" alt="Architecture: apps → TokenLens (proxy / metering / ledger / dashboard) → upstream LLMs" width="100%">

Design notes worth reading:

- **Three-tier metering fallback**: prefer the upstream's own usage (exact), then tiktoken, then heuristics — the ledger always has numbers, even offline or on unknown models
- **Streaming is fully metered**: SSE parsed chunk-by-chunk, `include_usage` auto-injected so the provider reports real usage, survives chunk-split events
- **Budget enforcement happens before forwarding**: rejected calls cost you nothing upstream
- **Keys are stored as fingerprints**: first 12 hex chars of SHA-256, mappable to friendly app names ("Claude Code", not a hash)

## Feature map

| | What it does |
|---|---|
| Proxy | OpenAI-compatible; SSE streaming; 37+ path-alias upstreams (`/v1/deepseek/…`); redirect hops re-validated |
| Metering | tokens (incl. cached / reasoning), cost (LiteLLM price table, 2,000+ models + manual overrides), latency / TTFT / P95 |
| Attribution | project (header) · session · app (key-fingerprint alias) · provider / model / endpoint |
| Dashboard | today / monthly spend, budget bars, trends (cost / tokens / requests / latency), cost breakdown, sortable detail, offline demo data |
| Budget | daily / monthly, 80% alert, 402 hard block; DingTalk / WeCom / Feishu webhooks |
| Data | CSV export (formula-injection safe) / JSONL export / idempotent CSV merge; retention auto-prune |
| Onboarding | `tokenlens onboard` scans local AI apps and wires them safely (Claude Code, reversible); SDK instrumentation (decorator / openai patch) |
| Local usage detection | zero-intrusion read-only scan of local AI app records — OpenCode (SQLite), ZCode (model IO logs), Claude Code (session JSONL) — idempotently imported by `request_id`; auto-scan at startup & every 10 min, or `tokenlens scan-local` / dashboard button (`TOKENLENS_LOCAL_SCAN=0` to disable) |
| App panel | dashboard strip listing every installed AI app and how it is metered (local records count / auto-wire / manual / not meterable) |
| Windows tray | double-click the exe to run in the system tray (open dashboard / quit); terminal runs and CLI subcommands unchanged; falls back to console mode without tray deps |
| Security | local-first; dashboard_token auth; CSRF protection; DNS-rebinding protection; request size caps; full analysis in [docs/ANALYSIS.md](docs/ANALYSIS.md) |

## Quality

```
Unit tests          59/59   (metering / storage / auth / pricing / SDK)
Onboard tests       26/26   (wire / unwire / aliases)
Local scan tests    23/23   (parsers / idempotent import / bad data)
Apps API tests      7/7     (metering flags / record counts)
End-to-end smoke    52/52   (real processes: proxy → mock upstream → assert ledger)
Type check          mypy 0 errors (CI gate)
Adversarial suite   31 checks (SSRF / injection / CSRF / concurrency / metering accuracy)
```

## Known limits (stated honestly)

- Budget enforcement is best-effort under extreme concurrency (check-then-act race); fine for personal scale
- The price table is a snapshot (synced monthly from LiteLLM); provider price changes can lag — override in settings
- Postgres backend is on the roadmap; SQLite only for now

## Docs

[Changelog](CHANGELOG.md) · [Architecture & security analysis](docs/ANALYSIS.md) · [Contributing](CONTRIBUTING.md) · [Security policy](SECURITY.md) · [Website](https://x33834.github.io/tokenlens/en/)

## License

[MIT](LICENSE) — use it freely. If you come back and open an issue about what you built with it, even better.
