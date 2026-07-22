# SSO Device Flow Build 真活复测设计

**日期:** 2026-07-22  
**状态:** 待实现  
**范围:** build_reprobe 新增 SSO→Build 转换路（chenyme sso_build.go 复刻），每号换代理转换、测活，不通时浏览器重抽 SSO 再试。

## 1. 目标

| # | 行为 |
|---|------|
| G1 | 输入：`--emails emails.txt` 或不指定时 chenyme export 全量 |
| G2 | 数据源：`chenyme export` grok_web → 本地 `accounts_*.txt` sso+password 补全 |
| G3 | 每号 Phase A：最多 5 代理 → Device Flow 转 Build → POST `/v1/responses` grok-4.5 测活。首个 live 即停。 |
| G4 | 若有 refresh_token：跑完 Phase A 无活时补官方 OAuth refresh → 再 5 代理测活（保留现有 refresh 路）。 |
| G5 | Phase B：Phase A 中首代理 GET accounts.x.ai 即 302 sign-in 时，调用浏览器用 email+password 重抽 SSO cookie → 重跑 Phase A 5 代理。 |
| G6 | 输出 `reprobe_sso_*.jsonl`，含 status + attempts + bot_flag。 |

## 2. 优先级与分支

```
email
  ↓ (数据源：chenyme 优先)
  ├─ chenyme export grok_web: sso1
  ├─ accounts_*.txt: sso2, password
  └─ 最终 sso = sso1 or sso2 or None

Phase A (max_proxies=5):
  for proxy in proxies:
    GET accounts.x.ai with Cookie
      if 302 sign-in → sso_dead → goto Phase B
      if 非 200 → error, next proxy
    sso_to_build (Device Flow 6步)
      if error → next proxy
    probe /v1/responses
      if live → final_status=live_sso, STOP

Phase A 无活且有 refresh_token:
  official_refresh_access_token
    if success → probe 5 more proxies
      if live → final_status=live_refresh, STOP

Phase B (仅 sso_dead):
  if password not found → final_status=sso_dead_norelogin, STOP
  new_sso = browser_relogin(email, password)
    if failed → final_status=dead_relogin_failed, STOP
  re-run Phase A with new_sso
    if live → final_status=live_relogin, STOP

Still no live → final_status=dead
```

## 3. chenyme sso_build.go 6 步复刻（Python）

Client ID = `b1a00492-073a-47ea-816f-4c329264a828`（与 `cpa_xai/oauth_device.py:CLIENT_ID` 一致，不是 refresh 用的 `xai-grok-cli`）

Scope = `openid profile email offline_access grok-cli:access api:access conversations:read conversations:write`

**步骤**
1. `GET https://accounts.x.ai/` headers: `Cookie: sso=<token>; sso-rw=<token>`  
   - 302 → sign-in → 判定 SSO 已过期（`sso_expired`，permanent）
   - 非 200 → 代理/网络问题（非 permanent）
2. `POST https://auth.x.ai/oauth2/device/code` form: `client_id + scope` → 拿 `device_code`, `user_code`, `verification_uri_complete`, `interval`, `expires_in`
3. `GET <verification_uri_complete>` 同样带 sso cookie
4. `POST https://auth.x.ai/oauth2/device/verify` form: `user_code` → 302 consent 即 OK
5. `POST https://auth.x.ai/oauth2/device/approve` form: `user_code`, `action=allow`, `principal_type=User`, `principal_id=""`
6. Poll `POST https://auth.x.ai/oauth2/token` grant_type=device_code → `access_token / refresh_token / id_token / expires_in`

复用 `cpa_xai/oauth_device.py:_post_form` 与 `poll_device_token` 核心循环（调整日志与超时）。

## 4. 浏览器重抽 SSO

复用 `cpa_xai/browser_confirm.py:create_standalone_page` 开独立 Chromium，不带注册主浏览器上下文。
复用其 email → Turnstile → password → submit 登录逻辑。

```python
def relogin_to_fetch_sso(email, password, proxy=None, cancel=None, log=None):
    """return sso_token or raise BrowserLoginError."""
    page = create_standalone_page(proxy=proxy, timeout=120, log=log)
    page.get("https://accounts.x.ai/login")
    # 填入 email + password
    # 等 Turnstile
    # 点登录
    # 等 accounts.x.ai
    # 抓 cookies 中 sso=
    return sso_value
```

失败分类：`invalid_credentials`, `turnstile_timeout`, `login_blocked`, `no_sso_cookie`, `browser_error`

## 5. 数据源 merge

```python
sso_by_email, password_by_email = {}, {}

# 1. 本地 accounts 文件
for file in sorted(glob.glob("accounts_*.txt")):
    for rec in load_accounts_file(file):
        if not rec.sso:
            continue
        if rec.email not in sso_by_email:
            sso_by_email[rec.email] = rec.sso
        if rec.email not in password_by_email and rec.password:
            password_by_email[rec.email] = rec.password

# 2. chenyme export 覆蓋（优先）
for account in chenyme_export_accounts:
    provider = account.get("provider", "").lower()
    email = account.get("name", "").strip().lower()
    if provider == "grok_web":
        sso_token = account.get("access_token") or account.get("note")
        if sso_token and email:
            sso_by_email[email] = sso_token
    if provider == "grok_build":
        # 保留 refresh_token 走 refresh probe（build_reprobe 已有路）
        refresh_token = account.get("refresh_token")
        access_token = account.get("access_token")
        if email and access_token:
            build_creds[email] = {"access": access_token, "refresh": refresh_token}
```

**测活对象：**
- 有 `grok_web` SSO → 走 Device Flow 新路（转换后测 Build token `/responses`）
- 有 `grok_build` 无 SSO → 走现有 refresh 旧路
- 两路都有 → Device Flow 新路优先（refresh 作为 fallback）

## 6. 输出 JSONL

```json
{
  "ts": "2026-07-22T12:00:00Z",
  "email": "user@example.com",
  "final_status": "live_sso",
  "bot_flag": false,
  "sso_source": "chenyme|accounts_txt|relogin",
  "refreshed": false,
  "proxies_tried": ["socks5://..."],
  "attempts": [
    {"phase": "sso_convert", "proxy": "...", "http_code": 200, "status": "ok", "error": ""},
    {"phase": "probe", "proxy": "...", "http_code": 200, "status": "live", "preview": "hello!"},
    {"phase": "refresh", "proxy": "...", "http_code": 200, "status": "ok"},
    {"phase": "relogin", "proxy": "...", "status": "ok", "error": ""}
  ],
  "live_proxy": "...",
  "client_version": "0.2.103",
  "model": "grok-4.5",
  "error": ""
}
```

`final_status` 全集：
- `live_sso`
- `live_refresh`
- `live_relogin`
- `dead`
- `sso_dead_norelogin`
- `skipped_no_sso`
- `error`

## 7. 组件/文件修改

| 文件 | 变更 |
|------|------|
| `build_sso_convert.py` | NEW: `sso_to_build`, `SSOConvertError`, helpers |
| `cpa_xai/browser_confirm.py` | NEW: `relogin_to_fetch_sso(email, password, proxy, cancel, log)` |
| `build_reprobe.py` | Add Phase enum `"sso_convert"`, `"relogin"`. Add `run_sso_probe_cycle` parallel to `run_account_cycle`. Share attempt/proxy logic. |
| `scripts/reprobe_build_liveness.py` | Add `--accounts-glob`/`--no-accounts-glob`, merge chenyme + local accounts, dispatch SSO vs Build refresh paths, write output. |
| `tests/test_build_sso_convert.py` | NEW: mock 6-step device flow, sso_expired detection, proxy failures. |
| `tests/test_build_reprobe.py` | Add SSO cycle cases: live_sso, sso_dead, relogin success, relogin fail. |
| `tests/test_build_reprobe_e2e.py` | 可选：dry-run 数据源 merge。 |

## 8. 风险

| 风险 | 缓解 |
|------|------|
| Device Flow 6 步每代理一遍，很慢 | timeout=90 限制，每步有 retry 但不无限 |
| 浏览器重抽有 Turnstile 时卡 | 已有 browser_confirm Turnstile 逻辑可复用 |
| chenyme export grok_web 拿不到 sso token | accounts_*.txt 兜底，无则 skipped |
| sso cookie 格式带前辍 `sso=` | normalize_sso 统一去前辍 |
| 代理池全死 | 输出 error; 已有 proxy health 机制 |
