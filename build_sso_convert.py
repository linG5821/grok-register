"""chenyme sso_build.go 复刻：SSO cookie -> Device Flow -> Build OAuth token。"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

from cpa_xai.oauth_device import CLIENT_ID, _build_opener, _is_transient_net_error


SSO_SCOPE = " ".join([
    "openid", "profile", "email", "offline_access",
    "grok-cli:access", "api:access",
    "conversations:read", "conversations:write",
])
ACCOUNTS_URL = "https://accounts.x.ai/"
DEVICE_CODE_URL = "https://auth.x.ai/oauth2/device/code"
VERIFY_URL = "https://auth.x.ai/oauth2/device/verify"
APPROVE_URL = "https://auth.x.ai/oauth2/device/approve"
TOKEN_URL = "https://auth.x.ai/oauth2/token"
# chenyme: min(expires_in, 75s)。approve 成功后 token 应立刻可用，长 poll 无意义。
POLL_MAX_SEC = 75


class SSOConvertError(RuntimeError):
    def __init__(self, message: str, *, permanent: bool = False, code: str = "", http_status: int = 0):
        super().__init__(message)
        self.permanent = bool(permanent)
        self.code = str(code or "")
        self.http_status = int(http_status or 0)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止跟随 302，让调用方能读到 Location（判定 sso_expired）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


# 有效 SSO 访问 accounts.x.ai 会 3xx 跳进 app；只有跳到这些路径才是真过期。
_SIGNIN_MARKERS = ("sign-in", "signin", "/login", "log-in", "/auth/")


def _is_signin_location(location: str) -> bool:
    loc = str(location or "").lower()
    return any(marker in loc for marker in _SIGNIN_MARKERS)


def _raise_for_accounts_httperror(exc: urllib.error.HTTPError) -> None:
    """分类 accounts.x.ai 的 HTTPError。

    3xx 且 Location 跳 sign-in → sso_expired（permanent）。
    3xx 其它 → 登录态有效，视为成功（return，不抛）。
    其它状态码 → 网络/服务端错误。
    """
    location = str(exc.headers.get("Location") or "")
    if exc.code in (301, 302, 303, 307, 308):
        if _is_signin_location(location):
            raise SSOConvertError(
                "sso expired: redirect to sign-in",
                permanent=True,
                code="sso_expired",
                http_status=exc.code,
            )
        return  # 有效 SSO：已登录，跳转进 app
    raise SSOConvertError(
        f"accounts.x.ai HTTP {exc.code}",
        code=f"accounts_http_{exc.code}",
        http_status=exc.code,
    )


def _cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v)


def _get_with_cookies(
    url: str,
    cookies: dict[str, str],
    proxy: str = "",
    timeout: float = 30,
    *,
    allow_redirects: bool = True,
) -> Any:
    """带 cookie 的 GET。allow_redirects=False 时 3xx 以 HTTPError 抛出。"""
    request = urllib.request.Request(
        url,
        headers={
            "Cookie": _cookie_header(cookies),
            "User-Agent": "grok-register-sso/1.0",
            "Accept": "text/html,application/json",
        },
    )
    handlers: list[Any] = []
    if not allow_redirects:
        handlers.append(_NoRedirect())
    if proxy:
        # 与 oauth_device._build_opener 一致：http/https 都走同一代理
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers) if handlers else urllib.request.build_opener()
    return opener.open(request, timeout=timeout)


def _post_form_with_cookies(
    url: str,
    form: dict[str, Any],
    cookies: Optional[dict[str, str]] = None,
    *,
    timeout: float = 30.0,
    proxy: str = "",
    retries: int = 0,
) -> tuple[int, Any]:
    """POST application/x-www-form-urlencoded，可选 Cookie（verify/approve 必须带）。"""
    data = urllib.parse.urlencode({k: v for k, v in (form or {}).items()}).encode("utf-8")
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json, text/html, */*",
        "User-Agent": "grok-register-sso/1.0",
    }
    if cookies:
        headers["Cookie"] = _cookie_header(cookies)
    request = urllib.request.Request(url, data=data, method="POST", headers=headers)
    last_error: Optional[BaseException] = None
    for attempt in range(max(int(retries), 0) + 1):
        opener = _build_opener(proxy or None)
        try:
            with opener.open(request, timeout=float(timeout)) as response:
                body = response.read().decode("utf-8", errors="replace")
                status = int(getattr(response, "status", 200) or 200)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            status = int(exc.code)
        except Exception as exc:
            last_error = exc
            if not _is_transient_net_error(exc) or attempt >= int(retries):
                raise
            time.sleep(1.0 * (attempt + 1))
            continue
        try:
            return status, json.loads(body)
        except Exception:
            return status, body
    if last_error is not None:
        raise last_error
    raise SSOConvertError("form request failed without response", code="post_empty")


def _poll_device_token(
    device_code: str,
    client_id: str,
    token_url: str,
    interval: int,
    expires_in: int,
    proxy: str = "",
    cookies: Optional[dict[str, str]] = None,
    log: Optional[Callable[[str], None]] = None,
    post_form: Optional[Callable[..., Any]] = None,
) -> dict:
    # approve 后 token 应立刻下发；硬顶 POLL_MAX_SEC，避免 authorization_pending 死等 30 分钟
    poll_budget = min(max(int(expires_in) - 5, 15), POLL_MAX_SEC)
    deadline = time.time() + poll_budget
    sleep_sec = max(int(interval), 1)
    do_post = post_form or (
        lambda url, form, **kw: _post_form_with_cookies(url, form, cookies=cookies, **kw)
    )
    while time.time() < deadline:
        try:
            status, payload = do_post(
                token_url,
                {
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": str(device_code).strip(),
                    "client_id": client_id,
                },
                timeout=30,
                proxy=proxy,
                retries=0,
            )
            if status == 200 and isinstance(payload, dict) and payload.get("access_token"):
                return {
                    "access_token": str(payload.get("access_token") or "").strip(),
                    "refresh_token": str(payload.get("refresh_token") or "").strip(),
                    "id_token": str(payload.get("id_token") or "").strip(),
                    "expires_in": int(payload.get("expires_in") or 0),
                    "raw": payload,
                }
            error = str(payload.get("error") or "").lower() if isinstance(payload, dict) else ""
            if error in ("authorization_pending", "slow_down"):
                time.sleep(sleep_sec if error == "authorization_pending" else sleep_sec + 5)
                continue
            if error in ("access_denied", "expired_token", "invalid_grant"):
                raise SSOConvertError(
                    f"device token rejected: {error}",
                    permanent=True,
                    code=error,
                    http_status=status,
                )
            raise SSOConvertError(
                f"device token poll failed HTTP {status}",
                code=f"poll_http_{status}",
                http_status=status,
            )
        except SSOConvertError:
            raise
        except Exception as exc:
            if log:
                log(f"poll retry: {exc}")
            time.sleep(sleep_sec)
    raise SSOConvertError("device flow poll timeout", permanent=False, code="poll_timeout")


def sso_to_build(
    sso_token: str,
    proxy: str = "",
    timeout: float = 90,
    log: Optional[Callable[[str], None]] = None,
    *,
    _mock_get: Optional[Callable[..., Any]] = None,
    _mock_post: Optional[Callable[..., Any]] = None,
) -> dict:
    sso = str(sso_token or "").strip()
    if not sso:
        raise SSOConvertError("missing sso token", permanent=True, code="missing_sso")
    if sso.lower().startswith("sso="):
        sso = sso[4:].strip()

    cookies = {"sso": sso, "sso-rw": sso}
    do_get = _mock_get or _get_with_cookies

    def do_post(url: str, form: dict, **kwargs: Any) -> tuple[int, Any]:
        if _mock_post is not None:
            return _mock_post(url, form, cookies=cookies, **kwargs)
        return _post_form_with_cookies(url, form, cookies=cookies, **kwargs)

    # Step 1: 校验 SSO（禁止跟随 3xx，才能读 Location 判断是否跳 sign-in）
    try:
        if log:
            log(f"[SSO] 1/6 verify accounts.x.ai {proxy}")
        _ = do_get(ACCOUNTS_URL, cookies, proxy=proxy, timeout=timeout, allow_redirects=False)
    except TypeError:
        # mock 可能不接受 allow_redirects
        try:
            _ = do_get(ACCOUNTS_URL, cookies, proxy=proxy, timeout=timeout)
        except urllib.error.HTTPError as exc:
            _raise_for_accounts_httperror(exc)
        except Exception as exc:
            raise SSOConvertError(f"accounts.x.ai failed: {exc}", code="accounts_net_error") from exc
    except urllib.error.HTTPError as exc:
        _raise_for_accounts_httperror(exc)
    except Exception as exc:
        raise SSOConvertError(f"accounts.x.ai failed: {exc}", code="accounts_net_error") from exc

    # Step 2: Request device code（公开端点，cookie 可带可不带）
    try:
        if log:
            log("[SSO] 2/6 request device code")
        status, payload = do_post(
            DEVICE_CODE_URL,
            {"client_id": CLIENT_ID, "scope": SSO_SCOPE},
            proxy=proxy,
            timeout=30,
            retries=1,
        )
    except Exception as exc:
        raise SSOConvertError(f"device code request failed: {exc}", code="device_code_request_failed") from exc
    if status != 200 or not isinstance(payload, dict):
        raise SSOConvertError(
            f"device code HTTP {status}",
            code=f"device_code_http_{status}",
            http_status=status,
        )

    device_code = str(payload.get("device_code") or "").strip()
    user_code = str(payload.get("user_code") or "").strip()
    verify_uri = str(payload.get("verification_uri_complete") or "").strip()
    interval = max(int(payload.get("interval") or 5), 1)
    expires_in = max(int(payload.get("expires_in") or 1800), 60)
    if not device_code or not user_code:
        raise SSOConvertError("device code response missing fields", permanent=False, code="device_code_incomplete")

    # Step 3: GET verification_uri_complete（带 SSO cookie）
    try:
        if log:
            log("[SSO] 3/6 open verification uri")
        _ = do_get(verify_uri, cookies, proxy=proxy, timeout=30)
    except TypeError:
        _ = do_get(verify_uri, cookies, proxy=proxy, timeout=30)
    except Exception as exc:
        raise SSOConvertError(f"open verification uri failed: {exc}", code="verify_uri_failed") from exc

    # Step 4: POST device/verify —— 必须带 Cookie，否则服务端无会话
    try:
        if log:
            log("[SSO] 4/6 device verify")
        status, _ = do_post(VERIFY_URL, {"user_code": user_code}, proxy=proxy, timeout=30)
        # 200 / 302 consent 都算 OK；401 = 未认证
        if status == 401:
            raise SSOConvertError(
                "device verify HTTP 401 (SSO cookie rejected)",
                permanent=True,
                code="verify_unauthorized",
                http_status=401,
            )
        if status not in (200, 204, 302, 303) and status >= 400:
            raise SSOConvertError(
                f"device verify HTTP {status}",
                code=f"verify_http_{status}",
                http_status=status,
            )
    except SSOConvertError:
        raise
    except Exception as exc:
        raise SSOConvertError(f"device verify failed: {exc}", code="verify_failed") from exc

    # Step 5: POST device/approve —— 必须带 Cookie（根因：缺 cookie → 401，永远进不了 poll）
    try:
        if log:
            log("[SSO] 5/6 device approve")
        status, _ = do_post(
            APPROVE_URL,
            {
                "user_code": user_code,
                "action": "allow",
                "principal_type": "User",
                "principal_id": "",
            },
            proxy=proxy,
            timeout=30,
        )
        if status == 401:
            raise SSOConvertError(
                "device approve HTTP 401 (SSO cookie rejected)",
                permanent=True,
                code="approve_unauthorized",
                http_status=401,
            )
        if status not in (200, 204, 302, 303):
            raise SSOConvertError(
                f"device approve HTTP {status}",
                code=f"approve_http_{status}",
                http_status=status,
            )
    except SSOConvertError:
        raise
    except Exception as exc:
        raise SSOConvertError(f"device approve failed: {exc}", code="approve_failed") from exc

    # Step 6: Poll token
    if log:
        log("[SSO] 6/6 poll token")
    return _poll_device_token(
        device_code,
        CLIENT_ID,
        TOKEN_URL,
        interval,
        expires_in,
        proxy=proxy,
        cookies=cookies,
        log=log,
        post_form=do_post,
    )
