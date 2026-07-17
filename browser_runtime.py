"""提供共享的 HTTP 请求、代理处理和 Chromium 启动参数。"""
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
    """返回当前号应使用的代理 URL。

    proxy_pool 非空时由 proxy_manager.expand_proxy 选池条目；
    池为空时回退 config.proxy。两者皆空返回 ""。
    """
    raw = str(_config.get("proxy", "") or "").strip()
    try:
        from proxy_manager import expand_proxy
        return expand_proxy(raw)
    except Exception:
        return raw


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
    def logger(message):
        if not log_callback:
            return
        if "proxy bridge" in message:
            log_callback("[*] 已为 Chromium 启动本地代理桥: %s" % message.split(": ", 1)[-1])
        else:
            log_callback(message)
    return prepare_chromium_proxy(proxy, log=logger)


def apply_browser_proxy_option(options, proxy):
    """给 Chromium 设代理。

    Windows 实测：``--proxy-server`` 会卡 CDP；手测可用的是 PAC：
    localhost 走 DIRECT，其余走 PROXY host:port（本机桥）。
    """
    if not proxy:
        return
    raw = str(proxy or "").strip()
    parsed = _parse_proxy_url(raw)
    if not parsed or not parsed.hostname:
        return
    try:
        port = parsed.port
    except Exception:
        port = None
    if not port:
        port = 443 if (parsed.scheme or "http").lower() == "https" else 80
    host = parsed.hostname
    if not hasattr(options, "set_argument"):
        if hasattr(options, "set_proxy") and not raw.lower().startswith("socks"):
            options.set_proxy(raw)
        return
    pac = (
        "function FindProxyForURL(url, host) {"
        "  if (host == '127.0.0.1' || host == 'localhost' || host == '::1'"
        "      || host == '[::1]' || shExpMatch(host, '*.local')) {"
        "    return 'DIRECT';"
        "  }"
        "  return 'PROXY %s:%s';"
        "}"
    ) % (host, port)
    pac_url = "data:application/x-ns-proxy-autoconfig," + urllib.parse.quote(pac)
    try:
        options.set_argument("--proxy-pac-url", pac_url)
    except TypeError:
        options.set_argument("--proxy-pac-url=%s" % pac_url)


def create_browser_options(browser_proxy="", extension_path=None):
    options = ChromiumOptions()
    options.auto_port()
    try:
        options.set_timeouts(base=10, page_load=60, script=30)
    except TypeError:
        options.set_timeouts(base=10)
    apply_browser_proxy_option(options, browser_proxy)
    effective_extension = _extension_path if extension_path is None else str(extension_path or "")
    if effective_extension and os.path.exists(effective_extension):
        options.add_extension(effective_extension)
    return options


def remote_import_use_proxy():
    """远程入池/chenyme 导入是否走注册代理；默认 False（直连）。"""
    try:
        return bool(_config.get("remote_import_use_proxy", False))
    except Exception:
        return False


def mail_use_proxy():
    """邮箱 API 是否走注册代理；默认 False（与历史行为一致）。"""
    try:
        return bool(_config.get("mail_use_proxy", False))
    except Exception:
        return False


def _build_request_kwargs(**kwargs):
    request_kwargs = dict(kwargs)
    force_direct = bool(request_kwargs.pop("force_direct", False))
    proxies = request_kwargs.pop("proxies", None)
    if force_direct:
        # 显式空代理，避免 curl_cffi 再读 HTTP_PROXY
        request_kwargs["proxies"] = {}
    else:
        if proxies is None:
            proxies = get_proxies()
        if proxies:
            request_kwargs["proxies"] = proxies
    request_kwargs.setdefault("timeout", 15)
    return request_kwargs, force_direct


def _with_optional_cleared_proxy_env(force_direct, func):
    saved = {}
    if force_direct:
        try:
            from proxy_manager import clear_proxy_environment, restore_proxy_environment
            saved = clear_proxy_environment()
        except Exception:
            saved = {}
    try:
        return func()
    finally:
        if force_direct and saved:
            try:
                from proxy_manager import restore_proxy_environment
                restore_proxy_environment(saved)
            except Exception:
                pass


def http_get(url, **kwargs):
    request_kwargs, force_direct = _build_request_kwargs(**kwargs)

    def _do():
        try:
            return requests.get(url, **request_kwargs)
        except Exception as exc:
            if (not force_direct) and is_proxy_connection_error(exc):
                direct = dict(request_kwargs)
                direct.pop("proxies", None)
                return requests.get(url, **direct)
            raise

    return _with_optional_cleared_proxy_env(force_direct, _do)


def http_post(url, **kwargs):
    request_kwargs, force_direct = _build_request_kwargs(**kwargs)

    def _do():
        try:
            return requests.post(url, **request_kwargs)
        except Exception as exc:
            if (not force_direct) and is_proxy_connection_error(exc):
                direct = dict(request_kwargs)
                direct.pop("proxies", None)
                return requests.post(url, **direct)
            raise

    return _with_optional_cleared_proxy_env(force_direct, _do)


def remote_import_http_get(url, **kwargs):
    kwargs.setdefault("force_direct", not remote_import_use_proxy())
    return http_get(url, **kwargs)


def remote_import_http_post(url, **kwargs):
    kwargs.setdefault("force_direct", not remote_import_use_proxy())
    return http_post(url, **kwargs)


def mail_http_get(url, **kwargs):
    kwargs.setdefault("force_direct", not mail_use_proxy())
    return http_get(url, **kwargs)


def mail_http_post(url, **kwargs):
    kwargs.setdefault("force_direct", not mail_use_proxy())
    return http_post(url, **kwargs)
