# Build 真活检测（/responses 发 hi）设计

**日期:** 2026-07-19  
**状态:** 已批准，待实现  
**范围:** chenyme convert-to-build 之后，用 Build access_token 经注册代理直连 `cli-chat-proxy` `/responses`；不改主账号落盘、不 purge。

## 1. 问题

注册成功后现有链路为：

```text
SSO 导入 chenyme → convert-to-build → 解 JWT 查 bot_flag_source（仅日志）
```

`bot_flag_source` 只能发现「签发时已被打标」的号。以下情况仍会漏：

- JWT 干净但推理端 403 / 空响应  
- 出口/指纹导致 Build 不可用  
- token 过期或 convert 未真正落到可用 Build 凭据  

需要一次**真实推理调用**（发 `hi`）做活号检测。

## 2. 目标

| # | 行为 |
|---|------|
| G1 | convert-to-build 成功后，用该号 **Build access_token** 调一次 `/v1/responses` |
| G2 | 请求协议对齐 [chenyme/grok2api](https://github.com/chenyme/grok2api) Build CLI adapter（含伪装头） |
| G3 | HTTP 出口使用**该账号注册时同一代理**；无代理则直连 |
| G4 | 成功 = HTTP 2xx **且** 响应中可解析到非空模型文本 |
| G5 | 结果写日志 + 独立 `liveness_*.jsonl`；**不**改 `accounts_*.txt`、**不**删 chenyme 账号 |
| G6 | CLI 版本/UA 优先从本机 chenyme `GET /api/admin/v1/settings` 的 `recommendedProviderBuild` 拉取，失败用本地默认 |

非目标：

- 不经 chenyme 公开 OpenAI 兼容接口转发（避免测到轮询/别号）  
- 不自动 purge（继续用 `scripts/purge_bot_accounts.py` 人工处理）  
- 不做批量历史号补测 UI（可后续独立脚本）  
- 不把失败记成注册失败（账号已注册成功）

## 3. 协议（对齐 grok2api）

### 3.1 上游端点

- **URL:** `{base}/responses`  
- **默认 base:** `https://cli-chat-proxy.grok.com/v1`  
- **Method:** `POST`  
- **Body（最小）：**

```json
{
  "model": "grok-4.5",
  "input": "hi",
  "stream": false
}
```

### 3.2 请求头（grok2api `cli.Adapter.applyHeaders` + config 默认）

| Header | 默认 / 来源 |
|--------|-------------|
| `Authorization` | `Bearer <build access_token>` |
| `X-XAI-Token-Auth` | `xai-grok-cli`（可从 settings `providerBuild.tokenAuth` 覆盖；空则默认） |
| `x-grok-client-version` | 见 §4 CLI 参数解析 |
| `x-grok-client-identifier` | `grok-shell`（settings `providerBuild.clientIdentifier` 优先） |
| `x-grok-client-mode` | `headless`（固定） |
| `User-Agent` | 见 §4 |
| `Content-Type` | `application/json` |
| `Accept` | `application/json` |
| `x-authenticateresponse` | `authenticate-response` |
| `x-grok-agent-id` | 进程内固定 UUID（一次启动生成） |
| `x-grok-session-id` | 每次探测新 UUID |
| `x-grok-conv-id` | 与 session-id 相同 |
| `x-grok-req-id` | 每次新 UUID |
| `x-grok-model-override` | 与 body `model` 相同 |
| `traceparent` | `00-<32hex>-<16hex>-01`（每次新） |
| `x-grok-user-id` | 若 JWT/`export` 有 user id 则带；没有则省略 |

**不**使用浏览器 TLS 指纹（grok2api 注释：Build 走标准 Go HTTP/TLS；Web 才 impersonate）。Python 侧用现有 `curl_cffi` / `requests` 即可，经代理出站。

### 3.3 grok2api「同步最新」真实含义

管理后台「同步推荐版本」**不是**向 xAI 拉实时 CLI 版本，而是：

1. 编译常量（`backend/internal/infra/config/config.go`）：  
   - `RecommendedBuildClientVersion = "0.2.103"`  
   - `RecommendedBuildUserAgent = "grok-shell/0.2.103 (linux; x86_64)"`
2. `GET /api/admin/v1/settings` 返回：  
   `recommendedProviderBuild: { clientVersion, userAgent }`
3. 前端按钮把这两项写入表单；`clientIdentifier` / `tokenAuth` 来自当前 `providerBuild` 配置。

本项目不解析 grok2api 源码常量，而是**调用用户已部署的 chenyme 实例**取同一快照。

## 4. CLI 参数解析顺序

```text
resolve_build_cli_profile():
  1. 若 build_liveness_fetch_cli_from_chenyme 且 chenyme 已配置:
       GET {chenyme_base}/api/admin/v1/settings
       （Bearer = 现有 chenyme admin accessToken）
       读取:
         recommendedProviderBuild.clientVersion / userAgent
         providerBuild.clientIdentifier / tokenAuth / baseURL / userAgent / clientVersion
  2. 合并规则（每字段独立）:
       clientVersion = recommended.clientVersion
                     or providerBuild.clientVersion
                     or config.build_liveness_client_version
                     or "0.2.103"
       userAgent     = recommended.userAgent
                     or providerBuild.userAgent
                     or 由 version 合成 "grok-shell/{ver} (linux; x86_64)"
       clientIdentifier = providerBuild.clientIdentifier
                        or config or "grok-shell"
       tokenAuth     = providerBuild.tokenAuth（非空）
                     or config or "xai-grok-cli"
       baseURL       = config.build_liveness_base_url
                     or providerBuild.baseURL
                     or "https://cli-chat-proxy.grok.com/v1"
  3. 进程内缓存 profile（TTL 默认 1h，或 settings revision 变化时刷新）
  4. 拉取失败 → 打 [Debug] 日志，用步骤 2 的本地默认，不阻断探测
```

`model` / `prompt` **不**从 chenyme settings 取，只用本项目 config（默认 `grok-4.5` / `hi`）。

## 5. 触发时机

在 `add_token_to_chenyme_grok2api` 内，顺序：

```text
chenyme_import_sso
  → chenyme_convert_to_build（若 convert 开启）
  → chenyme_check_bot_flag
  → chenyme_probe_build_liveness   # 新增
```

门禁：

| 条件 | 行为 |
|------|------|
| `build_liveness_enabled == false` | 跳过 |
| `chenyme_grok2api_enabled == false` | 跳过（无 Build token 来源） |
| convert 关闭且池中无该 email 的 Build 号 | 跳过并 debug 日志 |
| 找不到 Build access_token | 写 liveness 行 `error=no_build_token`，不抛 |

探测失败**不**让 `add_token_to_chenyme_grok2api` 返回 False（入池仍算成功）；仅日志 + 文件。

可选：`ok=false` 时 `postprocess_warning_count += 1`（通过 flow 回调或返回结构扩展；实现时优先不破坏现有 `bool` 返回，可在 GUI observer 侧根据日志/文件统计，或让 probe 返回 dict 由 flow 累计警告）。

**推荐实现：** `chenyme_probe_build_liveness` 返回 `{"ok": bool, "skipped": bool, ...}`；`add_token_to_chenyme_grok2api` 仍返回 bool 表示导入；警告由 probe 内部写文件，flow 若需计数则后续小改 `RegistrationOperations`（非本版硬要求）。本版以**日志 + jsonl** 为准。

## 6. 代理绑定

### 6.1 问题

后处理阶段可能已 `rotate` 到下一节点；探测必须用**注册该号时**的出口。

### 6.2 方案

在账号开始使用代理时记录：

```text
# proxy_manager 或 registration 侧
_account_proxy_by_email[email] = current_proxy_url
# 或 thread-local / 批次上下文: last_bound_proxy
```

探测调用：

```text
proxy = proxy_for_account(email) or get_current_proxy() or config.proxy
http_post(url, ..., proxies=proxy_dict(proxy), timeout=...)
```

无代理配置：与现有 `force_direct` / 直连路径一致，不强制代理。

`remote_import_use_proxy` 不作用于本请求；本请求**显式**使用注册代理（与「测该出口下 Build 是否可用」一致）。

## 7. 成功 / 失败判定

| 结果 | 条件 | 日志 |
|------|------|------|
| `live` | HTTP 2xx 且抽出非空文本 | `[+] Build 真活 {email} model=… preview=…` |
| `dead` | HTTP 403，或 body 含 Access denied，或 JWT 已有 bot_flag 且 403 | `[!] Build 不可用 {email} http=403 …` |
| `error` | 超时、5xx、代理错误、解析失败、无 token、非 2xx 其它码 | `[!] Build 真活异常 {email}: …` |
| `skipped` | 功能关闭 / chenyme 未启用 | debug 或不记文件 |

文本抽取（按序尝试，取第一段非空 strip）：

1. `output_text`（string）  
2. `output[]` 中 `type==message` 的 `content[]` 里 `output_text` / `text`  
3. 任意嵌套 string 字段启发式（长度 1–500 预览截断）

## 8. 结果文件

- 路径：与当前 batch `accounts_*.txt` 同目录；文件名 `liveness_{batch_ts}.jsonl`  
  - batch_ts 优先复用账号输出文件时间戳段；否则 `YYYYMMDD_HHMMSS` 在 batch 开始时生成一次  
- 追加写入（每号一行 JSON），字段：

```json
{
  "ts": "ISO8601",
  "email": "a@b.com",
  "ok": true,
  "status": "live|dead|error|skipped",
  "http_code": 200,
  "proxy": "socks5://…",
  "model": "grok-4.5",
  "preview": "Hello! …",
  "error": "",
  "bot_flag": null,
  "client_version": "0.2.103",
  "base_url": "https://cli-chat-proxy.grok.com/v1"
}
```

主结果文件与 pending 逻辑**不变**。

## 9. 配置

| Key | 默认 | 说明 |
|-----|------|------|
| `build_liveness_enabled` | `true` | 总开关 |
| `build_liveness_model` | `"grok-4.5"` | 探测模型 |
| `build_liveness_prompt` | `"hi"` | 探测输入 |
| `build_liveness_timeout_sec` | `60` | HTTP 超时 |
| `build_liveness_base_url` | `""` | 空则走 §4 解析 |
| `build_liveness_client_version` | `""` | 空则走 §4 |
| `build_liveness_user_agent` | `""` | 空则走 §4 |
| `build_liveness_client_identifier` | `""` | 空则 `grok-shell` |
| `build_liveness_token_auth` | `""` | 空则 `xai-grok-cli` |
| `build_liveness_fetch_cli_from_chenyme` | `true` | 是否拉 settings |
| `build_liveness_cli_cache_ttl_sec` | `3600` | profile 缓存 |

写入 `app_config.py` 默认值与 `config.example.json`；`config.json` 不强制用户手改。

## 10. 组件划分

| 模块 | 职责 |
|------|------|
| `build_liveness.py`（新） | profile 解析/缓存、组 headers、POST `/responses`、解析文本、append jsonl |
| `grok_register_ttk.py` | `chenyme_fetch_build_cli_profile`（或放入 build_liveness）、在 convert 后调用 probe；传入 email、token、proxy、output path |
| `proxy_manager.py` | `bind_proxy_for_account(email)` / `get_proxy_for_account(email)`（或等价最小 API） |
| `app_config.py` / `config.example.json` | 新配置项 |
| `tests/test_build_liveness.py` | mock HTTP：live / 403 dead / timeout / 无 token；profile 合并优先级 |

可选小改：`chenyme_check_bot_flag` 与 probe 共用「按 email 取 Build 账号」helper，避免双次全量 export（可一次 export 复用 token + bot_flag）。

## 11. 数据流

```text
注册成功 → 保存 accounts 行
         → add_chenyme_tokens(sso, email)
              import → convert → bot_flag
              → resolve_cli_profile (chenyme settings cache)
              → export/find build access_token by email
              → POST /responses via account proxy
              → log + append liveness jsonl
         → export_cpa（不变）
```

## 12. 错误处理

- 探测异常全部吞掉并记 `error` 行，**不**影响注册成功统计  
- chenyme settings 401：清 token 缓存重登一次，再失败用默认 profile  
- 取消 batch：`cancel_callback` 为真则跳过后续探测  

## 13. 测试计划

1. **单元：** headers 含 Token-Auth / client-version / headless；body model+input  
2. **单元：** 2xx + output_text → live；403 → dead；timeout → error  
3. **单元：** profile 合并：recommended > providerBuild > config > hardcoded  
4. **单元：** 无 proxy 时 proxies=None；有 bind 时用 bind 而非当前 rotate  
5. **回归：** 现有 `test_chenyme_grok2api` mock probe 为 no-op 或成功  

## 14. 风险

| 风险 | 缓解 |
|------|------|
| CLI 版本升级 | 从 chenyme recommended 拉取；config 可覆盖 |
| `grok-4.5` 账号目录暂无 | 记 error；用户改 `build_liveness_model` |
| 全量 export 慢 | 与 bot_flag 共用一次 export；后续可改 accounts 分页按 name 查 |
| 真活仍依赖住宅出口 | 本功能只检测，不治本 |

## 15. 实现顺序（供 writing-plans）

1. config 默认值 + example  
2. `build_liveness.py` 核心（headers、POST、parse、jsonl）+ 单测  
3. chenyme settings profile 拉取 + 缓存  
4. proxy 账号绑定 API  
5. 接入 `add_token_to_chenyme_grok2api`  
6. 共享 export helper（可选）  
7. 跑相关单测 / lint  
