# TokenLens 项目深度分析

> 版本: v1.2.1 · 分析时间: 2026-10 · 依据: 逐文件精读 + 运行验证 + 修复记录

## 1. 项目定位与架构

TokenLens 是**零侵入的本地 AI Token 用量监控**工具：以本地透明代理为入口，旁路计量 LLM 调用并落库，仪表盘展示成本与用量，支持预算告警和 CLI/SDK 埋点。

```
客户端(OpenAI/Anthropic 兼容 SDK)
   │  base_url 指向本地代理
   ▼
tokenlens proxy.py ──转发──▶ 上游 LLM API
   │  (SSE 行缓冲解析 / 共享连接池 / 链路头)
   ▼
meter.py ──▶ store.py (SQLite)
   │             │
   ├─ 成本计算(pricing.py + tokenizer.py)
   ├─ 预算告警(webhook 推送)
   └─ 仪表盘 API(api.py) + 前端(web/index.html)
```

分层职责：`proxy`(流量入口) → `meter`(计量与策略) → `store`(存储与聚合) → `api`(展示与操作)。核心设计取舍：

- **透明代理优先**：不改业务代码即可接入，SDK 埋点为补充路径
- **流式精确计量**：SSE 按行缓冲解析，跨 chunk 也能完整提取 usage
- **读连接 + 独立写连接**：避免长读连接阻塞写请求
- **价格表外部同步**：LiteLLM 源 + 内置兜底 + 本地 override

## 2. 安全加固清单（v1.2.1）

| 级别 | 问题 | 修复 |
|---|---|---|
| 高危 | 仪表盘 tooltip 存储型 XSS | 前端统一 `esc()` 转义（含明细/详情/提示） |
| 中危 | 上游地址 SSRF | `_reject_unsafe_upstream`：内网/保留网段/链路本地拦截 |
| 中危 | 写接口无鉴权 | 对外监听且无 token 时禁止修改类操作 |
| 中危 | CSV 公式注入 | 导出时 `=+-@` 前缀字段前置单引号 |
| 低危 | 阻塞请求/延迟污染 | 计量走线程 + 客户端断开不计延迟 |
| 低危 | SDK 装饰器崩溃 | `functools.iscoroutinefunction` → `asyncio` 版 |
| 低危 | 数据无限增长 | `prune` 保留天数清理 + 清空时同步清告警 |
| 低危 | 导出内存 | 流式分页 5000 条/批 |
| 低危 | 版本漂移 | 统一 1.2.1（pyproject/`__init__`/Docker） |
| 低危 | 轮询翻页错位 | 重置分页偏移 |
| 低危 | CJK 计数误判 | 正则收敛到 CJK 统一表意文字区 |

另有 `ConfigPatch` 缺字段校验、webhook 日志、SECURITY 链接指向 GitCode 等完善项。

## 3. 工程评估

- **测试**：单元 37/37、冒烟 46/46（转发/流式/并发 50/预算/拦截/SSRF/链路头/会话归因/JSONL/SDK/CLI）
- **类型**：mypy 12 源文件 0 错误（CI 强制）
- **CI**：compileall + 依赖 + mypy + 单测 + 冒烟；价格表每月 cron 自动同步
- **CLI**：start/stats/top/export/import-csv/pricing/budget/alerts/seed-demo/prune/reset/live/config/doctor
- **部署**：Docker（3.11-slim + 健康检查 + 数据卷）

## 4. 竞品生态位

| 维度 | TokenLens | Helicone | OpenLIT | LangSmith/Langfuse |
|---|---|---|---|---|
| 部署 | 本地单机 | SaaS/自托管 | 自托管 | SaaS/自托管 |
| 接入 | 透明代理 | 代理 | SDK/OTel | SDK 深度 |
| 计量 | usage 精确+估算 | 精确 | 精确 | 精确 |
| 定位 | 个人/小团队成本账本 | 团队可观测 | 开源可观测 | 全链路 LLM 工程 |

生态位：**个人开发者/小团队的本地优先成本监控**——零部署成本、零侵入、无数据出域；与 OTel 生态型产品互补而非竞争。

## 5. 健康度

- 功能完整度: 高（计量/预算/告警/多端接入齐全）
- 安全基线: 修复后中高（对外部署需配置 token）
- 工程成熟度: 中高（测试/类型/CI 齐备，Docker 待实测）
- 扩展性: 中（SQLite 单机，Postgres 留待重构）

## 6. 路线图状态（v1.2.1）

| 项 | 状态 |
|---|---|
| session 会话归因 | ✅ 请求头/SDK 埋点 + 全查询筛选 + 仪表盘下拉 |
| CSV 多机合并导入 | ✅ request_id 幂等去重，往返不重复 |
| JSONL 导出 | ✅ 规避 CSV 公式注入 |
| Docker 部署 | ✅ 文件就绪，镜像构建待实测 |
| CI 价格刷新 + mypy 门禁 | ✅ cron + 强制检查 |
| 可插拔存储(SQLite→Postgres) | ⏳ 架构级重构，另迭代 |

## 7. 后续建议（按优先级）

1. **对外部署安全**：非回环监听必须 `dashboard_token`；建议 HTTPS 反代
2. **Postgres 后端**：多机并发写入场景再评估
3. **告警渠道**：当前 webhook 卡片，可加 email/SMS
4. **多租户**：`key_hash` 已预留维度，可扩展团队隔离
5. **导出订阅**：定时把 JSONL 推送到对象存储
