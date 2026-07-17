"""集中管理代理会话：占位符替换、每号一 IP 的 session 轮转、代理池选择、出口 IP 自检。

设计目标
--------
- 单代理：``config.proxy`` 支持 ``http://user-session-{rand}:pass@host:7000`` 占位符
- 代理池：``config.proxy_pool`` 支持列出多个不同协议 URL（http/https/socks4/socks5），
  每号按策略挑一个使用（round_robin / random / sticky）
- 池条目本身也支持 ``{rand}`` 占位符，可以混用商业代理和自建 socks5

对上层不透明：所有下游代码（browser_runtime.get_configured_proxy、
cpa_xai.proxyutil.resolve_proxy、mail_service 的 http_get/http_post）
最终只调用 ``expand_proxy(raw)``，本模块负责根据当前"选中"状态返回同一个 URL。

用法
----
只有两个对外方法：

- ``rotate_session(reason)`` — 换号时调用，重新掷 rand 并从池里挑一个
  新条目；本模块自动同步 ``os.environ`` 的 ``HTTPS_PROXY/HTTP_PROXY``
- ``expand_proxy(raw)`` — 供下游代理消费方使用；含占位符时展开成
  当前号已锁定的 URL

启动自检 ``diagnose_egress(logger)`` 可选择性调用；用当前展开后的代理请求
``https://ipinfo.io/json``，把 ASN / country / hosting 打印到日志。
"""

from __future__ import annotations

import json
import os
import random
import secrets
import threading
import urllib.parse

from curl_cffi import requests


_lock = threading.Lock()
_current_session_id: str = ""
_current_pool_url: str = ""     # 当前号选中的池条目原文（含占位符）
_current_pool_index: int = -1   # round_robin 游标
_last_expanded: str = ""
_placeholder_tokens = ("{rand}", "{RAND}", "{session}", "{SESSION}")

_VALID_STRATEGIES = ("round_robin", "random", "sticky")


def _new_session_id(length: int = 12) -> str:
    return secrets.token_hex(length // 2 or 6)


def _load_config() -> dict:
    try:
        from app_config import config as _cfg  # type: ignore
        return dict(_cfg)
    except Exception:
        return {}


def _pool_entries() -> list[str]:
    cfg = _load_config()
    pool = cfg.get("proxy_pool") or []
    if not isinstance(pool, list):
        return []
    result = []
    for entry in pool:
        text = str(entry or "").strip()
        if text:
            result.append(text)
    return result


def _pool_strategy() -> str:
    cfg = _load_config()
    value = str(cfg.get("proxy_pool_strategy", "round_robin") or "round_robin").strip()
    if value not in _VALID_STRATEGIES:
        value = "round_robin"
    return value


def _pick_from_pool() -> str:
    pool = _pool_entries()
    if not pool:
        return ""
    global _current_pool_index
    strategy = _pool_strategy()
    if strategy == "sticky":
        if 0 <= _current_pool_index < len(pool):
            return pool[_current_pool_index]
        _current_pool_index = 0
        return pool[0]
    if strategy == "random":
        _current_pool_index = random.randrange(len(pool))
        return pool[_current_pool_index]
    # round_robin
    _current_pool_index = (_current_pool_index + 1) % len(pool)
    return pool[_current_pool_index]


def rotate_session(reason: str = "") -> str:
    """换号时调用：换一个 rand、并从池里挑一个新条目（sticky 除外）。

    返回新的 session id，方便调用方 log。
    """
    global _current_session_id, _current_pool_url
    with _lock:
        _current_session_id = _new_session_id()
        pool = _pool_entries()
        if pool:
            _current_pool_url = _pick_from_pool()
        else:
            _current_pool_url = ""
        session_id = _current_session_id
    _sync_environment()
    return session_id


def current_session_id() -> str:
    with _lock:
        return _current_session_id


def current_pool_url() -> str:
    """调试/日志用：当前号锁定的池条目原文（可能包含 {rand}）。"""
    with _lock:
        return _current_pool_url


def _ensure_session_id() -> str:
    with _lock:
        if not _current_session_id:
            return ""
        return _current_session_id


def _has_placeholder(raw: str) -> bool:
    return any(token in raw for token in _placeholder_tokens)


def _resolve_active_proxy(fallback_raw: str) -> str:
    """决定当前号"要用哪个代理"。

    - 池非空 → 走当前锁定的池条目（rotate_session 时选定）
    - 池为空 → 用 fallback_raw（通常是 config.proxy）
    """
    with _lock:
        active = _current_pool_url
    if active:
        return active
    return fallback_raw


def expand_proxy(raw: str) -> str:
    """替换 URL 中的 {rand}/{session} 占位符为当前 session id。

    - 若配置了 proxy_pool，忽略 raw、使用当前锁定的池条目
    - 池非空但尚未 rotate_session() 时自动 rotate 一次
    - 若含占位符但尚未 rotate_session()，会自动 rotate 一次保证结果稳定
    """
    global _last_expanded
    fallback = str(raw or "").strip()
    active = _resolve_active_proxy(fallback)
    if not active and _pool_entries():
        rotate_session("auto-init-pool")
        active = _resolve_active_proxy(fallback)
    if not active:
        _last_expanded = ""
        return ""
    if _has_placeholder(active):
        session_id = _ensure_session_id() or rotate_session("auto-init")
        for token in _placeholder_tokens:
            active = active.replace(token, session_id)
    _last_expanded = active
    return active


def last_expanded_proxy() -> str:
    return _last_expanded


_PROXY_ENV_KEYS = (
    "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
    "ALL_PROXY", "all_proxy",
)
_NO_PROXY_VALUE = "localhost,127.0.0.1,::1,.local"


def _sync_environment() -> None:
    """把当前号选中并展开后的代理写入 HTTPS_PROXY/HTTP_PROXY。

    同时写 NO_PROXY，避免 curl 等把本机请求也走代理。
    注意：Chromium 启动前仍须临时清掉这些变量（见 browser 启动路径），
    否则 Windows 上 Chrome 会继承 HTTP_PROXY 导致 CDP 失败。
    """
    cfg = _load_config()
    fallback = str(cfg.get("proxy", "") or "").strip()
    expanded = expand_proxy(fallback)
    if not expanded:
        for key in _PROXY_ENV_KEYS:
            os.environ.pop(key, None)
        return
    os.environ["HTTPS_PROXY"] = expanded
    os.environ["HTTP_PROXY"] = expanded
    os.environ["https_proxy"] = expanded
    os.environ["http_proxy"] = expanded
    # 本机与局域网环回不走代理
    for key in ("NO_PROXY", "no_proxy"):
        existing = str(os.environ.get(key) or "").strip()
        if existing:
            if "127.0.0.1" not in existing:
                os.environ[key] = existing + "," + _NO_PROXY_VALUE
        else:
            os.environ[key] = _NO_PROXY_VALUE


def clear_proxy_environment() -> dict:
    """临时清除代理相关环境变量，返回被清掉的原值以便恢复。"""
    saved = {}
    for key in _PROXY_ENV_KEYS + ("NO_PROXY", "no_proxy"):
        if key in os.environ:
            saved[key] = os.environ.pop(key)
    return saved


def restore_proxy_environment(saved: dict) -> None:
    for key, value in (saved or {}).items():
        os.environ[key] = value


def _safe_label(url: str) -> str:
    if not url:
        return "(direct)"
    try:
        parsed = urllib.parse.urlsplit(url if "://" in url else "http://" + url)
    except Exception:
        return "(proxy)"
    host = parsed.hostname or "?"
    port = ""
    try:
        if parsed.port:
            port = ":%s" % parsed.port
    except Exception:
        port = ""
    scheme = parsed.scheme or "http"
    auth = ""
    if parsed.username:
        user = parsed.username
        if len(user) > 24:
            user = user[:20] + "..."
        auth = "%s:***@" % user
    return "%s://%s%s%s" % (scheme, auth, host, port)


def diagnose_egress(logger=None, timeout: float = 6.0) -> dict:
    """通过当前展开后的代理请求 ipinfo.io，返回 ASN/国家/hosting 布尔。"""
    def log(message: str) -> None:
        if logger:
            try:
                logger(message)
            except Exception:
                pass

    cfg = _load_config()
    fallback = str(cfg.get("proxy", "") or "").strip()
    expanded = expand_proxy(fallback)
    log("[*] 出口自检代理: %s" % _safe_label(expanded))
    try:
        # curl_cffi 的 timeout 在某些平台不生效；额外用 threading.Timer 兜底
        import threading as _threading
        state: dict = {"resp": None, "err": None}

        def _fetch():
            try:
                if expanded:
                    state["resp"] = requests.get(
                        "https://ipinfo.io/json",
                        timeout=timeout,
                        proxies={"http": expanded, "https": expanded},
                    )
                else:
                    state["resp"] = requests.get("https://ipinfo.io/json", timeout=timeout)
            except Exception as inner:
                state["err"] = inner

        thread = _threading.Thread(target=_fetch, daemon=True)
        thread.start()
        thread.join(timeout + 2)
        if thread.is_alive():
            log("[!] 出口自检超时（%ss），已放弃" % timeout)
            return {"ok": False, "error": "timeout"}
        if state["err"] is not None:
            raise state["err"]
        resp = state["resp"]
        data = json.loads((resp.text if resp is not None else "") or "{}")
    except Exception as exc:
        log("[!] 出口自检失败: %s" % exc)
        return {"ok": False, "error": str(exc)}
    org = str(data.get("org", "") or "")
    ip = str(data.get("ip", "") or "")
    country = str(data.get("country", "") or "")
    city = str(data.get("city", "") or "")
    hosting = any(marker in org.lower() for marker in ("cloud", "hosting", "server", "data center", "datacenter", "ovh", "vultr", "linode", "digitalocean", "cloudflare", "aws", "amazon", "google", "microsoft", "azure", "leaseweb", "hetzner", "colocation"))
    log("[*] 出口 IP: %s (%s / %s) org=%s hosting=%s" % (ip, country, city, org, hosting))
    if hosting:
        log("[!] 当前出口 IP 属于数据中心/托管 ASN，xAI 会大概率打 bot_flag。请换真住宅出口。")
    return {"ok": True, "ip": ip, "country": country, "city": city, "org": org, "hosting": hosting}


def is_placeholder_configured() -> bool:
    """判断当前号的实际代理会不会随 rotate_session 变化（决定是否需要每号轮转）。"""
    cfg = _load_config()
    pool = _pool_entries()
    if pool:
        # 池非空：sticky 策略下始终同一个（无需轮转），其他策略需要轮转
        return _pool_strategy() != "sticky" or any(_has_placeholder(entry) for entry in pool)
    raw = str(cfg.get("proxy", "") or "").strip()
    return _has_placeholder(raw)


def pool_summary() -> dict:
    """给日志/GUI 展示用。"""
    pool = _pool_entries()
    return {
        "size": len(pool),
        "strategy": _pool_strategy() if pool else "",
        "current_index": _current_pool_index if pool else -1,
    }
