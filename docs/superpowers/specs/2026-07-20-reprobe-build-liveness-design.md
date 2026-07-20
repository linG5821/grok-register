# Build 403 号换代理复测 + 官方 Refresh 设计

**日期:** 2026-07-20  
**状态:** 待用户确认后实现  
**范围:** 独立 CLI 脚本；对邮箱列表中的 Build 号做「换代理真活 + 周期内官方 refresh」诊断。不改注册主流程。

## 1. 问题

注册后真活（`build_liveness`）或日常使用会出现 403 / dead。可能原因：

| 原因 | 表现 | 换代理是否有用 | refresh 是否有用 |
|------|------|----------------|------------------|
| 出口差 / 坏节点 | 部分代理 403，住宅通 | 有 | 通常不必 |
| access_token 过期 | 多代理均鉴权失败 | 无 | 有 |
| 账号级 `bot_flag_source` | 多代理 403，refresh 后 JWT 仍带 flag | 无 | **通常无**（验证用） |
| 其它（限流/网络） | 超时/5xx | 可能 | 视情况 |

需要：**一批邮箱 → 系统化区分「代理问题 / token 问题 / 真死」**。

## 2. 目标

| # | 行为 |
|---|------|
| G1 | 输入：邮箱列表文件（每行一个 email） |
| G2 | 从 chenyme **一次全量** `accounts/export`，按 email + `provider=grok_build` **本地匹配**凭据 |
| G3 | 每号测活周期：最多 **N 个代理**（默认 5）→ 官方 OAuth **refresh** → 再 **N 个代理**；**首个 live 即停** |
| G4 | 复用现有 `/v1/responses` CLI 协议与 `build_liveness` 判定（2xx + 非空文本） |
| G5 | 输出 `reprobe_*.jsonl` + 终端摘要；**默认不**写回 chenyme、**不** purge |
| G6 | 独立脚本 `scripts/reprobe_build_liveness.py`，不拖注册 GUI |

非目标：

- 不经 grok2api 公开 chat 轮询指定账号（网关不支持点名）  
- 不自动删除 chenyme 账号  
- 不把本逻辑默认塞进注册成功路径（注册侧仍是单次绑定代理探测）

## 3. 输入 / 凭据匹配

### 3.1 邮箱列表

```text
# emails.txt
user1@example.com
user2@example.com
```

- 空行与 `#` 开头行忽略  
- email 规范化：`strip().lower()`  
- 去重（保留首次出现顺序）

### 3.2 chenyme export（一次）

```text
POST/login → GET {base}/api/admin/v1/accounts/export
```

构建索引：

```text
build_by_email[email] = {
  access_token, refresh_token?, name, raw_account, bot_flag from JWT
}
```

匹配规则：

- `provider == "grok_build"`（或与现网 export 字段一致的 Build 标识）  
- `name`（或等价邮箱字段）小写等于目标 email  

| 情况 | 行为 |
|------|------|
| 列表有、export 无 | `skipped_no_token`，继续下一号 |
| 有 access 无 refresh | 可跑 phase1；phase2 记 `no_refresh_token` 后结束 |
| export 失败 | 整批退出非零码 |

**说明：** grok2api 导出接口为全量；**无**「按 email 单导」时，全量 + 本地匹配是正确方案。进程内 export **只请求一次**。

若 export 条目不含 `refresh_token`：实现时尝试从分页 `/api/admin/v1/accounts` 或 export 其它字段补齐；仍无则 phase2 跳过。

## 4. 单号测活周期（状态机）

```text
creds = index[email]
if missing → skipped_no_token

# Phase 1 — 换代理（最多 max_proxies，默认 5）
for proxy in next_healthy_proxies(max_proxies, exclude_tried):
    result = probe_build_responses(access, proxy=proxy, ...)
    if live → final=live_proxy; stop
    record attempt

# 若 phase1 无 live：
# Phase 1.5 — 官方 refresh（同一测活周期内）
if no_refresh_token → final=dead|error + no_refresh_token; stop
refresh via auth.x.ai (official grant)
if refresh permanent fail → final=dead (refresh_failed); stop
access = new_access_token  # 默认仅内存；--push-chenyme 另议

# Phase 2 — 再换代理（再 max_proxies）
for proxy in next_healthy_proxies(...):
    result = probe(..., access=new)
    if live → final=live_refresh; stop

# 仍无 live
if 多次 403 / bot_flag → final=dead
else → final=error
```

### 4.1 参数

| 参数 | 默认 | 含义 |
|------|------|------|
| `max_proxies` | 5 | 每 phase 最多尝试代理数 |
| `model` | config / `grok-4.5` | 与注册真活一致 |
| `concurrency` | 1 | 默认串行 |
| `refresh` | on | `--no-refresh` 可关 phase2 |

### 4.2 代理选择

优先级：

1. `proxy_manager` 健康缓存 `available`（可启动/等待短扫）  
2. `config.proxy_pool` 顺序跳过已 dead / 本号已试  
3. 可选 `--proxy-file`  

规则：

- 每号维护 `tried_proxies` 集合，不重复  
- 首个 **live** 即停该号  
- 单次超时/连接错误：计一次尝试，换下一代理  
- 同一 access 在 **≥2 个不同代理** 上均为 403 Access denied：倾向账号侧，提前进入 refresh（仍受 max_proxies 上限约束，不无限试）

### 4.3 官方 refresh

与 grok2api Build / 项目 CPA OAuth 对齐：

```text
POST https://auth.x.ai/oauth2/token
Content-Type: application/x-www-form-urlencoded

grant_type=refresh_token
&client_id=<Build CLI client_id>
&refresh_token=<token>
```

- 代理：使用 phase1 最后一个可连通代理，或当前健康池一个节点（避免直连机房与注册环境差过大）  
- 成功：更新内存 access（及若返回则 refresh）  
- 永久失败（invalid_grant 等）：`refresh_failed`  
- **默认不**调用 chenyme 写回；可选二期 `--push-chenyme`

### 4.4 bot_flag

JWT 已有 `bot_flag_source`：

- **仍跑**完整周期（用数据证明 refresh 是否救得了）  
- 结果行带 `bot_flag=true`  
- 若最终仍 403 → `dead`（与项目既有认知一致：flag 后 Build 端点不可用）

## 5. 输出

### 5.1 文件

`reprobe_YYYYMMDD_HHMMSS.jsonl`（cwd 或 `--out-dir`）

每行示例：

```json
{
  "ts": "ISO8601",
  "email": "a@b.com",
  "final_status": "live_proxy|live_refresh|dead|error|skipped_no_token",
  "bot_flag": null,
  "refreshed": false,
  "proxies_tried": ["socks5://…", "…"],
  "attempts": [
    {"phase": 1, "proxy": "…", "http_code": 403, "status": "dead", "error": "…"}
  ],
  "live_proxy": "",
  "client_version": "0.2.103",
  "model": "grok-4.5",
  "error": ""
}
```

### 5.2 终端摘要

```text
total=100 live_proxy=12 live_refresh=3 dead=70 error=10 skipped=5
```

可选打印 `live_proxy` / `live_refresh` 邮箱列表（`--verbose`）。

## 6. CLI

```bash
python scripts/reprobe_build_liveness.py --emails emails.txt
python scripts/reprobe_build_liveness.py --emails emails.txt --max-proxies 5 --dry-run
python scripts/reprobe_build_liveness.py --emails emails.txt --no-refresh --proxy-file extra_proxies.txt
```

| 参数 | 说明 |
|------|------|
| `--emails` | 必填，邮箱文件 |
| `--max-proxies` | 默认 5 |
| `--model` | 默认读 config |
| `--concurrency` | 默认 1 |
| `--dry-run` | 只匹配 export + 列代理，不 POST / 不 refresh |
| `--no-refresh` | 跳过官方 refresh |
| `--proxy-file` | 额外代理 URL 列表 |
| `--out-dir` | 结果目录 |
| `--config` | 默认项目 `config.json` |

配置复用：`chenyme_grok2api_*`、`build_liveness_*`、`proxy_pool` / 健康缓存相关项。

## 7. 组件划分

| 模块 | 职责 |
|------|------|
| `scripts/reprobe_build_liveness.py` | argparse、批循环、摘要、退出码 |
| `build_liveness.py` | 复用 `probe_build_responses`、CLI profile；可加 `probe_with_proxy_rotation(...)` |
| `build_token_refresh.py`（新） | 官方 refresh_token 交换 |
| `proxy_manager.py` | 复用健康池 / `expand`；脚本侧 `next_proxy_candidates` |
| chenyme 辅助 | 登录 + export 一次建索引（可抽自 purge/现有 ttk 逻辑到薄公共函数，或脚本内复制最小实现避免拖 GUI） |

依赖方向：脚本 → 库函数；**不**让 `grok_register_ttk` 反向依赖脚本。

## 8. 退出码

| 码 | 含义 |
|----|------|
| 0 | 跑完；允许部分 dead |
| 2 | 配置/登录/export 失败 |
| 1 | 跑完但存在未处理异常（可选） |

## 9. 测试

1. 邮箱解析：注释、去重、大小写  
2. export 索引匹配 / 缺失号 skipped  
3. phase1 第 2 个代理 live → 不 refresh、attempts 长度 2  
4. phase1 全 403 → mock refresh 成功 → phase2 live → `live_refresh`  
5. refresh permanent fail → `dead` + `refresh_failed`  
6. dry-run 零 HTTP 上游调用  

## 10. 风险

| 风险 | 缓解 |
|------|------|
| 全量 export 大/慢 | 一次拉取；进度日志 |
| 无 refresh 字段 | phase2 明确 skipped |
| 代理池 IDC | 日志警告；复活率解释 |
| refresh 写回误伤号池 | 默认不写 chenyme |
| 与注册并发抢代理 | 建议单独跑；concurrency=1 |

## 11. 实现顺序

1. `build_token_refresh.py` + 单测  
2. export 索引 + 邮箱加载  
3. 单号状态机（5 + refresh + 5）  
4. CLI 脚本 + jsonl  
5. dry-run / 集成 mock 测试  

## 12. 与现有功能关系

```text
注册成功 → build_liveness（当前绑定代理测一次）→ liveness_*.jsonl
                ↓ 人工筛 403 邮箱
scripts/reprobe_build_liveness.py（本设计）→ reprobe_*.jsonl
                ↓ 确认真死
scripts/purge_bot_accounts.py（可选人工）
```
