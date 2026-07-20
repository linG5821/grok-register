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
import socket
import threading
import time
import urllib.parse
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed

from curl_cffi import requests


# RLock：rotate → _sync_environment → expand → _ensure_session_id 会重入
_lock = threading.RLock()
_current_session_id: str = ""
_current_pool_url: str = ""     # 当前号选中的池条目原文（含占位符）
_current_pool_index: int = -1   # round_robin 游标
_last_expanded: str = ""
_placeholder_tokens = ("{rand}", "{RAND}", "{session}", "{SESSION}")

_VALID_STRATEGIES = ("round_robin", "random", "sticky")

# --- 代理健康缓存（TCP 探活）---
_health_lock = threading.RLock()
_available: deque[str] = deque()
_dead: set[str] = set()
_dead_since: dict[str, float] = {}
_probed: set[str] = set()
_scanning = False
_full_pass_done = False
_scan_thread: threading.Thread | None = None
_health_logger = None
_DEAD_COOLDOWN_SEC = 60.0

# 账号级代理绑定：后处理真活探测需用注册时出口，而非 rotate 后的节点
_account_proxy_lock = threading.RLock()
_account_proxy_by_email: dict[str, str] = {}


class NoAvailableProxyError(RuntimeError):
    """已配置代理但当前无可用节点（全池探完仍空，或单代理探活失败）。"""


class ProxyWaitCancelled(Exception):
    """等待可用代理时用户取消。"""


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


def _pop_available_url() -> str:
    """从健康缓存取出一个非 dead URL；空则返回 ""。调用方须已持有或不依赖 _lock。"""
    with _health_lock:
        while _available:
            cand = _available.popleft()
            if cand in _dead:
                continue
            return cand
    return ""


def _pick_from_pool_skip_dead() -> str:
    """从配置池挑选，跳过 health dead；全死则返回 ""。"""
    pool = _pool_entries()
    if not pool:
        return ""
    with _health_lock:
        dead_snapshot = set(_dead)
    # 最多转一圈
    for _ in range(len(pool)):
        cand = _pick_from_pool()
        if cand and cand not in dead_snapshot:
            return cand
    return ""


def rotate_session(reason: str = "") -> str:
    """换号时调用：换一个 rand、并从池里挑一个新条目（sticky 除外）。

    健康缓存启用时：优先 available；available 空则跳过 dead 再挑；
    仍无节点时若当前已 dead 则清空（不绑定已知 dead）。
    返回新的 session id，方便调用方 log。
    """
    global _current_session_id, _current_pool_url
    with _lock:
        _current_session_id = _new_session_id()
        session_id = _current_session_id
        pool = list(_pool_entries())
        if not pool:
            _current_pool_url = ""
        elif proxy_health_should_run():
            # 不在 _lock 内 pop health（避免与扫线程长时间互等）；先记下 pool
            pass
        else:
            _current_pool_url = _pick_from_pool()

    if pool and proxy_health_should_run():
        picked = _pop_available_url()
        if not picked:
            picked = _pick_from_pool_skip_dead()
        with _lock:
            if picked:
                _current_pool_url = picked
                try:
                    _current_pool_index = pool.index(picked)
                except ValueError:
                    pass
            else:
                start_background_scan()
                with _health_lock:
                    cur = _current_pool_url
                    if cur and cur in _dead:
                        _current_pool_url = ""

    _sync_environment()
    if pool and proxy_health_should_run() and available_count() < _refill_threshold():
        start_background_scan()
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
    - 池非空且尚未 rotate（无 session）时自动 rotate 一次
    - 若含占位符但尚未 rotate，会自动 rotate 一次保证结果稳定
    - 已 rotate 仍无 active（如全 dead）→ 返回 ""，禁止再 rotate 防递归
    """
    global _last_expanded
    fallback = str(raw or "").strip()
    active = _resolve_active_proxy(fallback)
    if not active and _pool_entries() and not _ensure_session_id():
        rotate_session("auto-init-pool")
        active = _resolve_active_proxy(fallback)
    if not active:
        _last_expanded = ""
        return ""
    if _has_placeholder(active):
        session_id = _ensure_session_id()
        if not session_id:
            rotate_session("auto-init")
            session_id = _ensure_session_id()
            active = _resolve_active_proxy(fallback) or active
        if not session_id:
            _last_expanded = ""
            return ""
        for token in _placeholder_tokens:
            active = active.replace(token, session_id)
    _last_expanded = active
    return active


def last_expanded_proxy() -> str:
    return _last_expanded


def remember_proxy_for_account(email: str, proxy_url: str = "") -> str:
    """记录某邮箱注册时使用的展开代理 URL；空 proxy_url 时取 last_expanded/当前池。"""
    key = str(email or "").strip().lower()
    if not key:
        return ""
    raw = str(proxy_url or "").strip()
    if not raw:
        raw = str(_last_expanded or "").strip()
    if not raw:
        try:
            raw = str(expand_proxy(_load_config().get("proxy", "") or "") or "").strip()
        except Exception:
            raw = ""
    with _account_proxy_lock:
        if raw:
            _account_proxy_by_email[key] = raw
        return _account_proxy_by_email.get(key, "")


def get_proxy_for_account(email: str) -> str:
    """优先返回账号绑定代理，否则 last_expanded。"""
    key = str(email or "").strip().lower()
    with _account_proxy_lock:
        if key and key in _account_proxy_by_email:
            return _account_proxy_by_email[key]
    return str(_last_expanded or "").strip()


def clear_account_proxy_bindings():
    with _account_proxy_lock:
        _account_proxy_by_email.clear()


_PROXY_ENV_KEYS = (
    "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
    "ALL_PROXY", "all_proxy",
)
_NO_PROXY_VALUE = "localhost,127.0.0.1,::1,.local"


_syncing_env = False


def _sync_environment() -> None:
    """把当前号选中并展开后的代理写入 HTTPS_PROXY/HTTP_PROXY。

    同时写 NO_PROXY，避免 curl 等把本机请求也走代理。
    注意：Chromium 启动前仍须临时清掉这些变量（见 browser 启动路径），
    否则 Windows 上 Chrome 会继承 HTTP_PROXY 导致 CDP 失败。
    重入保护：expand→rotate→sync 链路上禁止二次 expand 递归。
    """
    global _syncing_env
    if _syncing_env:
        return
    _syncing_env = True
    try:
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
    finally:
        _syncing_env = False


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


def probe_proxy_endpoint(proxy_url: str, timeout: float = 2.0) -> bool:
    """TCP 探活代理 host:port。空 URL 视为直连可用（返回 True）。

    仅检查 TCP 可达，不做 HTTP CONNECT。
    """
    raw = str(proxy_url or "").strip()
    if not raw:
        return True
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parsed = urllib.parse.urlsplit(raw)
    except Exception:
        return False
    host = parsed.hostname
    if not host:
        return False
    try:
        port = parsed.port
    except Exception:
        port = None
    if not port:
        scheme = (parsed.scheme or "http").lower()
        port = 443 if scheme == "https" else 80
    try:
        with socket.create_connection((host, int(port)), timeout=float(timeout)):
            return True
    except Exception:
        return False


def _health_check_url() -> str:
    cfg = _load_config()
    url = str(cfg.get("proxy_health_check_url") or "").strip()
    return url or "https://accounts.x.ai/"


def probe_proxy_grok_access(
    proxy_url: str,
    timeout: float | None = None,
    check_url: str | None = None,
) -> bool:
    """经代理访问 Grok/xAI 域名，确认隧道可用。

    能拿到任意 HTTP 响应（含 403/CF）即视为可达；连接失败/超时为不可用。
    空 proxy_url 表示直连探测。
    """
    if timeout is None:
        timeout = _cfg_float("proxy_health_http_timeout", 6.0)
    timeout = max(1.0, float(timeout))
    target = str(check_url or _health_check_url()).strip() or "https://accounts.x.ai/"
    raw = str(proxy_url or "").strip()
    state: dict = {"ok": False, "err": None}

    def _fetch():
        try:
            kwargs = {"timeout": timeout, "allow_redirects": True}
            if raw:
                kwargs["proxies"] = {"http": raw, "https": raw}
            # impersonate 提高过简单 bot 门的几率；失败则普通请求
            try:
                resp = requests.get(target, impersonate="chrome131", **kwargs)
            except TypeError:
                resp = requests.get(target, **kwargs)
            except Exception:
                resp = requests.get(target, timeout=timeout, proxies=kwargs.get("proxies"))
            # 有状态码即说明代理隧道/网络通到目标
            code = int(getattr(resp, "status_code", 0) or 0)
            state["ok"] = code > 0
        except Exception as exc:
            state["err"] = exc
            state["ok"] = False

    thread = threading.Thread(target=_fetch, daemon=True)
    thread.start()
    thread.join(timeout + 2.0)
    if thread.is_alive():
        return False
    return bool(state["ok"])


def probe_proxy_usable(
    proxy_url: str,
    tcp_timeout: float | None = None,
    http_timeout: float | None = None,
) -> bool:
    """完整探活：TCP 通 + 经代理能访问 Grok 域名。

    空 URL（直连）只做 Grok 探测。
    """
    raw = str(proxy_url or "").strip()
    if tcp_timeout is None:
        tcp_timeout = _cfg_float("proxy_health_tcp_timeout", 2.0)
    if http_timeout is None:
        http_timeout = _cfg_float("proxy_health_http_timeout", 6.0)
    if raw and not probe_proxy_endpoint(raw, timeout=float(tcp_timeout)):
        return False
    return probe_proxy_grok_access(raw, timeout=float(http_timeout))


def _cfg_float(key: str, default: float) -> float:
    try:
        return float(_load_config().get(key, default) or default)
    except Exception:
        return float(default)


def _cfg_int(key: str, default: int) -> int:
    try:
        return int(_load_config().get(key, default) or default)
    except Exception:
        return int(default)


def _refill_threshold() -> int:
    return max(1, _cfg_int("proxy_health_refill_threshold", 5))


def proxy_health_should_run() -> bool:
    """是否启用健康探活/门禁。未配置 proxy 且无 pool → False（直连）。"""
    cfg = _load_config()
    if cfg.get("proxy_health_enabled") is False:
        return False
    if _pool_entries():
        return True
    return bool(str(cfg.get("proxy", "") or "").strip())


def _health_log(message: str) -> None:
    logger = _health_logger
    if logger:
        try:
            logger(message)
        except Exception:
            pass


def _candidate_urls() -> list[str]:
    pool = _pool_entries()
    if pool:
        return list(pool)
    cfg = _load_config()
    single = str(cfg.get("proxy", "") or "").strip()
    return [single] if single else []


def _mark_alive_locked(url: str) -> None:
    _dead.discard(url)
    _dead_since.pop(url, None)
    _probed.add(url)
    if url not in _available:
        _available.append(url)


def _mark_dead_locked(url: str) -> None:
    if not url:
        return
    _probed.add(url)
    _dead.add(url)
    _dead_since[url] = time.time()
    try:
        while url in _available:
            _available.remove(url)
    except ValueError:
        pass


def mark_proxy_dead(url: str | None = None, reason: str = "") -> None:
    """将代理标为不可用（开页失败/探活失败）。url 默认当前池条目。"""
    target = str(url or current_pool_url() or last_expanded_proxy() or "").strip()
    if not target:
        return
    # 同时标当前池原文与传入 URL（expanded 可能与原文不同）
    raw_current = current_pool_url()
    with _health_lock:
        _mark_dead_locked(target)
        if raw_current and raw_current != target:
            _mark_dead_locked(raw_current)
    if reason:
        _health_log("[!] 代理标 dead (%s): %s" % (reason, _safe_label(target)))


def available_count() -> int:
    with _health_lock:
        return len(_available)


def list_available_proxies() -> list[str]:
    """非破坏性快照当前健康缓存中的代理 URL。"""
    with _health_lock:
        return list(_available)


def _expand_pool_entry_literal(entry: str) -> str:
    """展开条目中的 {rand} 占位符，不走 expand_proxy（避免被当前池锁定项劫持）。"""
    text = str(entry or "").strip()
    if not text:
        return ""
    if _has_placeholder(text):
        sid = _ensure_session_id() or _new_session_id()
        for token in _placeholder_tokens:
            text = text.replace(token, sid)
    return text


def list_proxy_candidates(limit: int = 5, exclude: set[str] | None = None) -> list[str]:
    """选出最多 limit 个待测代理：先 available，再 pool 中未 dead/未 exclude 的条目。"""
    cap = max(int(limit or 0), 0)
    if cap <= 0:
        return []
    blocked = {str(x or "").strip() for x in (exclude or set()) if str(x or "").strip()}
    out: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> bool:
        expanded = _expand_pool_entry_literal(raw)
        if not expanded or expanded in blocked or expanded in seen:
            return False
        seen.add(expanded)
        out.append(expanded)
        return len(out) >= cap

    for url in list_available_proxies():
        if _add(url):
            return out

    for entry in _pool_entries():
        with _health_lock:
            if entry in _dead:
                continue
        if _add(entry):
            return out

    if not out:
        cfg = _load_config()
        single = str(cfg.get("proxy") or "").strip()
        if single:
            _add(single)
    return out[:cap]


def scan_status() -> dict:
    with _health_lock:
        return {
            "scanning": _scanning,
            "full_pass_done": _full_pass_done,
            "probed": len(_probed),
            "pool_size": len(_candidate_urls()),
            "available": len(_available),
            "dead": len(_dead),
            "enabled": proxy_health_should_run(),
        }


def reset_proxy_health_state() -> None:
    """测试用：清空健康缓存状态。"""
    global _scanning, _full_pass_done, _scan_thread, _health_logger
    with _health_lock:
        _available.clear()
        _dead.clear()
        _dead_since.clear()
        _probed.clear()
        _scanning = False
        _full_pass_done = False
        _scan_thread = None
    _health_logger = None


def start_background_scan(log=None, concurrency: int | None = None) -> None:
    """异步 TCP 探活；无代理配置时 no-op。幂等。"""
    global _health_logger, _scanning, _scan_thread, _full_pass_done
    if log is not None:
        _health_logger = log
    if not proxy_health_should_run():
        return
    with _health_lock:
        if _scanning:
            return
        _scanning = True
        _full_pass_done = False

    workers = concurrency if concurrency is not None else _cfg_int("proxy_health_concurrency", 8)
    workers = max(1, min(int(workers), 32))
    tcp_timeout = _cfg_float("proxy_health_tcp_timeout", 2.0)

    def _run():
        global _scanning, _full_pass_done
        mark_full_pass = True
        try:
            candidates = _candidate_urls()
            if not candidates:
                return
            now = time.time()
            to_probe = []
            with _health_lock:
                for url in candidates:
                    if url in _available:
                        continue
                    if url in _dead:
                        since = _dead_since.get(url, 0)
                        if now - since < _DEAD_COOLDOWN_SEC:
                            continue
                    to_probe.append(url)
            if not to_probe:
                # 仅冷却导致无可探：不要标 full_pass_done，否则 wait 会立刻判死
                with _health_lock:
                    only_cooldown = (
                        len(_available) == 0
                        and len(_dead) > 0
                        and any(
                            (time.time() - _dead_since.get(u, 0)) < _DEAD_COOLDOWN_SEC
                            for u in _dead
                        )
                    )
                if only_cooldown:
                    _health_log("[*] 代理探活：节点在冷却中，稍后重试")
                    mark_full_pass = False
                    return
                _health_log("[*] 代理探活：无需探测（缓存仍有可用）")
                return
            http_timeout = _cfg_float("proxy_health_http_timeout", 6.0)
            _health_log(
                "[*] 开始代理探活(TCP+Grok): %s 条, 并发=%s check=%s"
                % (len(to_probe), workers, _health_check_url())
            )
            alive = 0
            dead = 0

            def _one(url: str):
                # 占位符 URL：用临时 session 展开再探
                expanded = url
                if _has_placeholder(url):
                    sid = _new_session_id()
                    expanded = url
                    for token in _placeholder_tokens:
                        expanded = expanded.replace(token, sid)
                ok = probe_proxy_usable(
                    expanded,
                    tcp_timeout=tcp_timeout,
                    http_timeout=http_timeout,
                )
                return url, ok

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(_one, u) for u in to_probe]
                for fut in as_completed(futures):
                    try:
                        url, ok = fut.result()
                    except Exception:
                        continue
                    with _health_lock:
                        if ok:
                            _mark_alive_locked(url)
                            alive += 1
                        else:
                            _mark_dead_locked(url)
                            dead += 1
                    if (alive + dead) % 20 == 0 or (alive + dead) == len(to_probe):
                        st = scan_status()
                        _health_log(
                            "[*] 代理探活进度: 已处理 %s/%s 可用=%s dead+=%s"
                            % (alive + dead, len(to_probe), st["available"], dead)
                        )
            _health_log(
                "[*] 代理探活本轮完成: 新可用=%s 新dead=%s 当前可用=%s"
                % (alive, dead, available_count())
            )
        finally:
            with _health_lock:
                _scanning = False
                if mark_full_pass:
                    _full_pass_done = True
                _scan_thread = None

    t = threading.Thread(target=_run, name="proxy-health-scan", daemon=True)
    with _health_lock:
        _scan_thread = t
    t.start()


def wait_until_available(cancel_callback=None, log=None, poll: float = 0.5) -> str:
    """阻塞直到至少 1 个可用代理；未配置代理时返回 ""。

    full_pass_done 且仍空 → NoAvailableProxyError。
    cancel_callback() 为真 → ProxyWaitCancelled。
    """
    if log is not None:
        global _health_logger
        _health_logger = log

    def _log(msg: str) -> None:
        if log:
            try:
                log(msg)
            except Exception:
                pass

    if not proxy_health_should_run():
        return ""

    start_background_scan(log=log)
    last_log = 0.0
    while True:
        if cancel_callback is not None:
            try:
                if cancel_callback():
                    raise ProxyWaitCancelled()
            except ProxyWaitCancelled:
                raise
            except Exception:
                pass
        with _health_lock:
            if _available:
                return _available[0]
            scanning = _scanning
            done = _full_pass_done
            probed = len(_probed)
            size = len(_candidate_urls())
            avail = len(_available)
        if done and not scanning and avail == 0:
            raise NoAvailableProxyError(
                "全池探活无可用代理（已探 %s/%s）" % (probed, size)
            )
        now = time.time()
        if now - last_log >= 5.0:
            _log(
                "[*] 等待可用代理… 已探 %s/%s 可用 %s scanning=%s"
                % (probed, size, avail, scanning)
            )
            last_log = now
        if not scanning and not done:
            start_background_scan(log=log)
        time.sleep(max(0.1, float(poll)))


def acquire_healthy_proxy(
    reason: str = "",
    wait_sec: float = 0,
    cancel_callback=None,
    poll: float = 0.3,
) -> str:
    """取出一个健康池条目并设为当前代理；成功返回 session_id。

    未配置代理时返回 ""。
    wait_sec>0：缓存空且仍在扫/冷却时短等，再 pop；超时或 full_pass 仍空 →
    NoAvailableProxyError。cancel_callback 为真 → ProxyWaitCancelled。
    """
    if not proxy_health_should_run():
        return ""

    deadline = time.time() + max(0.0, float(wait_sec or 0))
    while True:
        if cancel_callback is not None:
            try:
                if cancel_callback():
                    raise ProxyWaitCancelled()
            except ProxyWaitCancelled:
                raise
            except Exception:
                pass

        with _health_lock:
            url = ""
            while _available:
                cand = _available.popleft()
                if cand in _dead:
                    continue
                url = cand
                break
            scanning = _scanning
            done = _full_pass_done

        if url:
            global _current_session_id, _current_pool_url, _current_pool_index
            with _lock:
                _current_session_id = _new_session_id()
                _current_pool_url = url
                pool = _pool_entries()
                try:
                    _current_pool_index = pool.index(url) if pool else -1
                except ValueError:
                    _current_pool_index = -1
                session_id = _current_session_id
            _sync_environment()
            if available_count() < _refill_threshold():
                start_background_scan()
            return session_id

        # 无货
        if done and not scanning:
            start_background_scan()
            raise NoAvailableProxyError("无可用代理（缓存为空且探活已结束）")

        start_background_scan()
        if time.time() >= deadline:
            raise NoAvailableProxyError(
                "暂无可用代理（已等待 %.1fs，探活进行中或未完成）" % float(wait_sec or 0)
            )
        time.sleep(max(0.05, float(poll)))
