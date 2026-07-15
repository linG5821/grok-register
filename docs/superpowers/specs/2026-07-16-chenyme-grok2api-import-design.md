# Chenyme grok2api 自动导入设计

日期: 2026-07-16  
状态: 待用户确认  
方案: A — 并行独立模块（与现有 grok2api 配置/流程分离）

## 背景

本项目（Grok Register）在账号注册成功后，会：

1. 从浏览器 cookie 取得 SSO token
2. 写入 `accounts_*.txt`
3. 调用 `add_token_to_grok2api_pools()`，按需写入**另一套** grok2api 的本地池或远端池（`app_key` + `/tokens/add`）

用户需要额外支持 **chenyme 作者的 grok2api**（管理 API 不同）：

| 步骤 | 接口 |
|------|------|
| 登录 | `POST {base}/api/admin/v1/auth/login` |
| 导入 | `POST {base}/api/admin/v1/accounts/web/import`（multipart 文件，响应 SSE） |
| 转 build | `POST {base}/api/admin/v1/accounts/web/convert-to-build`（JSON，响应 SSE） |

参考笔记：`grok2api.txt`。参考项目：https://github.com/HSJ-BanFan/grok-register-web

## 目标

- 注册成功后，在现有 grok2api 入池逻辑之外，**可选**自动完成 chenyme：登录 → 导入 SSO → convert-to-build
- **独立配置**，可与现有 `grok2api_*` 同时开启或单独开启
- 失败不中断注册主流程
- 尽量少改动现有代码路径

## 非目标

- 不替换、不合并现有 grok2api local/remote 实现
- 不实现 chenyme 管理后台的其它功能（账号列表、删除、统计等）
- 不解析 SSE 业务细节做 UI 进度条（仅消费流并记录成功/失败日志）
- 不在本次改动中引入大范围架构重构（方案 B 的 Exporter 抽象）

## 决策摘要

| 项 | 决定 |
|----|------|
| 架构 | 方案 A：独立 `chenyme_grok2api_*` 配置 + 独立函数 |
| 触发时机 | **每个账号注册成功后立即** import + convert |
| 导入文件行格式 | **纯 SSO token，一行一个**（无 email 包装） |
| 与现有 grok2api | 并行；互不影响；可双开 |
| convert body | `{"all": true, "strategy": "<config>"}`，默认 strategy=`missing` |
| 错误策略 | 捕获异常，打日志，不抛出到注册循环 |

## 配置设计

在 `DEFAULT_CONFIG`、`config.example.json` 中新增（`config.json` 由用户自行填写，不提交密钥）：

```json
{
  "chenyme_grok2api_enabled": false,
  "chenyme_grok2api_base": "",
  "chenyme_grok2api_username": "",
  "chenyme_grok2api_password": "",
  "chenyme_grok2api_convert": true,
  "chenyme_grok2api_convert_strategy": "missing"
}
```

| 配置项 | 类型 | 默认 | 说明 |
|--------|------|------|------|
| `chenyme_grok2api_enabled` | bool | `false` | 是否在注册成功后自动导入 chenyme |
| `chenyme_grok2api_base` | string | `""` | 站点根，如 `http://192.168.8.228:31101`（无尾斜杠） |
| `chenyme_grok2api_username` | string | `""` | admin 用户名 |
| `chenyme_grok2api_password` | string | `""` | admin 密码 |
| `chenyme_grok2api_convert` | bool | `true` | import 成功后是否调用 convert-to-build |
| `chenyme_grok2api_convert_strategy` | string | `"missing"` | convert 的 `strategy` 字段 |

校验规则（启用时）：

- `base`、`username`、`password` 均非空，否则跳过并打 Debug 日志
- `base` 去尾 `/`；若用户误填了 `/api/admin/v1` 后缀，实现中**不强制剥除**，以文档约定「只填根地址」为准（日志可提示）

现有 `grok2api_*` 配置项保持不变。

## 架构与调用点

### 数据流

```
注册成功拿到 sso
  ├─ 写 accounts_*.txt（现有）
  ├─ add_token_to_grok2api_pools(sso, email, log)     # 现有，不变
  └─ add_token_to_chenyme_grok2api(sso, email, log)   # 新增
        if not enabled: return
        ensure accessToken (cache)
        import multipart (single-line sso file)
        if convert: convert-to-build SSE
```

### 调用位置

与现有 `add_token_to_grok2api_pools` 相同两处：

1. GUI 注册循环成功分支（约 `grok_register_ttk.py` 中 `add_token_to_grok2api_pools` 之后）
2. CLI 注册循环成功分支（同上）

两处均在 `add_token_to_grok2api_pools(...)` **之后**追加一行调用，互不依赖成功与否。

### 模块边界（均放在 `grok_register_ttk.py`，与现有 grok2api 函数相邻）

| 函数 | 职责 |
|------|------|
| `_chenyme_normalize_base(base)` | 去空白、去尾 `/` |
| `_chenyme_token_cache`（模块级变量） | 缓存 `accessToken` 与过期时间 |
| `chenyme_login(log_callback=None)` | POST login，解析 `data.tokens.accessToken`，写入缓存 |
| `chenyme_get_access_token(log_callback=None, force_refresh=False)` | 有未过期缓存则复用，否则 login |
| `chenyme_import_sso(raw_token, log_callback=None)` | multipart 上传；401 则 force_refresh 重试一次 |
| `chenyme_convert_to_build(log_callback=None)` | JSON POST + 读 SSE；401 则 force_refresh 重试一次 |
| `add_token_to_chenyme_grok2api(raw_token, email="", log_callback=None)` | 编排；失败只记日志 |

不单独拆文件，以保持与当前单文件主程序风格一致。若后续体积再增，可再拆 `chenyme_grok2api.py`。

## API 细节

### 1. 登录

- **URL**: `{base}/api/admin/v1/auth/login`
- **Method**: POST
- **Headers**: `Content-Type: application/json`
- **Body**:

```json
{"username": "<username>", "password": "<password>"}
```

- **成功响应（关键字段）**:

```json
{
  "data": {
    "tokens": {
      "accessToken": "<jwt>",
      "accessTokenExpiresAt": "2026-07-15T16:10:07Z"
    }
  }
}
```

- **缓存**:
  - 保存 `accessToken` 与 `accessTokenExpiresAt`（解析为 UTC datetime）
  - 若响应无过期时间，默认缓存 **50 分钟**
  - 距过期不足 **60 秒** 时视为过期，重新登录

### 2. 导入

- **URL**: `{base}/api/admin/v1/accounts/web/import`
- **Method**: POST
- **Headers**: `Authorization: Bearer <accessToken>`
- **Body**: `multipart/form-data`
  - 字段名: `files`
  - 文件名: `grok-web-sso-tokens.txt`（固定即可）
  - 文件内容: `_normalize_sso_token(raw_token)` 后的纯文本，**一行一个**，无多余前缀
  - Content-Type: `text/plain`

使用项目已有 `http_post` / `requests`（curl_cffi）发起；multipart 用 `files=` 参数。

- **响应**: SSE 流。实现读取直到连接关闭或超时；HTTP 非 2xx 视为失败。
- **超时**: 60 秒

### 3. Convert to build

- **URL**: `{base}/api/admin/v1/accounts/web/convert-to-build`
- **Method**: POST
- **Headers**: `Authorization: Bearer <accessToken>`, `Content-Type: application/json`
- **Body**:

```json
{"all": true, "strategy": "missing"}
```

`strategy` 取自配置 `chenyme_grok2api_convert_strategy`。

- **响应**: SSE。同样读完流或超时。
- **超时**: 120 秒
- **注意**: 每次账号成功后 `all: true` 会转换「所有」符合 strategy 的账号；与用户选择的「每账号立即 convert」一致。若远端压力大，后续可改为配置「仅批末 convert」，本次不实现。

### 4. 鉴权失败重试

- import / convert 收到 HTTP 401（或响应明确未授权）时：
  1. `force_refresh=True` 重新 login
  2. 用新 token **重试该步一次**
  3. 仍失败则记日志返回

## GUI

在现有 grok2api 配置区域附近增加一行/一块：

- Checkbox: `chenyme 自动导入` → `chenyme_grok2api_enabled`
- Entry: base / username / password（password 可用 `show="*"`）
- Checkbox: `导入后 convert` → `chenyme_grok2api_convert`
- strategy 可放 Entry 或固定默认不在 GUI 暴露（推荐 GUI 只暴露 enabled/base/user/pass/convert，strategy 仅 config 文件）

保存配置时写入 `config.json`（与现有 `save_config` 路径一致）。

CLI 无额外子命令；依赖 `config.json`。

## 错误与日志约定

| 场景 | 日志示例 |
|------|----------|
| 未启用 | 静默 return（不刷屏） |
| 缺 base/账号密码 | `[Debug] chenyme grok2api 未配置 base/账号，跳过` |
| 登录成功 | `[*] chenyme grok2api 登录成功` |
| 导入成功 | `[+] chenyme 已导入 SSO (.../import)` |
| convert 成功 | `[+] chenyme convert-to-build 完成` |
| 任一步失败 | `[Debug] chenyme ... 失败: <exc>` |

`add_token_to_chenyme_grok2api` 内部 try/except，**永不向注册循环抛出**。

## 测试

新增 `tests/test_chenyme_grok2api.py`：

1. **login 解析与缓存**: mock POST 返回 accessToken + expiresAt；二次 `get_access_token` 不重复请求
2. **import multipart**: 断言 URL、Authorization、files 字段内容为纯 sso 一行
3. **401 重登重试**: 第一次 import 401，login 后第二次 200
4. **convert body**: 断言 JSON `all=true` 与 strategy
5. **disabled 跳过**: enabled=false 时不发 HTTP

沿用现有测试风格（mock `http_post` / 模块内 requests），不依赖真实网络。

现有 `tests/test_grok2api_remote_pool.py` 不改。

## 文档

- 更新 `README.md`：配置表增加 chenyme 一节；说明与旧 grok2api 区别
- `config.example.json` 同步新字段

## 实现顺序（供后续 plan 使用）

1. 默认配置 + example + 纯函数（login/import/convert/编排）
2. 单元测试
3. GUI/CLI 调用点接入
4. README

## 风险与后续可选

| 风险 | 缓解 |
|------|------|
| 每账号 `convert all:true` 远端压力 | 配置 `chenyme_grok2api_convert` 可关；后续可加批末模式 |
| SSE 协议细节未完全文档化 | 以 HTTP 状态 + 读完流为准；必要时记录原始片段到 Debug |
| 登录 JWT 时钟偏差 | 提前 60s 刷新 |

## 自检记录

- 无 TBD/TODO 占位
- 与用户确认一致：方案 A、每账号立即 import+convert、纯 sso 行格式、独立配置
- 范围：仅 chenyme 导入链路 + 配置/GUI/测试/文档，不重构现有 grok2api
- 歧义已消除：import 行格式、触发时机、convert body 字段
