# 更新日志

所有显著变更记录在本文件。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

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
