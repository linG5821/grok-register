# 本地 CPA 导出同步产出 grok2api Build 导入文件

**日期:** 2026-07-22
**状态:** 待实现
**范围:** `cpa_export.py` 每号 mint 成功后，除写 `cpa_auths/xai-*.json`，再累加到一个 grok2api Build 导入文件；新增配置 `cpa_build_import_file`。不改 device flow / 浏览器 / mint 逻辑。

## 1. 背景与动机

远程 chenyme `convert-to-build` 在**服务器出口 IP**（多为机房 ASN）用纯 HTTP device flow 换 Build token，容易被 xAI 打 `bot_flag_source`，导致 `cli-chat-proxy.grok.com/v1/*` 全 403。

本地 CPA mint 用**真 Chromium**（走完 email+密码+Turnstile+consent）+ **本机可控代理**换 token，指纹接近真人、出口可为住宅 IP，打标概率显著更低。

因此用户注册时**取消勾选 chenyme 转换**，改为本地 CPA 导出。缺口：CPA 只写自己的 `xai-*.json`，无法直接导入 grok2api。本设计补上「导出时同步产出 grok2api Build 导入文件」。

## 2. 目标

| # | 行为 |
|---|------|
| G1 | `export_cpa_xai_for_account` mint **成功**后，把该号追加进累加导入文件 |
| G2 | 文件格式 `{"accounts": [...]}`，`provider=grok_build`，与 chenyme export/reprobe 导入格式一致 |
| G3 | 单文件累加，同 email 覆盖旧条目（去重） |
| G4 | 原子写 + 损坏备份，避免中断/并发丢数据 |
| G5 | 新增配置 `cpa_build_import_file`，默认项目根 `grok2api_build_import.json` |
| G6 | mint 失败 / CPA 未启用 时不写入 |

非目标：
- 不自动导入到 grok2api（只产文件，用户手动导）。
- 不改 `mint_and_export` 签名；从 `result["path"]` 读回刚写的 CPA JSON 取字段。
- 不动 device flow / 浏览器 / Turnstile 逻辑。

## 3. 数据来源与字段映射

CPA 写出的 `xai-*.json`（`build_cpa_xai_auth` 的 payload）已含所需字段。读回该文件即可拼装：

| grok2api 导入字段 | 来源 |
|---|---|
| `provider` | 常量 `"grok_build"` |
| `name` / `email` | CPA `email` |
| `client_id` | 常量 `b1a00492-073a-47ea-816f-4c329264a828` |
| `access_token` | CPA `access_token` |
| `refresh_token` | CPA `refresh_token` |
| `id_token` | CPA `id_token`（可能缺，留空） |
| `token_type` | `"Bearer"` |
| `scope` | `""` |
| `expires_at` | CPA `expired`（RFC3339；缺则由 `expires_in` 现算） |
| `expires_in` | CPA `expires_in` |
| `user_id` / `principal_id` | CPA `sub` |
| `team_id` | 解 `access_token` JWT 的 `team_id`（缺则 `""`） |

`team_id` 用轻量本地 JWT 解码（base64url payload），失败留空，不引依赖。

## 4. 累加写盘（防丢数据）

```
1. 读 cpa_build_import_file
   - 不存在 → accounts = []
   - 存在但解析失败/结构非法 → 先复制成 <file>.bak，再视为 accounts = []
2. 用 email 建索引，插入或覆盖当前号条目
3. 原子写：mkstemp(同目录) → json.dump → flush → fsync → os.replace
   - chmod 0o600（best-effort）
```

- 原子写复用 `write_cpa_xai_auth` / `save_config` 同款模式（tmp → fsync → replace），中断不损坏目标文件。
- 损坏时**先备份再重建**，绝不静默丢已有数据。
- 单进程串行（注册循环逐号），本设计用**模块级 Lock** 兜住万一的并发调用。

## 5. 模块边界

新增 `cpa_build_import.py`（独立小模块，单一职责）：

| 函数 | 职责 |
|---|---|
| `_decode_jwt_payload(token) -> dict` | base64url 解 JWT payload，失败返回 `{}` |
| `build_import_entry(cpa_payload) -> dict` | CPA payload → grok2api 导入条目 |
| `append_build_import(file_path, entry) -> None` | 读→去重→原子写；损坏先备份 |

`cpa_export.export_cpa_xai_for_account` 在 mint 成功分支调用：读回 `result["path"]` 的 JSON → `build_import_entry` → `append_build_import`。写失败只记日志 + 打 `warning/partial` 标记，**不**影响 CPA 主结果。

## 6. 配置

`DEFAULT_CONFIG` / `config.example.json` 新增：

```json
{ "cpa_build_import_file": "grok2api_build_import.json" }
```

| 配置项 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `cpa_build_import_file` | string | `grok2api_build_import.json` | 累加导入文件路径；相对路径以项目根为基准；空字符串=禁用该产出 |

校验：`_require_string`（path 类），空值合法（表示不产出）。

## 7. 测试

新增 `tests/test_cpa_build_import.py`：

1. `build_import_entry`：字段映射正确；`expires_at` 缺失时由 `expires_in` 现算；team_id 从 JWT 解出。
2. `append_build_import` 新建：文件不存在 → 生成含 1 条的 `{"accounts":[...]}`。
3. `append_build_import` 去重：同 email 覆盖，条目数不增。
4. `append_build_import` 追加：不同 email 累加。
5. 损坏备份：写入非法 JSON 后调用 → 生成 `.bak`，目标重建为合法单条。
6. `cpa_export` 集成：mock mint 成功 + 写出 CPA JSON → 断言导入文件产出对应条目；mint 失败 → 不写。

不依赖真实网络/浏览器。

## 8. 自检记录

- 无 TBD/TODO 占位。
- 与用户确认一致：取消 chenyme 转换、本地 CPA 导出、同步产出单文件累加的 grok2api Build 导入文件、格式同 grok2api 导出。
- 范围：仅新增 `cpa_build_import.py` + `cpa_export` 挂钩 + 配置 + 测试；不改 device flow/浏览器/mint。
- 歧义已消除：累加策略（单文件去重覆盖）、失败不写、损坏先备份。
