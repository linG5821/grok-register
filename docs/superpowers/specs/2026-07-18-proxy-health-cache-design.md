# 代理健康缓存 + 开页超时换节点设计

**日期:** 2026-07-18  
**状态:** 已实现（核心）  
**范围:** `proxy_manager`、注册浏览器开页、GUI 开始/停止门禁；不改「清 cookie + 新标签」隔离策略。

## 0. 代码联合审查（实现前）

### 0.1 已证实卡死/堵点（现网）

| 点 | 位置 | 影响 | 处理 |
|----|------|------|------|
| `doc_loaded()` 无超时 | `open_signup_page` | 页面挂起可无限等 | G5：timeout=28 |
| 错误页识别窄 | `page_has_proxy_error` | 「无法访问此网站」不触发换节点 | 扩展 `page_has_navigation_failure` |
| 开页内 `restart_browser` 走 **worker 线程** | `open_signup_page` 直接调 | Windows CDP 要求主线程；与 GUI 的 `call_on_ui_thread` **不一致** | 注入 `start/restart_browser_fn`，GUI 传入 `_ui_*` |
| 换节点上限 5 | `_signup_proxy_max_attempts` | 294 池浪费 | 改为缓存驱动 + 安全阀 30 |
| 探活失败只外层 4 次 | `_retry_ui_browser_op` | 未扫池 | health 缓存优先 |

### 0.2 设计闭环补丁

1. **wait 启动竞态**：`wait_until_available` 入口若未在扫且未 full_pass → **先 `start_background_scan`**，避免永远等。  
2. **单 proxy 无 pool**：探 1 次；失败 → `full_pass_done` + `NoAvailableProxyError`（不无限等）。  
3. **G4 耗尽**：`available==0` 时触发一次 refill 扫；若 `full_pass_done and not scanning and available==0` → 停任务。  
4. **NoAvailableProxyError 映射**：flow 内捕获 → `cancelled=True` + 日志，**不**算普通 fail 空转。  
5. **直连**：`proxy_health_should_run()==False` 全路径 no-op（已写 §1.1）。  
6. **开页 restart 必须可注入**：`open_signup_page(..., start_browser_fn=, restart_browser_fn=)`；默认仍调模块内 start/restart（CLI）；GUI 注入 UI 包装。  
7. **安全阀**：单账号开页换节点 ≤ `min(pool_size, 30)`，防 dead 抖动死循环。  
8. **再扫 dead 冷却**：默认 60s 内不重探同一 dead，避免 thrash。
## 1. 问题

1. **探活只试约 4 次就放弃**，294 节点池下极易撞死节点，不会「扫到可用再启浏览器」。
2. **无可用代理运行时缓存**，每次启动/换号随机或 round_robin 硬扫，效率差。
3. **浏览器已起来但页面「无法访问」仍长时间等待**：`open_signup_page` 中 `page.wait.doc_loaded()` 无超时，且错误页识别不全，用户看到 Chrome 错误页却不结束、不换代理。

## 2. 目标

| # | 行为 |
|---|------|
| G1 | 程序启动后 **异步 TCP 探活** `proxy_pool`，可用 URL 进入 **运行时缓存** |
| G2 | 点「开始注册」时若缓存为空：**一直等到 ≥1 个可用**，或用户停止 |
| G3 | **全池探完仍 0 可用** → **禁止启动**注册，日志写明原因 |
| G4 | 注册已启动后若缓存耗尽且再扫全池仍 0 → **停止任务**，日志写明原因 |
| G5 | 打开注册页 **~28s 内**进不了正常页（超时或错误页）→ 标 dead → 换缓存节点 → **restart_browser** 再开 |
| G6 | 探活标准：**TCP host:port** + **经代理访问 Grok 域名**（默认 `https://accounts.x.ai/`；任意 HTTP 响应即通，含 403/CF） |
| G7 | **未配置代理时**：不启探活/缓存/门禁，**直连**走现有注册流程 |

非目标：

- 不用「清 cookie + 新标签」替代每号重启浏览器。
- 不把 HTTP/ipinfo 作为入缓存门槛（可后续增强）。
- 不改变邮箱/远程入池的 `mail_use_proxy` / `remote_import_use_proxy` 语义。

## 1.1 无代理 / 直连（硬门禁）

**判定「已配置代理」**（满足任一即视为已配置）：

- `config.proxy_pool` 非空（至少一条非空 URL），或  
- `config.proxy` 非空字符串  

**未配置时（两条件皆空）：**

| 机制 | 行为 |
|------|------|
| 后台 TCP 探活 | **不启动** |
| 可用代理缓存 | **不创建 / 空操作** |
| 开始注册 `wait_until_available` | **跳过**，不阻塞 |
| 全池无可用 → 拒绝启动 | **不生效** |
| 运行中耗尽停任务 | **不生效** |
| `start_browser` / 开页 | `use_proxy=False` 路径，**直连** Chromium（无 PAC/桥） |
| 开页失败换节点 | **不换代理**（无节点可换）；可按现有逻辑重试/失败本号，**禁止**为了「换代理」而 restart 空转 |

实现上统一入口，例如：

```text
def proxy_health_should_run() -> bool:
    return bool(pool_entries()) or bool(str(config.proxy or "").strip())
```

所有 `start_background_scan` / `wait_until_available` / `acquire_proxy` / `mark_dead` 在 `False` 时立即 no-op 或走直连分支。
## 3. 架构

### 3.1 模块边界（方案 A：落在 `proxy_manager`）

```
proxy_manager
  ├── expand_proxy / rotate_session（现有）
  ├── probe_proxy_endpoint（已有 TCP）
  ├── ProxyHealthCache（新增）
  │     available: deque[str]   # 配置原文 URL
  │     dead: set[str]
  │     probed_count / pool_size
  │     full_pass_done: bool
  │     scanning: bool
  │     lock + 后台 daemon 线程
  └── 对外 API（见 §4）

registration_browser.open_signup_page
  └── 硬超时 + 扩展错误页 + 从缓存换节点重启

grok_register_ttk / registration_flow
  └── 开始门禁 wait_for_available；耗尽 cancel
```

### 3.2 数据流

```
GUI 启动
  → start_background_scan()  # 若 pool 非空
  → 并发 TCP（建议 8–16）
  → 通 → available.append；不通 → dead.add

点开始
  → wait_until_available(cancel)  # 见 §5
  → run_batch → start_browser 使用当前 expand 结果
       （rotate 优先从 available 取）

开注册页失败 / 超时 / 错误页
  → mark_dead(current) + take_next_available() + restart_browser
  → 无可用且 full_pass_done → 抛错 / 停任务

available 低于阈值（默认 5）且未在扫
  → 触发再扫 dead/未探条目（策略 c）
```

## 4. API 草案（`proxy_manager`）

```text
start_background_scan(log=None, concurrency=12) -> None
  # 幂等：已在扫则忽略；无 pool 则 no-op

wait_until_available(cancel_callback=None, log=None, poll=0.5) -> str
  # 阻塞到 available 非空，返回一个可用 URL（不必然 pop，见下）
  # cancel → RegistrationCancelled 同类异常或由调用方映射
  # full_pass_done 且 empty → raise NoAvailableProxyError

acquire_proxy_for_account(reason="") -> str
  # 从 available popleft（或 peek+rotate 语义统一）
  # sticky/random/round_robin 在 available 子集上生效
  # 同步 _current_pool_url / expand / env

mark_proxy_dead(url=None, reason="") -> None
  # url 默认当前；移出 available，加入 dead

available_count() -> int
scan_status() -> dict  # scanning, probed, pool_size, available, dead, full_pass_done

NoAvailableProxyError(Exception)
```

**与 `rotate_session` 的关系：**

- **无 proxy 且无 pool**：health 关闭；`rotate_session` 保持现有空操作/无害行为；注册 **直连**。
- 有 pool 且 health 启用时：`rotate_session` **优先** `acquire` 自 available；available 空且仍在扫时可短等；否则 mark 并尝试 take_next。
- 无 pool、仅单 `proxy`：对该 URL 做 TCP 探活；失败等同无可用（可拒绝启动或等待用户改配置），**不**回退静默直连（用户显式配了代理则必须用代理）。
**再扫策略（已定：c）：**

- 一轮 `full_pass_done` 后线程可休眠。
- 当 `available_count() < 5`（常量或 config `proxy_health_refill_threshold`，默认 5）且存在 dead/未探 → 再启扫描（dead 可重新探，避免节点短暂抖动永久拉黑；可选 dead 冷却时间 60s）。

## 5. 开始注册门禁

```text
if not proxy_health_should_run():
  # 未配置代理：直连，不探活、不等待
  log("[*] 未配置代理，使用直连注册")
  → run_registration / run_batch
else:
  try:
    wait_until_available(cancel=should_stop, log=log)
  except NoAvailableProxyError:
    log("[!] 全池探活无可用代理，拒绝启动注册")
    return  # 不设 is_running 或立即 _set_running_ui(False)
  except Cancelled:
    log("[!] 等待可用代理时用户停止")
    return
  → run_registration / run_batch
```
等待期间日志 **节流**（如每 5s）：`[*] 等待可用代理… 已探 {probed}/{size} 可用 {available}`。

## 6. 开页超时与换节点（G5）

### 6.1 超时

- `page.get(SIGNUP_URL)` / `page.wait.doc_loaded(timeout=28)`（约 25–30s，默认 **28**）。
- 禁止无参数 `doc_loaded()`。
- 可选：整体单次开页墙钟超时与 page_load 对齐。

### 6.2 错误页识别扩展

在现有 `page_has_proxy_error` 基础上增加（大小写不敏感），例如：

- 中文：`无法访问此网站`、`无法访问`、`网页无法打开`、`连接已重置`、`DNS_PROBE`
- 英文/Chrome：`err_connection`、`err_name_not_resolved`、`err_timed_out`、`err_tunnel`、`this site can't be reached`、`took too long to respond`、`dns_probe`

可重命名/拆分为 `page_has_navigation_failure`，注册开页与 NSFW Web 路径共用。

### 6.3 失败处理循环

```text
loop:
  cancel?
  open with timeout
  if success (URL/host 像 accounts.x.ai 且非错误页): break
  mark_dead(current)
  if no available and full_pass_done: raise / stop batch
  if no available and scanning: short wait then continue
  acquire next + restart_browser (GUI 走现有 _ui_restart_browser)
  continue
```

**重试上限：** 不再用固定 5 次作为「死上限」；以 **缓存耗尽 + full_pass_done** 为停。为防死循环，可设安全阀（如单账号开页换节点 ≤ min(pool_size, 30)），超过则本号失败。

### 6.4 成功判定

- `page.url` 含 `accounts.x.ai` 或注册相关 path，且  
- 非 navigation failure，且  
- 可选：能看到邮箱注册入口（现有 `click_email_signup_button` 前的状态）。

## 7. 运行中耗尽

在 `_prepare_next_account` / `open_signup_page` / `start_browser` 选代理失败时：

- 若 `NoAvailableProxyError` → `callbacks` 记日志 → `result.cancelled = True` 或等价停批（**停止已启动的注册**），不得静默直连。

## 8. GUI / 线程

- 后台扫：daemon 线程，**不**占 Tk 主线程。
- `wait_until_available`：在 **worker** 线程调用（与现网 `run_registration` 一致）。
- 停止：`stop_requested` 打断 wait；并保留现有 `_force_stop_browser_best_effort`。
- 可选状态栏：`可用代理: n`（非必须，首版日志足够）。

## 9. 配置（可选，有默认即可）

| 键 | 默认 | 含义 |
|----|------|------|
| `proxy_health_enabled` | `true`（有 pool 时） | 总开关 |
| `proxy_health_concurrency` | `8` | 探活并发（含 HTTP，不宜过大） |
| `proxy_health_tcp_timeout` | `2.0` | TCP 超时秒 |
| `proxy_health_http_timeout` | `6.0` | 经代理访问 Grok 超时秒 |
| `proxy_health_check_url` | `https://accounts.x.ai/` | Grok 可达性探测 URL |
| `proxy_health_refill_threshold` | `5` | available 低于此再扫 |
| `signup_page_load_timeout` | `28` | 开注册页超时秒 |

## 10. 测试

- `probe` / cache：空 pool、全死、部分通、并发入队不重复。
- `wait_until_available`：先空后入队；full_pass_done 空 → 异常；cancel 中断。
- `mark_dead` + acquire 顺序。
- `open_signup_page`：`doc_loaded` 超时 → rotate/restart；错误页文案 → 失败；成功 URL 不误杀。
- 门禁：无可用拒绝启动（mock scan_status）。
- 回归：现有 `test_open_signup_proxy_rotate`、`test_proxy_manager`、`test_browser_lifecycle`。

## 11. 实现顺序（供 plan 拆分）

1. `ProxyHealthCache` + 后台扫 + 单测  
2. `rotate_session` / acquire 接入缓存  
3. GUI 开始门禁 + 耗尽停任务  
4. `open_signup_page` 超时 + 错误页扩展 + 换节点  
5. 联调日志与安全阀  

## 12. 风险与缓解

| 风险 | 缓解 |
|------|------|
| TCP 通但 HTTPS 不通 | 开页 28s 超时 + 错误页补杀；标 dead |
| 294 节点首扫慢 | 异步；有 1 个即可开始，不必等扫完 |
| 主线程 restart 仍慢 | 沿用 max_attempts=1 + 外层重试；开页失败才 restart |
| dead 永久化 | 再扫时重探 dead（阈值触发） |

## 13. 验收标准

- [ ] **未配置 proxy/pool 时**：不启探活线程；点开始立即直连注册，无「等待可用代理」  
- [ ] 启动后（有 pool）日志出现探活进度；available 增加  
- [ ] 缓存空时点开始会等待；停止可打断  
- [ ] 全死拒绝启动，日志含「无可用代理」  
- [ ] 错误页/卡住加载 ≤ ~28s 后换代理重启，不再无限等  
- [ ] 运行中全耗尽会停任务并记原因  
- [ ] 相关单测通过（含「无代理 no-op」用例）  
