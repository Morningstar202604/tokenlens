# 更新日志

所有显著变更记录在本文件。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.3.0] - 2026-10-03

### 新增
- 本地应用用量检测扩展到 10 个应用：在 OpenCode/ZCode/Claude Code 之外新增
  Codex CLI（rollout token_count 增量）、Gemini CLI / Qwen Code / iFlow（chats 会话
  tokens 摘要）、aider（.aider.llm.history 自记成本）、Cline / Roo Code（编辑器
  globalStorage 任务记录，自记成本）；本机未安装或无会话数据时返回空不报错；服务启动与每 10 分钟自动扫描，仪表盘一键「扫描本机应用记录」，
  CLI `tokenlens scan-local`，`TOKENLENS_LOCAL_SCAN=0` 可关闭
- 仪表盘「本机 AI 应用」状态条：展示每个已装应用的计量方式（本地已计量条数 /
  可自动接入 / 手动接入 / 不计量），数据来自 onboard 检测与各扫描器入库计数（GET /api/apps）
- Windows 托盘常驻：双击 exe 隐藏控制台缩到系统托盘，左键开仪表盘、右键退出；
  终端运行与 CLI 子命令不受影响，托盘依赖缺失自动回退控制台模式（可选依赖 `tray` extra）

### 修复
- 本地扫描行成本不再恒为 0：未记价时按价格表牌价估算（cost_source 以 -est 标记），
  价格表更新后自动幂等回填存量记录；-free 免费档与未知价模型保持 0 不臆造，
  仪表盘成本构成对两者分别显示「免费 / 单价未知」
- 仪表盘「先看演示数据」改为直接渲染前端内置数据集，不再调用 /api/seed 往真实账本写入假记录；
  演示态带全宽横幅与「演示数据」水印，点任意时间范围 / 切换维度或筛选即退出
- 仪表盘 HTML 响应补 `Cache-Control: no-cache`，升级后浏览器不再使用旧缓存页面
- `reset` 输出提示本机应用记录会被自动扫描重新导入的行为

## [1.2.1] - 2026-09-30 ~ 2026-10-02

### 新增
- 会话归因：请求头 / SDK 埋点打 session 标签，仪表盘可按会话筛选
- CSV 多机合并导入（按 request_id 幂等去重）与 JSONL 导出（规避 CSV 公式注入）
- `tokenlens onboard`：扫描本机 AI 应用并自动接入（Claude Code 安全改写、备份可还原）
- 密钥别名：API Key 指纹映射应用名，仪表盘直接显示「谁在花钱」
- 上游目录扩至 37 家（海外 / 国内 / 本地推理运行时），路径别名直连
- Windows 单文件发行（PyInstaller），双击即用；Docker 部署就绪
- CI：mypy 类型门禁 + 每月自动同步 LiteLLM 价格表

### 安全
- 全量审查修复 26 项：上游重定向逐跳 SSRF 校验（跨主机跳转剥离凭据）、
  timeseries 时间范围上限与补桶硬上限、写操作 CSRF 自定义头、回环部署 Host 校验、
  配置负值拒绝、请求体 / 缓冲响应大小上限、token 常量时间比较、查询参数钳位
- `onboard --unwire` 改为只还原接入键，不再整文件覆盖用户后续配置；写盘原子化

### 修复
- SDK track 失败时 model 丢失；Anthropic / Azure 头（x-api-key / api-key）指纹兜底
- 启动按 retention_days 自动清理；SDK 流式调用完整记账；configure() 后新配置即时生效

## [1.2.0] - 2026-09-24

### 新增
- 预算硬拦截：超限请求 402 直接拒绝不转发上游，可配置开关与阈值，明细标「拒」
- 仪表盘整页重构：花销账本叙事、ECharts 趋势 / 成本构成、预算直改、设置抽屉、明细排序筛选分页
- 价格表同步 LiteLLM（2400+ 模型）；告警支持钉钉 / 企业微信 / 飞书卡片
- CLI 完整化：stats / top / export / import-csv / config / live / alerts / doctor

### 优化
- P95 延迟改 SQL 端取分位行，不再全表拉取；共享连接池；SQLite 独立写连接
- dashboard_token 最小鉴权；移动端适配打磨

## [1.1.0] - 2026-09-23

### 新增
- 轻量 CI、PR / Issue 模板、贡献与安全披露文档、发布流程清单

## [1.0.0] - 2026-09-19

### 新增
- 首个公开版本：透明代理 + token / 成本计量 + 花销账本仪表盘 + 预算告警
- 三级计量降级（上游 usage → tiktoken → 启发式）；本地 SQLite 存储，数据不出本机
