# TokenLens Android 端技术方案（讨论稿）

> 目标：在 Android 设备上观测「本机各 App 调用云端 AI 服务」的用量，给出 near-realtime 的 token / 成本视图。
> 定位：**个人用量自测（quantified self）**，只采集元数据，不解密内容、不上传、不针对他人设备。

## 1. 结论先行

| 想要的东西 | 默认条件下能否拿到 | 途径 |
|---|---|---|
| 某 App 是否调用了 AI 服务、哪个厂商 | ✅ 能做到 | 域名/SNI + 包名指纹 |
| 该 App 的 AI 交互轮次、时长、流量 | ✅ 能做到 | 连接级统计 |
| 大致 token 数与成本（±20%~50%） | 🟡 能做到，需校准 | 字节→token 模型 |
| 精确的 prompt/completion/cached token | ❌ 默认拿不到 | 需解密（受限于下节） |
| 精确值且不解密 | 🟡 只能对部分场景 | 官方用量 API / 自建网关 |

**一句话**：在普通非 root 手机上，不要指望拿到精确 token；可行且体面的做法是「**流量侧观测 + 估算模型 + 多源校准**」，把误差做成可见的置信区间，而不是假装精确。

## 2. Android 的平台硬约束（决定架构的四条）

### 2.1 TLS / CA：解密路线基本被堵死

- **Android 7（API 24）起**：App 默认只信任**系统 CA**，用户安装的 CA 不被信任（除非 App 自己在 `networkSecurityConfig` 里声明 `user` 源）。第三方商业 AI App 几乎都不会为你开这个口子。
- **Android 11 起**：非托管设备上的 App 不能再通过 `createInstallIntent()` 自动装 CA，必须用户在设置里手动装。
- **Android 14 起**：系统 CA 存储被移进 APEX 容器（`com.android.conscrypt`），即使有 root 也不能直接改系统证书列表，只能通过 Magisk 模块挂载等 hack 手段，稳定性随版本走。
- **Device Owner / MDM**：`DevicePolicyManager.installCaCert()` 装的也是**用户 CA**，不是系统 CA——对企业托管场景有帮助，但对第三方 App 依然无效。

→ 结论：**「装个证书 + 本地 VPN 解密」这条路，对自家 App 和无 CA 限制的 App 有效，对主流第三方 AI App 无效。**

### 2.2 传输层演进：可见性在持续下降

- **QUIC / HTTP/3**：基于 UDP，传统 TCP 侧解析拿不到；实测中可通过在 VPN 内丢弃 UDP 443 迫使降级到 TCP+TLS（HTTP Toolkit 等工具的思路），但会牺牲性能与部分服务兼容性。
- **ECH（RFC 9849，2025）**：加密 ClientHello 中的 SNI。主流浏览器已默认启用、CDN 大规模部署；**但配置显式代理时浏览器会禁用 ECH**，这对我们的「代理/网关路线」是有利的。原生 App（OkHttp / Conscrypt 系）目前普遍未启用 ECH，SNI 仍可读——这是当下的窗口期，不是长期保障。
- **私有协议**：部分头部 App 使用自研传输层（如腾讯系 MMTLS、字节系 QUIC 实践），标准 TLS 解析对它们无效，只能退化为纯字节统计。

### 2.3 流量统计 API 的能力边界

| 能力 | API | 权限 | 粒度 | 备注 |
|---|---|---|---|---|
| per-UID 累计收发字节 | `TrafficStats.getUidTxBytes/RxBytes` | 无 | 包级总量 | 重启清零；底层读 `/proc/net/xt_qtaguid/stats` |
| 分网络类型（Wi-Fi/移动）历史 | `NetworkStatsManager` | 受限（第三方能力有限） | 较细 | 体验不稳定 |
| 读 `/proc/net/*` | 文件 | Android 9 起仅 **VPN 应用**可见 | 连接级 | 这是 VPN 应用的特权 |
| 全流量 + 域名 + 连接 | `VpnService` | 用户授权一次 | 连接级 | 拿的是 IP 包，需自建解析 |

→ `TrafficStats` 是「零权限、零耗电、粗粒度」的保底方案；`VpnService` 是「有域名、有连接、需常驻」的主力方案。

### 2.4 商店与合规

- Google Play 对 `VpnService` 有专门政策（必须声明、提供隐私政策、数据表单），并禁止利用 VPN 静默收集用户数据；无障碍服务政策更严（必须真是辅助功能用途）。
- 国内渠道需满足个人信息保护法相关要求：明示采集范围、本地优先、用户可关闭、不上传原始内容。
- 因此产品话术应定位为「**我的 AI 用量与花费管理**」，而不是「监控手机上别的 App」。

## 3. 四条路线对比

| 路线 | 精度 | 门槛 | 覆盖面 | 判定 |
|---|---|---|---|---|
| **A. 官方用量 API 聚合**<br>（OpenAI Organization usage / billing、Anthropic Admin API、各家云账单 API） | ⭐⭐⭐⭐⭐ | 需要 API Key / 组织权限 | 仅限有 Key 的账号，覆盖不到 App 内登录态 | **最佳校准源**，做不成主力 |
| **B. 自建网关**（支持自定义 endpoint 的客户端接入 TokenLens 代理） | ⭐⭐⭐⭐⭐ | 客户端要支持改 Base URL | 仅少数第三方客户端 | 现成能力，直接复用 |
| **C. 流量侧观测**（VpnService 统计型 + 域名指纹 + 估算模型） | ⭐⭐⭐（校准后 ±20%~30%） | 无需 root，用户授权 VPN | **所有 App** | **推荐主力** |
| **D. 解密观测**（root + Magisk / Frida / LSPosed hook SSL 或 OkHttp） | ⭐⭐⭐⭐ | root + 对抗 pinning/加固 | 视对抗情况 | 极客可选模块，不作默认 |

推荐组合：**C 为主力，A/B 作为校准与补充，D 作为可选增强**。

## 4. 推荐架构：C 路线的四层

```
[采集层] VpnService (统计型, 不解密)
   │  tun fd → IP/TCP 解析 → 连接级事件
   │  DNS 拦截 / SNI 提取 → 域名
   │  UID → 包名 (PackageManager.getPackagesForUid)
   ▼
[识别层] AI 域名指纹库 + AI 包名库（可远程更新 JSON）
   │  判定：这是哪个厂商 / 哪个模型族 / 是否流式
   ▼
[估算层] per-app 字节→token 系数模型（可校准） + 定价表
   ▼
[展示/同步层] 本地 SQLite → WebView 仪表盘（复用现有单文件 HTML）
               可选同步到桌面端 TokenLens，合并进同一个库
```

### 采集层实现要点

- `VpnService.Builder()`：`addAddress()` + `addRoute("0.0.0.0", 0)`；用 `addAllowedApplication(pkg)` **只引流 AI 类 App**，显著降低耗电与用户不适感。
- 出网 socket 必须 `VpnService.protect()`，否则自己把自己绕死循环。
- 解析策略：只做**轻量解析**（IP 头 + TCP 头 + 首个 TLS ClientHello 的 SNI + 双向字节计数），不做完整用户态 TCP 栈；可参考 Google ToyVpn 示例 / tun2socks 思路，自研一个最小 TCP 中继即可（每个连接一对 socket + 字节计数）。
- 域名获取优先级：本地 DNS 拦截（把 `addDnsServer` 指向自己的解析服务）> TCP 443 的 SNI > 目标 IP 反查。
- 常驻必须有**前台服务 + 常驻通知**，并给出「一键断开」。
- 省电策略：`addAllowedApplication` 限定范围 + 前台才全量统计（`UsageStatsManager` / 前台服务检测）+ 提供「仅 TrafficStats 轮询」的省电模式（零 VPN、零额外耗电，代价是失去域名维度）。

### 事件数据结构（与现有 store 表对齐，便于合并）

```
ts, package, app_label, uid, domain, provider, model_family,
is_stream(推断), tx_bytes, rx_bytes, duration_ms,
est_prompt_tokens, est_completion_tokens, est_cost, confidence(0-1)
```

## 5. 字节→token 估算模型

```
est_tokens ≈ (bytes - protocol_overhead) / bytes_per_token(service, app)
```

误差来源（必须逐项处理，否则误差会飙到 100%+）：

1. **协议开销**：SSE 每个 chunk 的 `data: {"choices":[{"delta":{"content":...}}]}` JSON 包装约 50~80 字节/片，短回答时开销占比极高；
2. **压缩**：gzip/br 后中英文比例差异大（中文约 2~3 字节/字，英文约 4 字符/token）；
3. **上下文累积**：多轮对话上行包含历史消息，上行字节 ≠ 本轮 prompt；
4. **非文本负载**：图片/文件/插件/埋点上报混在同一连接里；
5. **缓存与压缩token**：无法区分。

**校准机制（这是能拉开差距的地方）**：

- 用户手动输入一次「官方账单/网页端显示的真实 token 数」→ 反推该 App 的 `bytes_per_token`；
- 用桌面端 TokenLens 代理跑同一模型同一段对话，拿精确 usage 与手机侧字节数做回归；
- 系数库按 `app + domain` 维度持久化，匿名可选共享（众包系数库）。

**置信度展示**：UI 上明确分级——精确（来自 usage/账单）/ 估算-已校准 / 估算-未校准，不要用一个数字假装精确。

## 6. 与现有 TokenLens 的复用点

| 现有资产 | 移动端复用方式 |
|---|---|
| `pricing.py` 价格表 + 成本计算 | 抽成 JSON，两侧共用同一份价格源 |
| `store.py` 表结构 | Android 端 Room 建同构表，导出 CSV/JSON 直接并入桌面库 |
| `web/index.html` 单文件仪表盘 | **直接用 WebView 加载**，无需重写前端；或手机内起 Ktor/NanoHTTPD 复刻 5 个 `/api/*` 接口 |
| `tokenizer.py` 启发式估算 | 中英加权算法原样移植（约 30 行 Kotlin） |
| 预算/告警逻辑 | 复用阈值与去重策略 |

## 7. 落地路线

- **Phase 0（验证，1~2 天）**：桌面 mock 上游 + 手机浏览器/支持自定义 endpoint 的客户端走 TokenLens 代理，同步用 VpnService PoC 采集同一时段的字节数 → 得出首版误差分布，验证「估算是否站得住」。
- **Phase 1（MVP）**：统计型 VPN + AI 域名/包名指纹库 + 按 App 的用量排行与成本估算 + WebView 仪表盘。
- **Phase 2（校准）**：官方用量 API 导入、手动 ground-truth 校准、系数库持久化、置信度分级。
- **Phase 3（增强）**：省电模式（TrafficStats 轮询）、与桌面端数据合并、可选 root/hook 精确模块。

## 8. 红线

1. 不解密、不存储请求/响应内容；
2. 默认本地处理，任何同步都需用户显式开启且仅传元数据；
3. 只用于**自己的设备**、有明确知情同意；
4. 商店政策与隐私合规优先于功能炫技——宁可少一个功能，也不要下架风险。
