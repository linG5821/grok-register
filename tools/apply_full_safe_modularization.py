#!/usr/bin/env python3
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = ROOT / "grok_register_ttk.py"


def node_span(text: str, node: ast.AST) -> tuple[int, int]:
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    start = offsets[node.lineno - 1] + getattr(node, "col_offset", 0)
    end_line = getattr(node, "end_lineno", node.lineno)
    end_col = getattr(node, "end_col_offset", len(lines[end_line - 1]))
    end = offsets[end_line - 1] + end_col
    while end < len(text) and text[end] in "\r\n":
        end += 1
    return start, end


def assignment_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return names


def definition_map(text: str) -> dict[str, ast.AST]:
    tree = ast.parse(text)
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def assignment_map(text: str) -> dict[str, ast.AST]:
    tree = ast.parse(text)
    out: dict[str, ast.AST] = {}
    for node in tree.body:
        for name in assignment_names(node):
            out[name] = node
    return out


def source_for_names(text: str, names: list[str]) -> str:
    defs = definition_map(text)
    chunks = []
    for name in names:
        node = defs.get(name)
        if node is None:
            raise RuntimeError(f"definition not found: {name}")
        start, end = node_span(text, node)
        chunks.append(text[start:end].rstrip() + "\n\n")
    return "".join(chunks)


def source_for_assignments(text: str, names: list[str]) -> str:
    amap = assignment_map(text)
    chunks = []
    for name in names:
        node = amap.get(name)
        if node is None:
            raise RuntimeError(f"assignment not found: {name}")
        start, end = node_span(text, node)
        chunks.append(text[start:end].rstrip() + "\n\n")
    return "".join(chunks)


def remove_top_level(text: str, definition_names=(), assignment_name_set=()) -> str:
    tree = ast.parse(text)
    spans = []
    for node in tree.body:
        remove = False
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            remove = node.name in set(definition_names)
        elif assignment_names(node) & set(assignment_name_set):
            remove = True
        if remove:
            spans.append(node_span(text, node))
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + text[end:]
    return text


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, got {count}")
    return text.replace(old, new, 1)


main_original = MAIN_PATH.read_text(encoding="utf-8")
main = main_original

APP_CONFIG_DEFS = [
    "ConfigError", "_require_bool", "_require_int", "_require_string",
    "validate_config_structure", "validate_run_requirements", "validate_config",
]
TOKEN_NAMES = [
    "resolve_grok2api_local_token_file", "_normalize_sso_token",
    "add_token_to_grok2api_local_pool", "get_grok2api_remote_api_bases",
    "add_token_to_grok2api_remote_pool", "add_token_to_grok2api_pools",
]
RUNTIME_NAMES = [
    "get_configured_proxy", "get_proxies", "_parse_proxy_url", "_safe_proxy_port",
    "_proxy_has_auth", "_strip_proxy_auth", "_proxy_endpoint_terms",
    "is_proxy_connection_error", "page_has_proxy_error",
    "_ReusableThreadingTCPServer", "_proxy_recv_until_headers", "_proxy_relay",
    "_LocalAuthProxyBridgeHandler", "LocalAuthProxyBridge", "prepare_browser_proxy",
    "apply_browser_proxy_option", "create_browser_options", "_build_request_kwargs",
    "http_get", "http_post",
]
MAIL_EXACT = {
    "get_duckmail_api_key", "get_user_agent", "get_domains", "create_account",
    "get_token", "get_messages", "get_message_detail", "generate_username",
    "pick_domain", "get_email_provider", "get_email_and_token", "get_oai_code",
    "extract_verification_code", "duckmail_get_oai_code", "_pick_list_payload",
}
main_defs = definition_map(main_original)
MAIL_NAMES = sorted(
    name for name in main_defs
    if name in MAIL_EXACT
    or name.startswith("cloudflare_")
    or name.startswith("cloudmail_")
    or name.startswith("yyds_")
    or name.startswith("get_cloudflare_")
    or name.startswith("get_cloudmail_")
    or name.startswith("get_yyds_")
)
# Exclude non-mail Cloudflare response helpers used for NSFW settings.
MAIL_NAMES = [name for name in MAIL_NAMES if name not in {
    "is_cloudflare_block_response",
}]
REGISTRATION_NAMES = [
    "generate_random_birthdate", "response_preview", "is_cloudflare_block_response",
    "set_birth_date", "set_tos_accepted", "encode_grpc_nsfw_settings",
    "update_nsfw_settings", "enable_nsfw_for_token", "stop_browser_proxy_bridge",
    "start_browser", "stop_browser", "restart_browser", "cleanup_runtime_memory",
    "refresh_active_page", "click_email_signup_button", "open_signup_page",
    "has_profile_form", "fill_email_and_submit", "fill_code_and_submit",
    "getTurnstileToken", "build_profile", "fill_profile_and_submit",
    "wait_for_sso_cookie",
]

# ---------------------------------------------------------------------------
# app_config.py: standalone mutable config object and two-stage validation.
# ---------------------------------------------------------------------------
default_config_src = source_for_assignments(main_original, ["DEFAULT_CONFIG"])
config_error_src = source_for_names(main_original, ["ConfigError"])
validator_src = source_for_names(main_original, APP_CONFIG_DEFS[1:])
app_config = f'''"""Application configuration loading, normalization, and validation."""
import json
import os
import tempfile
import urllib.parse

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

{default_config_src}
config = DEFAULT_CONFIG.copy()

{config_error_src}
{validator_src}

def _replace_config(value):
    config.clear()
    config.update(value)
    return config


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            return _replace_config(validate_config_structure(loaded))
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError(f"配置文件解析失败: {{CONFIG_FILE}}: {{exc}}") from exc
    return _replace_config(validate_config_structure(DEFAULT_CONFIG.copy()))


def save_config():
    normalized = validate_config_structure(config)
    _replace_config(normalized)
    config_dir = os.path.dirname(os.path.abspath(CONFIG_FILE))
    os.makedirs(config_dir, exist_ok=True)
    fd = None
    temp_path = None
    try:
        fd, temp_path = tempfile.mkstemp(prefix=".config-", suffix=".json.tmp", dir=config_dir)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            json.dump(config, handle, indent=4, ensure_ascii=False)
            handle.write("\\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp_path, 0o600)
        except Exception:
            pass
        os.replace(temp_path, CONFIG_FILE)
        temp_path = None
        try:
            os.chmod(CONFIG_FILE, 0o600)
        except Exception:
            pass
    except Exception as exc:
        raise ConfigError(f"保存配置失败: {{exc}}") from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except Exception:
                pass
    return config
'''
ast.parse(app_config)
(ROOT / "app_config.py").write_text(app_config, encoding="utf-8")

# ---------------------------------------------------------------------------
# browser_runtime.py: single proxy implementation via cpa_xai.proxyutil.
# ---------------------------------------------------------------------------
browser_runtime = r'''"""Shared HTTP, proxy, and Chromium option helpers."""
import os
import urllib.parse

from DrissionPage import ChromiumOptions
from curl_cffi import requests
from cpa_xai.proxyutil import (
    LocalAuthProxyBridge,
    prepare_chromium_proxy,
    proxy_for_chromium,
)

_config = {}
_extension_path = ""


def configure_runtime(config_ref, extension_path=""):
    global _config, _extension_path
    _config = config_ref
    _extension_path = str(extension_path or "")


def get_configured_proxy():
    return str(_config.get("proxy", "") or "").strip()


def get_proxies():
    proxy = get_configured_proxy()
    return {"http": proxy, "https": proxy} if proxy else {}


def _parse_proxy_url(proxy):
    raw = str(proxy or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    try:
        return urllib.parse.urlsplit(raw)
    except Exception:
        return None


def _safe_proxy_port(parsed):
    try:
        return parsed.port
    except Exception:
        return None


def _proxy_has_auth(proxy):
    parsed = _parse_proxy_url(proxy)
    return bool(parsed and parsed.hostname and (parsed.username is not None or parsed.password is not None))


def _strip_proxy_auth(proxy):
    raw = str(proxy or "").strip()
    parsed = _parse_proxy_url(raw)
    if not parsed or not parsed.hostname:
        return raw
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = "[%s]" % host
    port = _safe_proxy_port(parsed)
    netloc = "%s:%s" % (host, port) if port else host
    stripped = urllib.parse.urlunsplit((parsed.scheme or "http", netloc, parsed.path, parsed.query, parsed.fragment))
    return stripped.split("://", 1)[1] if "://" not in raw else stripped


def _proxy_endpoint_terms(proxy=None):
    parsed = _parse_proxy_url(proxy or get_configured_proxy())
    if not parsed or not parsed.hostname:
        return []
    terms = [parsed.hostname]
    port = _safe_proxy_port(parsed)
    if port:
        terms.extend(["%s:%s" % (parsed.hostname, port), "port %s" % port])
    return [item.lower() for item in terms if item]


def is_proxy_connection_error(exc):
    if not get_configured_proxy():
        return False
    err = str(exc or "").lower()
    if not err:
        return False
    if any(item in err for item in ("proxy", "tunnel", "socks")):
        return True
    markers = (
        "could not connect", "failed to connect", "connection refused",
        "connection reset", "connect error", "timed out", "timeout",
    )
    if any(item in err for item in markers):
        terms = _proxy_endpoint_terms()
        return not terms or any(term in err for term in terms)
    return False


def page_has_proxy_error(page_obj):
    try:
        url = str(getattr(page_obj, "url", "") or "")
        title = str(page_obj.run_js("return document.title || ''") or "")
        body = str(page_obj.run_js("return document.body ? document.body.innerText.slice(0, 2000) : ''") or "")
    except Exception:
        return False
    text = "%s\n%s\n%s" % (url, title, body)
    text = text.lower()
    return any(marker in text for marker in (
        "err_proxy", "proxy connection failed", "proxy server",
        "proxy authentication", "tunnel connection failed",
        "无法连接到代理服务器", "代理服务器",
    ))


def prepare_browser_proxy(use_proxy=True, log_callback=None):
    proxy = get_configured_proxy()
    if not use_proxy or not proxy:
        return "", None
    parsed = _parse_proxy_url(proxy)
    if _proxy_has_auth(proxy) and parsed and (parsed.scheme or "http").lower() not in ("http", "https"):
        stripped = _strip_proxy_auth(proxy)
        if log_callback:
            log_callback("[!] Chromium 暂不直接支持该认证代理协议，已使用去认证代理地址，失败将回退直连")
        return stripped, None
    logger = None
    if log_callback:
        logger = lambda message: log_callback("[*] 已为 Chromium启动本地认证代理桥: %s" % message.split(": ", 1)[-1]) if "started authenticated proxy bridge" in message else log_callback(message)
    return prepare_chromium_proxy(proxy, log=logger)


def apply_browser_proxy_option(options, proxy):
    if not proxy:
        return
    if hasattr(options, "set_proxy"):
        try:
            options.set_proxy(proxy)
            return
        except Exception:
            pass
    if not hasattr(options, "set_argument"):
        raise AttributeError("当前 DrissionPage ChromiumOptions 不支持设置浏览器代理")
    try:
        options.set_argument("--proxy-server=%s" % proxy)
    except TypeError:
        options.set_argument("--proxy-server", proxy)


def create_browser_options(browser_proxy=""):
    options = ChromiumOptions()
    options.auto_port()
    options.set_timeouts(base=1)
    apply_browser_proxy_option(options, browser_proxy)
    if _extension_path and os.path.exists(_extension_path):
        options.add_extension(_extension_path)
    return options


def _build_request_kwargs(**kwargs):
    request_kwargs = dict(kwargs)
    proxies = request_kwargs.pop("proxies", None)
    if proxies is None:
        proxies = get_proxies()
    if proxies:
        request_kwargs["proxies"] = proxies
    request_kwargs.setdefault("timeout", 15)
    return request_kwargs


def http_get(url, **kwargs):
    request_kwargs = _build_request_kwargs(**kwargs)
    try:
        return requests.get(url, **request_kwargs)
    except Exception as exc:
        if is_proxy_connection_error(exc):
            direct = dict(request_kwargs)
            direct.pop("proxies", None)
            return requests.get(url, **direct)
        raise


def http_post(url, **kwargs):
    request_kwargs = _build_request_kwargs(**kwargs)
    try:
        return requests.post(url, **request_kwargs)
    except Exception as exc:
        if is_proxy_connection_error(exc):
            direct = dict(request_kwargs)
            direct.pop("proxies", None)
            return requests.post(url, **direct)
        raise
'''
ast.parse(browser_runtime)
(ROOT / "browser_runtime.py").write_text(browser_runtime, encoding="utf-8")

# ---------------------------------------------------------------------------
# account_outputs.py: add token-pool persistence with explicit dependencies.
# ---------------------------------------------------------------------------
account_path = ROOT / "account_outputs.py"
account = account_path.read_text(encoding="utf-8")
if "import time\n" not in account:
    account = account.replace("import tempfile\n", "import tempfile\nimport time\n")
token_source = source_for_names(main_original, TOKEN_NAMES)
account += r'''

# Token-pool runtime dependencies are injected by the application adapter.
config = {}
_http_get = None
_http_post = None
_log_exception = None
_remote_compat_error = RuntimeError
_remote_request_error = RuntimeError


def configure_token_runtime(config_ref, http_get, http_post, log_exception,
                            compatibility_error=RuntimeError, request_error=RuntimeError):
    global config, _http_get, _http_post, _log_exception
    global _remote_compat_error, _remote_request_error
    config = config_ref
    _http_get = http_get
    _http_post = http_post
    _log_exception = log_exception
    _remote_compat_error = compatibility_error
    _remote_request_error = request_error
    globals()["http_get"] = http_get
    globals()["http_post"] = http_post
    globals()["log_exception"] = log_exception
    globals()["RemoteTokenCompatibilityError"] = compatibility_error
    globals()["RemoteTokenRequestError"] = request_error


'''
account += token_source
ast.parse(account)
account_path.write_text(account, encoding="utf-8")

# ---------------------------------------------------------------------------
# mail_service.py: move the existing provider implementations unchanged.
# ---------------------------------------------------------------------------
mail_source = source_for_names(main_original, MAIL_NAMES)
constants = source_for_assignments(main_original, ["DUCKMAIL_API_BASE", "YYDS_API_BASE"])
mail_service = f'''"""Temporary-mail providers shared by GUI, CLI, and debug tooling."""
import re
import secrets
import string
import time
from typing import Any, Dict, List, Optional, Tuple

from curl_cffi import requests

{constants}
config = {{}}
_cf_domain_index = 0
_cloudmail_domain_index = 0
_OWN_NAMES = {set(MAIL_NAMES)!r}


def bind_runtime(namespace):
    global config
    config = namespace.get("config", config)
    for name, value in namespace.items():
        if name.startswith("__") or name in _OWN_NAMES or name in {{"config", "_cf_domain_index", "_cloudmail_domain_index"}}:
            continue
        globals()[name] = value


{mail_source}

class CloudflareMailClient:
    """Standalone Cloudflare mail client used by the debug CLI."""
    def __init__(self, api_base, auth_mode="none", api_key="", create_path="/api/new_address", timeout=20):
        self.api_base = str(api_base or "").rstrip("/")
        self.auth_mode = str(auth_mode or "none").lower()
        self.api_key = str(api_key or "")
        self.create_path = self.normalize_path(create_path, "/api/new_address")
        self.timeout = int(timeout)

    @staticmethod
    def normalize_path(path, default_path):
        raw = (path or default_path).strip() or default_path
        return raw if raw.startswith("/") else "/" + raw

    def build_auth_headers(self, content_type=False):
        headers = {{"Content-Type": "application/json"}} if content_type else {{}}
        if not self.api_key:
            return headers
        if self.auth_mode == "x-admin-auth":
            headers["x-admin-auth"] = self.api_key
        elif self.auth_mode == "x-api-key":
            headers["X-API-Key"] = self.api_key
        elif self.auth_mode == "bearer":
            headers["Authorization"] = "Bearer " + self.api_key
        return headers

    @staticmethod
    def json_or_text(response):
        try:
            return response.json(), ""
        except Exception:
            return None, str(getattr(response, "text", "") or "")[:400]

    def create_address(self, domain="", name=""):
        is_admin = self.create_path.rstrip("/").lower() == "/admin/new_address"
        payload = {{}}
        headers = {{"Content-Type": "application/json"}}
        if is_admin:
            payload = {{"name": name.strip() if str(name).strip() else generate_username(), "enablePrefix": True}}
            if str(domain).strip():
                payload["domain"] = str(domain).strip()
            headers = self.build_auth_headers(content_type=True)
        elif str(domain).strip():
            payload["domain"] = str(domain).strip()
        response = requests.post(self.api_base + self.create_path, json=payload, headers=headers, timeout=self.timeout)
        response.raise_for_status()
        data, raw = self.json_or_text(response)
        if not data:
            raise RuntimeError("%s 非JSON: %s" % (self.create_path, raw))
        address = str(data.get("address", "")).strip()
        jwt = str(data.get("jwt", "")).strip()
        if not address or not jwt:
            raise RuntimeError("%s 缺少 address/jwt: %r" % (self.create_path, data))
        return address, jwt

    def fetch_box(self, jwt, path, params):
        response = requests.get(
            self.api_base + path,
            params=params,
            headers={{"Authorization": "Bearer " + str(jwt)}},
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            return []
        data, _ = self.json_or_text(response)
        return _pick_list_payload(data)

    def probe_all_boxes(self, jwt):
        probes = [
            ("/api/mails", {{"limit": 20, "offset": 0}}),
            ("/api/sendbox", {{"limit": 20, "offset": 0}}),
            ("/api/mails", {{"limit": 20, "offset": 0, "box": "trash"}}),
            ("/api/mails", {{"limit": 20, "offset": 0, "folder": "trash"}}),
            ("/api/mails", {{"limit": 20, "offset": 0, "deleted": "1"}}),
            ("/api/mails", {{"limit": 20, "offset": 0, "status": "deleted"}}),
        ]
        return [("%s?%s" % (path, params), self.fetch_box(jwt, path, params)) for path, params in probes]

    def get_detail(self, jwt, mail_id):
        for path in ("/api/mail/%s" % mail_id, "/api/mails/%s" % mail_id):
            try:
                response = requests.get(
                    self.api_base + path,
                    headers={{"Authorization": "Bearer " + str(jwt)}},
                    timeout=self.timeout,
                )
                if response.status_code >= 400:
                    continue
                data, _ = self.json_or_text(response)
                if isinstance(data, dict):
                    return data
            except Exception:
                continue
        return {{}}

    @staticmethod
    def flatten_mail_text(item, detail):
        subject = str(item.get("subject") or detail.get("subject") or "")
        parts = []
        for source in (item, detail):
            for key in ("text", "raw", "content", "intro", "body", "snippet"):
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    parts.append(value)
            html_value = source.get("html")
            if isinstance(html_value, str):
                html_value = [html_value]
            if isinstance(html_value, list):
                parts.extend(re.sub(r"<[^>]+>", " ", item) for item in html_value if isinstance(item, str))
        return subject, "\\n".join(parts)
'''
ast.parse(mail_service)
(ROOT / "mail_service.py").write_text(mail_service, encoding="utf-8")

# ---------------------------------------------------------------------------
# registration_browser.py: move browser state and page automation unchanged.
# ---------------------------------------------------------------------------
registration_source = source_for_names(main_original, REGISTRATION_NAMES)
registration_browser = f'''"""Registration browser lifecycle and page automation."""
import gc
import random
import re
import secrets
import struct
import time

from DrissionPage import Chromium
from DrissionPage.errors import PageDisconnectedError
from curl_cffi import requests

browser = None
page = None
browser_proxy_bridge = None
browser_started_with_proxy = False
cf_clearance = ""
SIGNUP_URL = "https://accounts.x.ai/sign-up"
_OWN_NAMES = {set(REGISTRATION_NAMES)!r}


def bind_runtime(namespace):
    for name, value in namespace.items():
        if name.startswith("__") or name in _OWN_NAMES or name in {{
            "browser", "page", "browser_proxy_bridge", "browser_started_with_proxy", "cf_clearance",
        }}:
            continue
        globals()[name] = value


{registration_source}
'''
ast.parse(registration_browser)
(ROOT / "registration_browser.py").write_text(registration_browser, encoding="utf-8")

# ---------------------------------------------------------------------------
# Rewrite main as GUI/CLI adapter plus compatibility wrappers.
# ---------------------------------------------------------------------------
main = remove_top_level(main, APP_CONFIG_DEFS, {"CONFIG_FILE", "DEFAULT_CONFIG", "config", "_cf_domain_index", "_cloudmail_domain_index"})
main = remove_top_level(main, TOKEN_NAMES)
main = remove_top_level(main, RUNTIME_NAMES)
main = remove_top_level(main, MAIL_NAMES, {"DUCKMAIL_API_BASE", "YYDS_API_BASE"})
main = remove_top_level(main, REGISTRATION_NAMES, {"browser", "page", "browser_proxy_bridge", "browser_started_with_proxy", "cf_clearance", "SIGNUP_URL"})

import_block = '''\nimport functools\nimport app_config as _app_config\nimport account_outputs as _account_outputs\nimport browser_runtime as _browser_runtime\nimport mail_service as _mail_service\nimport registration_browser as _registration_browser\nfrom app_config import (\n    CONFIG_FILE, DEFAULT_CONFIG, ConfigError, config, load_config, save_config,\n    validate_config, validate_config_structure, validate_run_requirements,\n)\n\n'''
main = replace_once(main, "from curl_cffi import requests\n", "from curl_cffi import requests\n" + import_block, "main imports")

compat_block = f'''\ndef _make_compat_proxy(module, name, binder=None):
    target = getattr(module, name)
    @functools.wraps(target)
    def proxy(*args, **kwargs):
        if binder is not None:
            binder()
        return getattr(module, name)(*args, **kwargs)
    return proxy


def _bind_browser_runtime():
    _browser_runtime.configure_runtime(config, EXTENSION_PATH)


def _bind_account_outputs():
    _account_outputs.configure_token_runtime(
        config, http_get, http_post, log_exception,
        compatibility_error=RemoteTokenCompatibilityError,
        request_error=RemoteTokenRequestError,
    )


def _bind_mail_service():
    _mail_service.bind_runtime(globals())


def _bind_registration_browser():
    _registration_browser.bind_runtime(globals())


LocalAuthProxyBridge = _browser_runtime.LocalAuthProxyBridge
for _name in {RUNTIME_NAMES!r}:
    if _name.startswith("_") and _name in {{"_ReusableThreadingTCPServer", "_LocalAuthProxyBridgeHandler", "_proxy_recv_until_headers", "_proxy_relay"}}:
        continue
    if _name != "LocalAuthProxyBridge":
        globals()[_name] = _make_compat_proxy(_browser_runtime, _name, _bind_browser_runtime)
for _name in {TOKEN_NAMES!r}:
    globals()[_name] = _make_compat_proxy(_account_outputs, _name, _bind_account_outputs)
for _name in {MAIL_NAMES!r}:
    globals()[_name] = _make_compat_proxy(_mail_service, _name, _bind_mail_service)
for _name in {REGISTRATION_NAMES!r}:
    globals()[_name] = _make_compat_proxy(_registration_browser, _name, _bind_registration_browser)


def __getattr__(name):
    if name in {{"browser", "page", "browser_proxy_bridge", "browser_started_with_proxy", "cf_clearance"}}:
        return getattr(_registration_browser, name)
    raise AttributeError(name)


'''
main = replace_once(main, "def raise_if_cancelled(cancel_callback=None):", compat_block + "def raise_if_cancelled(cancel_callback=None):", "compat block")
main = main.replace("current_page = page", "current_page = _registration_browser.page")
main = main.replace("browser_missing=lambda: browser is None", "browser_missing=lambda: _registration_browser.browser is None")
ast.parse(main)
MAIN_PATH.write_text(main, encoding="utf-8")

# ---------------------------------------------------------------------------
# CPA browser lifecycle extraction.
# ---------------------------------------------------------------------------
browser_confirm_path = ROOT / "cpa_xai" / "browser_confirm.py"
browser_confirm_original = browser_confirm_path.read_text(encoding="utf-8")
SESSION_NAMES = [
    "_noop_log", "BrowserConfirmError", "_sleep", "create_standalone_page",
    "close_standalone", "_register_mint_browser", "_unregister_mint_browser",
    "_mint_tls_get", "clear_page_session", "normalize_cookies", "inject_cookies",
    "acquire_mint_browser", "release_mint_browser", "shutdown_mint_browsers",
]
session_source = source_for_names(browser_confirm_original, SESSION_NAMES)
browser_session = '''"""CPA Chromium session lifecycle, reuse, cleanup, and cookies."""\nfrom __future__ import annotations\n\nimport os\nimport sys\nimport threading\nimport time\nfrom pathlib import Path\nfrom typing import Any, Callable, Optional\n\nLogFn = Callable[[str], None]\n_mint_tls = threading.local()\n_mint_registry_lock = threading.Lock()\n_mint_registry = set()\n\n''' + session_source
ast.parse(browser_session)
(ROOT / "cpa_xai" / "browser_session.py").write_text(browser_session, encoding="utf-8")

browser_confirm = remove_top_level(browser_confirm_original, SESSION_NAMES, {"_mint_tls", "_mint_registry_lock", "_mint_registry"})
session_import = '''from .browser_session import (\n    BrowserConfirmError, _noop_log, _sleep, create_standalone_page, close_standalone,\n    clear_page_session, normalize_cookies, inject_cookies, acquire_mint_browser,\n    release_mint_browser, shutdown_mint_browsers,\n)\n\n'''
browser_confirm = replace_once(browser_confirm, "LogFn = Callable[[str], None]\n", "LogFn = Callable[[str], None]\n\n" + session_import, "browser session import")
ast.parse(browser_confirm)
browser_confirm_path.write_text(browser_confirm, encoding="utf-8")

# ---------------------------------------------------------------------------
# cf_mail_debug.py becomes a thin CLI around CloudflareMailClient.
# ---------------------------------------------------------------------------
cf_debug = r'''#!/usr/bin/env python
# -*- coding: utf-8 -*-
import argparse
import time

from mail_service import CloudflareMailClient, extract_verification_code


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-base", required=True)
    parser.add_argument("--address", default="")
    parser.add_argument("--credential", default="")
    parser.add_argument("--auth-mode", default="none", choices=["none", "bearer", "x-api-key", "x-admin-auth"])
    parser.add_argument("--api-key", default="")
    parser.add_argument("--create-path", default="/api/new_address")
    parser.add_argument("--domain", default="")
    parser.add_argument("--name", default="")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--interval", type=int, default=3)
    args = parser.parse_args()

    client = CloudflareMailClient(
        args.api_base,
        auth_mode=args.auth_mode,
        api_key=args.api_key,
        create_path=args.create_path,
    )
    address = args.address.strip()
    credential = args.credential.strip()
    if not credential:
        address, credential = client.create_address(domain=args.domain, name=args.name)
        print("[NEW] address=%s" % address)
        print("[NEW] credential(jwt)=%s" % credential)
    else:
        print("[USE] address=%s" % (address or "(unknown, from credential)"))

    deadline = time.time() + max(args.timeout, 1)
    seen_ids = set()
    while time.time() < deadline:
        boxes = client.probe_all_boxes(credential)
        total = 0
        for box_name, mails in boxes:
            if mails:
                print("[BOX] %s -> %s" % (box_name, len(mails)))
            total += len(mails)
            for item in mails:
                mail_id = item.get("id") or item.get("mail_id")
                if not mail_id or mail_id in seen_ids:
                    continue
                seen_ids.add(mail_id)
                detail = client.get_detail(credential, mail_id)
                subject, text = client.flatten_mail_text(item, detail)
                code = extract_verification_code(text, subject)
                print("[MAIL] id=%s subject=%r code=%r" % (mail_id, subject, code))
                if code:
                    print("[FOUND] %s" % code)
                    return
        if total == 0:
            print("[INFO] no mails yet")
        time.sleep(max(args.interval, 1))
    print("[TIMEOUT] no code found")


if __name__ == "__main__":
    main()
'''
ast.parse(cf_debug)
(ROOT / "cf_mail_debug.py").write_text(cf_debug, encoding="utf-8")

# ---------------------------------------------------------------------------
# cpa_export.py settings object and import without sys.path mutation.
# ---------------------------------------------------------------------------
cpa_export = r'''"""Optional post-registration CPA/OIDC export hook."""
from dataclasses import dataclass
import importlib.util
import os
import shutil
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_DEFAULT_AUTH_DIR = _ROOT / "cpa_auths"


@dataclass(frozen=True)
class CpaExportSettings:
    enabled: bool
    auth_dir: Path
    hotload_dir: Path | None
    copy_to_hotload: bool
    proxy: str
    headless: bool
    mint_timeout: float
    request_timeout: float
    poll_timeout: float
    base_url: str
    force_standalone: bool
    cookie_inject: bool
    tools_dir: str

    @classmethod
    def from_config(cls, config):
        cfg = dict(config or {})
        auth_dir = Path(cfg.get("cpa_auth_dir") or _DEFAULT_AUTH_DIR).expanduser()
        if not auth_dir.is_absolute():
            auth_dir = (_ROOT / auth_dir).resolve()
        hotload_value = str(cfg.get("cpa_hotload_dir") or "").strip()
        hotload_dir = Path(hotload_value).expanduser() if hotload_value else None
        if hotload_dir is not None and not hotload_dir.is_absolute():
            hotload_dir = (_ROOT / hotload_dir).resolve()
        return cls(
            enabled=bool(cfg.get("cpa_export_enabled", False)),
            auth_dir=auth_dir,
            hotload_dir=hotload_dir,
            copy_to_hotload=bool(cfg.get("cpa_copy_to_hotload", False)),
            proxy=str(cfg.get("cpa_proxy") or cfg.get("proxy") or "").strip(),
            headless=bool(cfg.get("cpa_headless", False)),
            mint_timeout=float(cfg.get("cpa_mint_timeout_sec") or 300),
            request_timeout=float(cfg.get("cpa_oidc_request_timeout_sec") or 15),
            poll_timeout=float(cfg.get("cpa_oidc_poll_timeout_sec") or 15),
            base_url=str(cfg.get("cpa_base_url") or "https://cli-chat-proxy.grok.com/v1").strip(),
            force_standalone=bool(cfg.get("cpa_force_standalone", True)),
            cookie_inject=bool(cfg.get("cpa_mint_cookie_inject", True)),
            tools_dir=str(cfg.get("api_reverse_tools") or "").strip(),
        )


def _load_mint_and_export(tools_dir=""):
    tools_value = str(tools_dir or "").strip()
    if not tools_value:
        from cpa_xai import mint_and_export
        return mint_and_export
    tools = Path(tools_value).expanduser().resolve()
    package = tools if tools.name == "cpa_xai" else tools / "cpa_xai"
    init_path = package / "__init__.py"
    if package.resolve() == (_ROOT / "cpa_xai").resolve():
        from cpa_xai import mint_and_export
        return mint_and_export
    if not init_path.is_file():
        raise ImportError("cpa_xai package not found under %s" % tools)
    module_name = "_external_cpa_xai_%s" % abs(hash(str(package)))
    spec = importlib.util.spec_from_file_location(
        module_name,
        str(init_path),
        submodule_search_locations=[str(package)],
    )
    if spec is None or spec.loader is None:
        raise ImportError("unable to load cpa_xai from %s" % package)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.mint_and_export


def export_cookies_from_page(page):
    if page is None:
        return []
    cookies = None
    for getter in (
        lambda: page.cookies(all_domains=True, all_info=True),
        lambda: page.cookies(all_domains=True),
        lambda: page.cookies(),
    ):
        try:
            cookies = getter()
            if cookies:
                break
        except TypeError:
            continue
        except Exception:
            continue
    if not cookies:
        try:
            browser = getattr(page, "browser", None)
            if browser is not None:
                cookies = browser.cookies()
        except Exception:
            cookies = None
    return [item for item in cookies if isinstance(item, dict)] if isinstance(cookies, list) else []


def _normalize_result(result, email=""):
    value = dict(result or {})
    value.setdefault("ok", False)
    value.setdefault("skipped", False)
    value.setdefault("email", str(email or ""))
    value.setdefault("error", None)
    return value


def export_cpa_xai_for_account(email, password, page=None, cookies=None, sso=None,
                               config=None, log_callback=None, cancel_callback=None):
    settings = CpaExportSettings.from_config(config)
    log = log_callback or (lambda message: None)
    if not settings.enabled:
        return _normalize_result({"ok": False, "skipped": True, "reason": "disabled"}, email)
    try:
        mint_and_export = _load_mint_and_export(settings.tools_dir)
    except Exception as exc:
        log("[cpa] import cpa_xai failed: %s" % exc)
        return _normalize_result({"ok": False, "error": "import: %s" % exc}, email)

    use_cookies = cookies
    if use_cookies is None and settings.cookie_inject and page is not None:
        use_cookies = export_cookies_from_page(page)
    if not settings.cookie_inject:
        use_cookies = None
    elif sso:
        base = list(use_cookies) if isinstance(use_cookies, list) else []
        sso_value = str(sso).strip()
        for cookie_name in ("sso", "sso-rw"):
            for domain in (".x.ai", "accounts.x.ai", ".accounts.x.ai", "auth.x.ai", ".auth.x.ai", "grok.com", ".grok.com"):
                base.append({"name": cookie_name, "value": sso_value, "domain": domain,
                             "path": "/", "secure": True, "httpOnly": True})
        use_cookies = base

    settings.auth_dir.mkdir(parents=True, exist_ok=True)
    log("[cpa] mint OIDC for %s -> %s" % (email, settings.auth_dir))
    result = mint_and_export(
        email=email, password=password, auth_dir=settings.auth_dir,
        page=None if settings.force_standalone else page,
        proxy=settings.proxy or None, headless=settings.headless,
        base_url=settings.base_url, browser_timeout_sec=settings.mint_timeout,
        force_standalone=settings.force_standalone, cookies=use_cookies,
        reuse_browser=True, recycle_every=15,
        log=lambda message: log("[cpa] %s" % message), cancel=cancel_callback,
        request_timeout_sec=settings.request_timeout,
        poll_timeout_sec=settings.poll_timeout,
    )
    result = _normalize_result(result, email)
    if result.get("ok") and result.get("path") and settings.copy_to_hotload and settings.hotload_dir:
        try:
            settings.hotload_dir.mkdir(parents=True, exist_ok=True)
            source = Path(result["path"])
            target = settings.hotload_dir / source.name
            shutil.copy2(str(source), str(target))
            try:
                os.chmod(str(target), 0o600)
            except Exception:
                pass
            result["hotload_path"] = str(target)
            log("[cpa] hotload copy -> %s" % target)
        except Exception as exc:
            result["cpa_copy_error"] = str(exc)
            log("[cpa] hotload copy failed: %s" % exc)
    if not result.get("ok"):
        fail_path = settings.auth_dir / "cpa_auth_failed.txt"
        try:
            with open(str(fail_path), "a", encoding="utf-8") as handle:
                handle.write("%s----%s----%s\n" % (email, result.get("error") or "unknown", int(time.time())))
        except Exception as exc:
            log("[cpa] failed to persist failure record: %s" % exc)
    return result
'''
ast.parse(cpa_export)
(ROOT / "cpa_export.py").write_text(cpa_export, encoding="utf-8")

# ---------------------------------------------------------------------------
# Additional regression tests.
# ---------------------------------------------------------------------------
compat_tests = f'''import unittest
from unittest.mock import patch

import app_config
import grok_register_ttk as app
import mail_service
import registration_browser
from cpa_xai import browser_confirm
from cpa_xai import browser_session


class ModuleCompatibilityTests(unittest.TestCase):
    def test_config_object_is_shared(self):
        self.assertIs(app.config, app_config.config)

    def test_original_public_functions_remain_available(self):
        names = {sorted(set(MAIL_NAMES + REGISTRATION_NAMES + TOKEN_NAMES + ["http_get", "http_post", "create_browser_options"]))!r}
        for name in names:
            self.assertTrue(callable(getattr(app, name)), name)

    def test_mail_wrapper_delegates(self):
        with patch.object(mail_service, "get_email_provider", return_value="duckmail") as mocked:
            self.assertEqual(app.get_email_provider(), "duckmail")
            mocked.assert_called_once_with()

    def test_browser_confirm_reexports_session_api(self):
        self.assertIs(browser_confirm.close_standalone, browser_session.close_standalone)
        self.assertIs(browser_confirm.normalize_cookies, browser_session.normalize_cookies)


if __name__ == "__main__":
    unittest.main()
'''
(ROOT / "tests" / "test_module_compatibility.py").write_text(compat_tests, encoding="utf-8")

flow_test_path = ROOT / "tests" / "test_registration_flow.py"
flow_tests = flow_test_path.read_text(encoding="utf-8")
extra_flow = r'''
    def test_cleanup_failure_does_not_change_success_statistics(self):
        fake = FakeOps()
        ops = fake.operations()
        base_cleanup = ops.cleanup
        def cleanup(reason):
            if "已成功" in reason:
                raise RuntimeError("cleanup failed")
            base_cleanup(reason)
        ops.cleanup = cleanup
        batch = run_batch(2, self.callbacks(), lambda *args: None, ops, cleanup_interval=1)
        self.assertEqual((batch.success_count, batch.fail_count, batch.processed_count), (2, 0, 2))

    def test_cancel_during_next_account_wait_is_normal_cancellation(self):
        fake = FakeOps()
        ops = fake.operations()
        ops.sleep = lambda seconds: (_ for _ in ()).throw(Cancelled())
        batch = run_batch(2, self.callbacks(), lambda *args: None, ops)
        self.assertTrue(batch.cancelled)
        self.assertEqual(batch.processed_count, 1)

    def test_final_cleanup_does_not_mask_original_error(self):
        fake = FakeOps()
        ops = fake.operations()
        ops.start_browser = lambda: (_ for _ in ()).throw(ValueError("original"))
        ops.cleanup = lambda reason: (_ for _ in ()).throw(RuntimeError("cleanup"))
        with self.assertRaisesRegex(ValueError, "original"):
            run_batch(1, self.callbacks(), lambda *args: None, ops)

    def test_optional_postprocessing_exceptions_become_warning(self):
        fake = FakeOps()
        ops = fake.operations()
        ops.add_tokens = lambda sso, email: (_ for _ in ()).throw(RuntimeError("pool"))
        ops.export_cpa = lambda email, password, sso: (_ for _ in ()).throw(RuntimeError("cpa"))
        batch = run_batch(1, self.callbacks(), lambda *args: None, ops)
        self.assertEqual(batch.success_count, 1)
        self.assertEqual(batch.postprocess_warning_count, 1)
'''
flow_tests = replace_once(flow_tests, '\n\nif __name__ == "__main__":\n', extra_flow + '\n\nif __name__ == "__main__":\n', "flow tests")
flow_test_path.write_text(flow_tests, encoding="utf-8")

oauth_tests = r'''import json
import unittest
from unittest.mock import patch

from cpa_xai import oauth_device as oauth


class Response:
    def __init__(self, body, status=200):
        self.body = body.encode("utf-8")
        self.status = status
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        return self.body


class Opener:
    def __init__(self, actions):
        self.actions = list(actions)
        self.calls = 0
    def open(self, request, timeout=None):
        self.calls += 1
        action = self.actions.pop(0)
        if isinstance(action, BaseException):
            raise action
        return action


class OAuthDeviceTests(unittest.TestCase):
    def test_discovery_success(self):
        payload = {"device_authorization_endpoint": "https://auth.x.ai/device", "token_endpoint": "https://auth.x.ai/token"}
        opener = Opener([Response(json.dumps(payload))])
        with patch.object(oauth, "_build_opener", return_value=opener):
            self.assertEqual(oauth.discover(retries=0)["token_endpoint"], payload["token_endpoint"])

    def test_discovery_cancelled_before_request(self):
        with self.assertRaisesRegex(oauth.OAuthDeviceError, "cancelled"):
            oauth.discover(cancel=lambda: True)

    def test_discovery_retries_transient_error(self):
        payload = {"device_authorization_endpoint": "https://auth.x.ai/device", "token_endpoint": "https://auth.x.ai/token"}
        opener = Opener([TimeoutError("slow"), Response(json.dumps(payload))])
        with patch.object(oauth, "_build_opener", return_value=opener), patch.object(oauth, "_sleep_with_cancel"):
            oauth.discover(retries=1)
        self.assertEqual(opener.calls, 2)

    def test_post_form_returns_non_json_body(self):
        opener = Opener([Response("not-json", status=502)])
        with patch.object(oauth, "_build_opener", return_value=opener):
            status, payload = oauth._post_form("https://auth.x.ai/token", {}, retries=0)
        self.assertEqual((status, payload), (502, "not-json"))

    def test_slow_down_increases_wait(self):
        responses = [
            (400, {"error": "slow_down"}),
            (200, {"access_token": "a", "refresh_token": "r"}),
        ]
        waits = []
        with patch.object(oauth, "_post_form", side_effect=responses), patch.object(oauth, "_sleep_with_cancel", side_effect=lambda seconds, cancel=None: waits.append(seconds)):
            result = oauth.poll_device_token("d", "https://auth.x.ai/token", interval=1, expires_in=60)
        self.assertEqual(result.refresh_token, "r")
        self.assertEqual(waits, [6])


if __name__ == "__main__":
    unittest.main()
'''
(ROOT / "tests" / "test_oauth_device.py").write_text(oauth_tests, encoding="utf-8")

browser_session_tests = r'''import unittest
from cpa_xai import browser_session


class Bridge:
    def __init__(self):
        self.stops = 0
    def stop(self):
        self.stops += 1


class Browser:
    def __init__(self, bridge=None):
        self._cpa_proxy_bridge = bridge
        self.quits = 0
    def quit(self):
        self.quits += 1


class BrowserSessionTests(unittest.TestCase):
    def test_close_standalone_closes_browser_and_bridge(self):
        bridge = Bridge()
        browser = Browser(bridge)
        browser_session._register_mint_browser(browser)
        browser_session.close_standalone(browser)
        self.assertEqual(browser.quits, 1)
        self.assertEqual(bridge.stops, 1)

    def test_normalize_cookies_rejects_invalid_items(self):
        value = browser_session.normalize_cookies([None, {"name": "a", "value": "b"}, "bad"])
        self.assertEqual(len(value), 1)
        self.assertEqual(value[0]["name"], "a")


if __name__ == "__main__":
    unittest.main()
'''
(ROOT / "tests" / "test_browser_session.py").write_text(browser_session_tests, encoding="utf-8")

core_tests = r'''import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cpa_xai.mint import mint_and_export
from cpa_xai.schema import build_cpa_xai_auth, jwt_payload
from cpa_xai.writer import write_cpa_xai_auth


class CpaCoreTests(unittest.TestCase):
    def test_schema_rejects_missing_tokens(self):
        with self.assertRaises(ValueError):
            build_cpa_xai_auth("a@example.com", "", "refresh")
        with self.assertRaises(ValueError):
            jwt_payload("not-a-jwt")

    def test_writer_failure_does_not_leave_temp_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("cpa_xai.writer.os.replace", side_effect=OSError("disk")):
                with self.assertRaises(OSError):
                    write_cpa_xai_auth(directory, {"email": "a@example.com"}, "a.json")
            self.assertEqual([p.name for p in Path(directory).iterdir()], [])

    def test_mint_rejects_missing_identity_without_browser(self):
        result = mint_and_export("", "", tempfile.gettempdir())
        self.assertFalse(result["ok"])
        self.assertIn("missing", result["error"])


if __name__ == "__main__":
    unittest.main()
'''
(ROOT / "tests" / "test_cpa_core.py").write_text(core_tests, encoding="utf-8")

# Syntax validation for every touched Python file.
for path in [
    MAIN_PATH, ROOT / "app_config.py", ROOT / "account_outputs.py",
    ROOT / "browser_runtime.py", ROOT / "mail_service.py",
    ROOT / "registration_browser.py", browser_confirm_path,
    ROOT / "cpa_xai" / "browser_session.py", ROOT / "cf_mail_debug.py",
    ROOT / "cpa_export.py",
] + list((ROOT / "tests").glob("test_*.py")):
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

print("full safe modularization applied")
