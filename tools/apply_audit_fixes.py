#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Apply targeted audit fixes without rewriting the project structure.

This script is intentionally idempotent because the repository may already
contain some of the fixes from earlier interrupted bot runs.
"""

from pathlib import Path
import ast
import re

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "grok_register_ttk.py"
REQ = ROOT / "requirements.txt"
GITIGNORE = ROOT / ".gitignore"
README = ROOT / "README.md"
OAUTH = ROOT / "cpa_xai" / "oauth_device.py"
BROWSER = ROOT / "cpa_xai" / "browser_confirm.py"
SCHEMA = ROOT / "cpa_xai" / "schema.py"
WRITER = ROOT / "cpa_xai" / "writer.py"


def read(path, encoding="utf-8"):
    return path.read_text(encoding=encoding)


def write(path, content, encoding="utf-8"):
    path.write_text(content, encoding=encoding)


def replace_required(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, got {count}")
    return text.replace(old, new, 1)


def replace_if_present(text, old, new):
    return text.replace(old, new, 1) if old in text else text


def replace_func_between(text, start_marker, end_marker, new_block, label):
    start = text.find(start_marker)
    if start < 0:
        if new_block in text:
            return text
        raise RuntimeError(f"{label}: start marker not found")
    end = text.find(end_marker, start)
    if end < 0:
        raise RuntimeError(f"{label}: end marker not found")
    return text[:start] + new_block + text[end:]


def ensure_line(content, line):
    lines = content.splitlines()
    if line not in lines:
        content = content.rstrip() + "\n" + line + "\n"
    return content


# Trigger token: audit-fixes-v4
# ---------------------------------------------------------------------------
# Main application fixes.
# ---------------------------------------------------------------------------
app = read(APP, encoding="utf-8-sig")

# L-01 / M-05: UTF-8 without BOM and CLI can start without Tkinter installed.
app = replace_if_present(
    app,
    "import tkinter as tk\nfrom tkinter import ttk, messagebox, scrolledtext\n",
    "try:\n    import tkinter as tk\n    from tkinter import ttk, messagebox, scrolledtext\n    TK_AVAILABLE = True\n    TK_IMPORT_ERROR = None\nexcept ImportError as exc:\n    tk = None\n    ttk = None\n    messagebox = None\n    scrolledtext = None\n    TK_AVAILABLE = False\n    TK_IMPORT_ERROR = exc\n",
)
app = app.replace(
    'def tk_button(parent, text="", command=None, state=tk.NORMAL, **kwargs):',
    'def tk_button(parent, text="", command=None, state="normal", **kwargs):',
)

# M-02: safer defaults, matching config.example.json.
app = app.replace('    "proxy": "http://127.0.0.1:7890",', '    "proxy": "",')
app = app.replace('    "grok2api_auto_add_local": True,', '    "grok2api_auto_add_local": False,')
old_load_config = '''def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            config = {**DEFAULT_CONFIG, **loaded}
        except Exception:
            config = DEFAULT_CONFIG.copy()
    return config
'''
new_load_config = '''def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                raise ValueError("config root must be a JSON object")
            config = {**DEFAULT_CONFIG, **loaded}
        except Exception as exc:
            message = f"配置文件解析失败: {CONFIG_FILE}: {exc}"
            print(f"[!] {message}", file=sys.stderr)
            raise SystemExit(message)
    else:
        config = DEFAULT_CONFIG.copy()
    return config
'''
app = replace_if_present(app, old_load_config, new_load_config)

# H-05 / M-01: local token writes are locked, backed up and atomic.
new_local_pool = '''def add_token_to_grok2api_local_pool(raw_token, email="", log_callback=None):
    token = _normalize_sso_token(raw_token)
    if not token:
        return False
    token_file = os.path.abspath(resolve_grok2api_local_token_file())
    pool_name = str(config.get("grok2api_pool_name", "ssoBasic") or "ssoBasic").strip() or "ssoBasic"
    parent = os.path.dirname(token_file)
    os.makedirs(parent, exist_ok=True)
    lock_path = token_file + ".lock"
    try:
        from filelock import FileLock
    except Exception as exc:
        raise RuntimeError(f"filelock 依赖不可用，拒绝非原子写入 token 池: {exc}")
    with FileLock(lock_path, timeout=30):
        data = {}
        if os.path.exists(token_file):
            try:
                with open(token_file, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
            except Exception as exc:
                broken_path = token_file + f".broken-{int(time.time())}"
                try:
                    os.replace(token_file, broken_path)
                except Exception:
                    broken_path = token_file
                raise RuntimeError(f"本地 token 文件 JSON 解析失败，已停止写入以避免覆盖: {broken_path}: {exc}")
        if not isinstance(data, dict):
            raise RuntimeError("本地 token 文件根节点不是 JSON object，拒绝覆盖")
        pool = data.get(pool_name)
        if not isinstance(pool, list):
            pool = []
        existing = set()
        for item in pool:
            if isinstance(item, str):
                existing.add(_normalize_sso_token(item))
            elif isinstance(item, dict):
                existing.add(_normalize_sso_token(item.get("token", "")))
        if token in existing:
            if log_callback:
                log_callback(f"[*] grok2api 本地池已存在 token: {pool_name}")
            return True
        pool.append({"token": token, "tags": ["auto-register"], "note": email})
        data[pool_name] = pool
        if os.path.exists(token_file):
            backup_path = token_file + ".bak"
            try:
                with open(token_file, "rb") as src, open(backup_path, "wb") as dst:
                    dst.write(src.read())
                    dst.flush()
                    os.fsync(dst.fileno())
            except Exception as exc:
                raise RuntimeError(f"创建本地 token 备份失败，拒绝继续写入: {exc}")
        temp_path = token_file + ".tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, token_file)
        finally:
            if os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass
    if log_callback:
        log_callback(f"[+] 已写入 grok2api 本地池: {pool_name} ({token_file})")
    return True


'''
if "with FileLock(lock_path" not in app:
    app = replace_func_between(
        app,
        "def add_token_to_grok2api_local_pool(raw_token, email=\"\", log_callback=None):\n",
        "def get_grok2api_remote_api_bases(base):\n",
        new_local_pool,
        "atomic local token pool",
    )

# H-01: remote fallback must not POST a full replacement unless old state was read.
old_remote_fallback = '''    # 兜底：旧版全量保存接口
    current = {}
    fallback_base = api_bases[0] if api_bases else base
    for api_base in api_bases or [base]:
        try:
            resp = http_get(f"{api_base}/tokens", headers=headers, params=query, timeout=20)
            if resp.status_code == 200:
                payload = resp.json()
                current = payload.get("tokens", {}) if isinstance(payload, dict) else {}
                fallback_base = api_base
                break
        except Exception:
            continue
    if not isinstance(current, dict):
        current = {}
'''
new_remote_fallback = '''    # 兜底：旧版全量保存接口。必须先成功读取远端旧状态，避免空池覆盖。
    current = {}
    fallback_base = api_bases[0] if api_bases else base
    loaded_remote_state = False
    load_errors = []
    for api_base in api_bases or [base]:
        try:
            resp = http_get(f"{api_base}/tokens", headers=headers, params=query, timeout=20)
            if resp.status_code == 200:
                payload = resp.json()
                if isinstance(payload, dict):
                    candidate = payload.get("tokens") if "tokens" in payload else payload
                    if isinstance(candidate, dict):
                        current = candidate
                        fallback_base = api_base
                        loaded_remote_state = True
                        break
                load_errors.append(f"{api_base}/tokens: unexpected payload")
            else:
                load_errors.append(f"{api_base}/tokens: HTTP {resp.status_code}")
        except Exception as exc:
            load_errors.append(f"{api_base}/tokens: {exc}")
    if not loaded_remote_state:
        raise RuntimeError("无法安全读取远端 token 池，拒绝执行全量覆盖: " + "; ".join(load_errors))
'''
app = replace_if_present(app, old_remote_fallback, new_remote_fallback)

# M-06: do not hide unexpected SSO wait exceptions forever.
if "last_wait_exception_message" not in app:
    app = replace_if_present(
        app,
        '''    final_no_submit_state = ""
    final_no_submit_since = None
    final_no_submit_timeout = 25

    while time.time() < deadline:
''',
        '''    final_no_submit_state = ""
    final_no_submit_since = None
    final_no_submit_timeout = 25
    last_wait_exception_message = ""
    last_wait_exception_at = 0.0

    while time.time() < deadline:
''',
    )
    app = replace_if_present(
        app,
        '''        except Exception:
            pass

        sleep_with_cancel(1, cancel_callback)
''',
        '''        except Exception as exc:
            if log_callback:
                now = time.time()
                message = f"{exc.__class__.__name__}: {exc}"
                if message != last_wait_exception_message or now - last_wait_exception_at >= 10:
                    log_callback(f"[Debug] 等待 sso cookie 时出现异常，将继续等待: {message}")
                    last_wait_exception_message = message
                    last_wait_exception_at = now

        sleep_with_cancel(1, cancel_callback)
''',
    )

# L-02: repair known mojibake that affects logs/control flow.
for bad, good in {
    "鐢ㄦ埛鍋滄娉ㄥ唽": "用户停止注册",
    "YYDS 鍒涘缓閭澶辫触": "YYDS 创建邮箱失败",
    "YYDS 鑾峰彇JWT澶辫触": "YYDS 获取 JWT 失败",
}.items():
    app = app.replace(bad, good)

# M-05: GUI-only Tk failure should not break CLI mode.
old_main = '''def main():
    if len(sys.argv) > 1 and sys.argv[1].strip().lower() in ("start", "cli", "--cli"):
        main_cli()
        return
    root = tk.Tk()
    setup_light_theme(root)
    app = GrokRegisterGUI(root)
    root.mainloop()
'''
new_main = '''def main():
    if len(sys.argv) > 1 and sys.argv[1].strip().lower() in ("start", "cli", "--cli"):
        main_cli()
        return
    if not TK_AVAILABLE:
        print(f"[!] GUI 模式需要 Tkinter，但当前环境不可用: {TK_IMPORT_ERROR}", file=sys.stderr)
        print("[*] 可改用 CLI 模式: python grok_register_ttk.py cli", file=sys.stderr)
        return
    root = tk.Tk()
    setup_light_theme(root)
    app = GrokRegisterGUI(root)
    root.mainloop()
'''
app = replace_if_present(app, old_main, new_main)

ast.parse(app)
write(APP, app, encoding="utf-8")

# ---------------------------------------------------------------------------
# CPA browser / OAuth fixes.
# ---------------------------------------------------------------------------
browser = read(BROWSER)
browser = browser.replace("except BaseException as exc:", "except Exception as exc:")
old_proxy_block = '''    from .proxyutil import proxy_for_chromium, proxy_log_label, resolve_proxy

    resolved = resolve_proxy(proxy)
    chrome_proxy = proxy_for_chromium(resolved)
    if chrome_proxy:
        options.set_argument("--proxy-server=%s" % chrome_proxy)
        logger("browser proxy=%s (chromium %s)" % (proxy_log_label(resolved), chrome_proxy))
    else:
        logger("browser proxy=(none)")

    browser = Chromium(options)
    page = browser.latest_tab
    logger("standalone chromium started")
    return browser, page
'''
new_proxy_block = '''    from .proxyutil import prepare_chromium_proxy, proxy_log_label, resolve_proxy

    resolved = resolve_proxy(proxy)
    proxy_bridge = None
    chrome_proxy, proxy_bridge = prepare_chromium_proxy(resolved, log=logger)
    if chrome_proxy:
        options.set_argument("--proxy-server=%s" % chrome_proxy)
        logger("browser proxy=%s (chromium %s)" % (proxy_log_label(resolved), chrome_proxy))
    else:
        logger("browser proxy=(none)")

    browser = Chromium(options)
    if proxy_bridge is not None:
        try:
            setattr(browser, "_cpa_proxy_bridge", proxy_bridge)
        except Exception:
            pass
    _register_mint_browser(browser)
    page = browser.latest_tab
    logger("standalone chromium started")
    return browser, page
'''
browser = replace_if_present(browser, old_proxy_block, new_proxy_block)
old_close_block = '''def close_standalone(browser: Any) -> None:
    try:
        browser.quit()
    except Exception:
        pass


_mint_tls = threading.local()
'''
new_close_block = '''def close_standalone(browser: Any) -> None:
    if browser is None:
        return
    _unregister_mint_browser(browser)
    bridge = getattr(browser, "_cpa_proxy_bridge", None)
    try:
        browser.quit()
    except Exception:
        pass
    if bridge is not None:
        try:
            bridge.stop()
        except Exception:
            pass


_mint_tls = threading.local()
_mint_registry_lock = threading.Lock()
_mint_registry = set()


def _register_mint_browser(browser: Any) -> None:
    if browser is None:
        return
    with _mint_registry_lock:
        _mint_registry.add(browser)


def _unregister_mint_browser(browser: Any) -> None:
    if browser is None:
        return
    with _mint_registry_lock:
        _mint_registry.discard(browser)
'''
if "_mint_registry = set()" not in browser:
    browser = replace_required(browser, old_close_block, new_close_block, "CPA global browser registry")
old_shutdown = '''def shutdown_mint_browsers() -> None:
    state = _mint_tls_get()
    browser = state.get("browser")
    if browser is not None:
        try:
            close_standalone(browser)
        except Exception:
            pass
    state.update({"browser": None, "page": None, "served": 0, "proxy": None, "headless": None})
'''
new_shutdown = '''def shutdown_mint_browsers() -> None:
    state = _mint_tls_get()
    with _mint_registry_lock:
        browsers = list(_mint_registry)
    for browser in browsers:
        try:
            close_standalone(browser)
        except Exception:
            pass
    state.update({"browser": None, "page": None, "served": 0, "proxy": None, "headless": None})
'''
browser = replace_if_present(browser, old_shutdown, new_shutdown)
old_poll = '''        def _poll() -> None:
            try:
                time.sleep(2)
                result = poll_device_token(
                    session.device_code,
                    token_endpoint=session.token_endpoint,
                    interval=max(session.interval, 5),
                    expires_in=min(session.expires_in, int(browser_timeout_sec) + 60),
                    log=logger,
                    cancel=cancel,
                    proxy=resolved or None,
                )
                token_box["token"] = result
                stop_event.set()
                logger("token poll SUCCESS — stop_event set")
            except Exception as exc:
                error_box["err"] = exc
                stop_event.set()
'''
new_poll = '''        def combined_cancel():
            return stop_event.is_set() or bool(cancel and cancel())

        def _poll() -> None:
            try:
                for _ in range(20):
                    if combined_cancel():
                        raise OAuthDeviceError("cancelled")
                    time.sleep(0.1)
                result = poll_device_token(
                    session.device_code,
                    token_endpoint=session.token_endpoint,
                    interval=max(session.interval, 5),
                    expires_in=min(session.expires_in, int(browser_timeout_sec) + 60),
                    log=logger,
                    cancel=combined_cancel,
                    proxy=resolved or None,
                )
                token_box["token"] = result
                stop_event.set()
                logger("token poll SUCCESS — stop_event set")
            except Exception as exc:
                error_box["err"] = exc
                stop_event.set()
'''
browser = replace_if_present(browser, old_poll, new_poll)
old_join = '''            if hard:
                stop_event.set()
                raise
        thread.join(timeout=max(browser_timeout_sec, 60) + 30)
'''
new_join = '''            if hard:
                stop_event.set()
                thread.join(timeout=5)
                if thread.is_alive():
                    logger("token poll thread did not stop within 5s after browser failure")
                raise
        thread.join(timeout=max(browser_timeout_sec, 60) + 30)
        if thread.is_alive():
            stop_event.set()
            thread.join(timeout=5)
            if thread.is_alive():
                raise OAuthDeviceError("token poll thread did not stop after timeout")
'''
browser = replace_if_present(browser, old_join, new_join)
ast.parse(browser)
write(BROWSER, browser)

oauth = read(OAUTH)
oauth = oauth.replace("except BaseException as exc:", "except Exception as exc:")
old_poll_header = '''def poll_device_token(
    device_code,
    token_endpoint,
    client_id=CLIENT_ID,
    interval=5,
    expires_in=1800,
    timeout=30.0,
    log=None,
    cancel=None,
    proxy=None,
):
    logger = log or (lambda message: None)
    deadline = time.time() + max(int(expires_in) - 5, 30)
    sleep_seconds = max(int(interval), 1)
    net_streak = 0
    max_net_streak = 20
    while time.time() < deadline:
        if cancel and cancel():
            raise OAuthDeviceError("cancelled")
'''
new_poll_header = '''def _sleep_with_cancel(seconds, cancel=None):
    deadline = time.time() + max(float(seconds), 0.0)
    while time.time() < deadline:
        if cancel and cancel():
            raise OAuthDeviceError("cancelled")
        time.sleep(min(0.2, max(deadline - time.time(), 0.0)))


def poll_device_token(
    device_code,
    token_endpoint,
    client_id=CLIENT_ID,
    interval=5,
    expires_in=1800,
    timeout=30.0,
    log=None,
    cancel=None,
    proxy=None,
):
    logger = log or (lambda message: None)
    deadline = time.time() + max(int(expires_in) - 5, 30)
    sleep_seconds = max(int(interval), 1)
    net_streak = 0
    max_net_streak = 20
    while time.time() < deadline:
        if cancel and cancel():
            raise OAuthDeviceError("cancelled")
'''
if "def _sleep_with_cancel(" not in oauth:
    oauth = replace_required(oauth, old_poll_header, new_poll_header, "oauth cancellable sleep helper")
    oauth = oauth.replace("time.sleep(wait_seconds)", "_sleep_with_cancel(wait_seconds, cancel)")
    oauth = oauth.replace("time.sleep(sleep_seconds)", "_sleep_with_cancel(sleep_seconds, cancel)")
ast.parse(oauth)
write(OAUTH, oauth)

# ---------------------------------------------------------------------------
# CPA schema/writer hardening.
# ---------------------------------------------------------------------------
schema = read(SCHEMA)
old_expired = '''    expired = ""
    if exp:
        expired = datetime.fromtimestamp(exp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
'''
new_expired = '''    expired = ""
    if exp:
        expired = datetime.fromtimestamp(exp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    elif expires_in:
        expired = datetime.fromtimestamp(time.time() + int(expires_in or 0), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
'''
schema = replace_if_present(schema, old_expired, new_expired)
ast.parse(schema)
write(SCHEMA, schema)

writer = read(WRITER)
old_writer_head = '''def write_cpa_xai_auth(auth_dir, payload, filename=None):
    root = Path(auth_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    target_name = filename or credential_file_name(payload.get("email", ""), payload.get("sub", ""))
    if not str(target_name).endswith(".json"):
        target_name = str(target_name) + ".json"
    destination = root / str(target_name)
'''
new_writer_head = '''def _is_relative_to(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def write_cpa_xai_auth(auth_dir, payload, filename=None):
    root = Path(auth_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    target_name = filename or credential_file_name(payload.get("email", ""), payload.get("sub", ""))
    target_name = Path(str(target_name)).name
    if not str(target_name).endswith(".json"):
        target_name = str(target_name) + ".json"
    destination = (root / str(target_name)).resolve()
    if not _is_relative_to(destination, root):
        raise ValueError("CPA auth filename must stay inside auth_dir")
'''
writer = replace_if_present(writer, old_writer_head, new_writer_head)
ast.parse(writer)
write(WRITER, writer)

# ---------------------------------------------------------------------------
# Requirements / ignore / docs.
# ---------------------------------------------------------------------------
req = read(REQ)
req = req.replace("DrissionPage==4.1.1.2", "DrissionPage>=4.1.1.2,<4.2")
write(REQ, req)

gitignore = read(GITIGNORE)
gitignore = ensure_line(gitignore, "screenshots/")
gitignore = ensure_line(gitignore, "*.png")
write(GITIGNORE, gitignore)

readme = read(README)
readme = readme.replace(
    "- `cpa_auths/cpa_auth_failed.txt`：OIDC 导出失败记录。\n- `*.log`：可选日志文件。",
    "- `cpa_auths/cpa_auth_failed.txt`：OIDC 导出失败记录。\n- `screenshots/`：CPA/OIDC 浏览器失败调试截图，已被 `.gitignore` 忽略。\n- `*.log`：可选日志文件。",
)
old_tree = '''```text
.
├── grok_register_ttk.py   # 主程序
├── cf_mail_debug.py       # Cloudflare 邮箱调试工具
├── config.example.json    # 配置示例
├── requirements.txt       # Python 依赖
└── README.md
```
'''
new_tree = '''```text
.
├── grok_register_ttk.py   # 主程序（GUI / CLI）
├── cpa_export.py          # 注册成功后的 CPA/OIDC 导出入口
├── cpa_xai/               # xAI Device Auth、浏览器授权和凭证写入模块
├── cf_mail_debug.py       # Cloudflare 邮箱调试工具
├── config.example.json    # 配置示例
├── requirements.txt       # Python 依赖
├── tests/                 # 现有测试用例
├── assets/                # README 资源
└── README.md
```
'''
readme = replace_if_present(readme, old_tree, new_tree)
write(README, readme)

# Final syntax validation for touched Python files.
for path in (APP, OAUTH, BROWSER, SCHEMA, WRITER):
    ast.parse(read(path, encoding="utf-8"))

print("audit fixes applied")
